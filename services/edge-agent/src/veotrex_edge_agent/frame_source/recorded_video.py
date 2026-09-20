"""Recorded-video frame source for demonstrations.

A local file is decoded by GStreamer into a bounded stream of JPEG frames on a pipe, decoded
to RGB, and handed to the same detector and tracker a camera would feed. There is no second
inference path and no pre-computed result anywhere in this module: if the model finds nothing
in the clip, the demo shows nothing.

Decoder selection is explicit rather than delegated to ``decodebin``. On this Jetson decodebin
ranks ``nvv4l2decoder`` first and that element rejects ordinary H.264 files with "Unsupported
Codec", so the software decoder is the default and hardware is opt-in.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import structlog

from veotrex_edge_agent.frame_source.source import (
    FrameSourceError,
    SourceFrame,
    SourceHealth,
    SourceKind,
    SourceStatus,
    decode_jpeg,
)

JPEG_START = b"\xff\xd8\xff"
JPEG_END = b"\xff\xd9"
SUPPORTED_CADENCE_FPS = frozenset({1, 2, 3, 4, 5, 6, 8, 10, 12, 15})
# qtdemux covers MP4 and MOV. Anything else is refused by name rather than guessed at.
SUPPORTED_SUFFIXES = frozenset({".mp4", ".m4v", ".mov"})
_H264 = re.compile(r"video #\d+: H\.264", re.IGNORECASE)
_H265 = re.compile(r"video #\d+: (H\.265|HEVC)", re.IGNORECASE)


class VideoDecoder(StrEnum):
    SOFTWARE = "software"
    NVIDIA = "nvidia"


@dataclass(frozen=True, slots=True)
class RecordedVideoConfig:
    path: Path
    loop: bool = True
    cadence_fps: int = 5
    decoder: VideoDecoder = VideoDecoder.SOFTWARE
    jpeg_quality: int = 85
    # One frame can never grow past this, so a malformed stream cannot exhaust memory.
    max_frame_bytes: int = 4 * 1024 * 1024
    max_file_bytes: int = 4 * 1024 * 1024 * 1024
    read_chunk_bytes: int = 64 * 1024
    probe_timeout_seconds: float = 30.0
    stop_timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        if self.cadence_fps not in SUPPORTED_CADENCE_FPS:
            raise FrameSourceError("unsupported_cadence")
        if not 1 <= self.jpeg_quality <= 100:
            raise FrameSourceError("invalid_jpeg_quality")
        if min(self.max_frame_bytes, self.max_file_bytes, self.read_chunk_bytes) < 1:
            raise FrameSourceError("invalid_bounds")
        if min(self.probe_timeout_seconds, self.stop_timeout_seconds) <= 0:
            raise FrameSourceError("invalid_timeouts")


@dataclass(frozen=True, slots=True)
class VideoProbe:
    codec: str
    duration_seconds: float | None


def probe_video(config: RecordedVideoConfig) -> VideoProbe:
    """Refuse an unusable file before any frame is produced.

    Every rejection is a category name. The path never reaches the message, because a demo
    operator's directory layout is not something to spray into logs.
    """
    discoverer = shutil.which("gst-discoverer-1.0")
    if discoverer is None:
        raise FrameSourceError("gstreamer_unavailable")
    path = config.path
    if not path.exists():
        raise FrameSourceError("source_missing")
    if path.is_symlink():
        raise FrameSourceError("source_is_symlink")
    if not path.is_file():
        raise FrameSourceError("source_not_a_file")
    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise FrameSourceError("unsupported_container")
    try:
        size = path.stat().st_size
    except OSError:
        raise FrameSourceError("source_unreadable") from None
    if size < 1:
        raise FrameSourceError("source_empty")
    if size > config.max_file_bytes:
        raise FrameSourceError("source_too_large")
    try:
        completed = subprocess.run(  # noqa: S603 - executable resolved from trusted PATH
            [discoverer, str(path)],
            capture_output=True,
            text=True,
            timeout=config.probe_timeout_seconds,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise FrameSourceError("probe_timeout") from None
    except OSError:
        raise FrameSourceError("probe_failed") from None
    report = completed.stdout
    if completed.returncode != 0 or "video #" not in report:
        raise FrameSourceError("source_undecodable")
    if _H264.search(report):
        codec = "H.264"
    elif _H265.search(report):
        codec = "H.265"
    else:
        raise FrameSourceError("unsupported_video_codec")
    duration: float | None = None
    match = re.search(r"Duration: (\d+):(\d\d):(\d\d)\.(\d+)", report)
    if match:
        hours, minutes, seconds, fraction = match.groups()
        duration = int(hours) * 3600 + int(minutes) * 60 + int(seconds) + float(f"0.{fraction}")
    return VideoProbe(codec=codec, duration_seconds=duration)


def build_pipeline(config: RecordedVideoConfig, probe: VideoProbe) -> list[str]:
    launcher = shutil.which("gst-launch-1.0")
    if launcher is None:
        raise FrameSourceError("gstreamer_unavailable")
    if probe.codec == "H.264":
        parser, software_decoder = "h264parse", "openh264dec"
    else:
        parser, software_decoder = "h265parse", "libde265dec"
    if config.decoder is VideoDecoder.NVIDIA:
        decoder_elements = ["nvv4l2decoder", "!", "nvvidconv"]
    else:
        decoder_elements = [software_decoder]
    return [
        launcher,
        "-q",
        "filesrc",
        f"location={config.path}",
        "!",
        "qtdemux",
        "!",
        # A bounded queue is the whole backpressure story: the reader pulls frames one at a
        # time, so a slow detector stalls the decoder instead of growing a buffer.
        "queue",
        "max-size-buffers=8",
        "max-size-bytes=0",
        "max-size-time=0",
        "leaky=no",
        "!",
        parser,
        "!",
        *decoder_elements,
        "!",
        "videoconvert",
        "!",
        "videorate",
        "!",
        f"video/x-raw,framerate={config.cadence_fps}/1",
        "!",
        "jpegenc",
        f"quality={config.jpeg_quality}",
        "!",
        "fdsink",
        "fd=1",
    ]


class RecordedVideoSource:
    """Streams a local recording as decoded frames, optionally looping.

    Not thread-safe for concurrent iteration; one pipeline owns one source.
    """

    kind = SourceKind.RECORDED_DEMO

    def __init__(
        self, config: RecordedVideoConfig, *, stream_instance_id: str | None = None
    ) -> None:
        self._config = config
        self._stream_instance_id = stream_instance_id or f"recorded-demo:{config.path.name}"
        self._probe: VideoProbe | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._health = SourceHealth.STOPPED
        self._frames_emitted = 0
        self._loops_completed = 0
        self._decode_failures = 0
        self._last_error: str | None = None
        self._stop_requested = threading.Event()
        self._logger = structlog.get_logger()

    @property
    def stream_instance_id(self) -> str:
        return self._stream_instance_id

    @property
    def probe(self) -> VideoProbe | None:
        return self._probe

    def status(self) -> SourceStatus:
        return SourceStatus(
            kind=self.kind,
            health=self._health,
            frames_emitted=self._frames_emitted,
            loops_completed=self._loops_completed,
            decode_failures=self._decode_failures,
            last_error_category=self._last_error,
        )

    def start(self) -> VideoProbe:
        """Validate the file and open the first decoder process."""
        self._stop_requested.clear()
        self._health = SourceHealth.STARTING
        try:
            self._probe = probe_video(self._config)
        except FrameSourceError as exc:
            self._health = SourceHealth.FAILED
            self._last_error = exc.category
            raise
        self._spawn()
        return self._probe

    def _spawn(self) -> None:
        assert self._probe is not None
        command = build_pipeline(self._config, self._probe)
        try:
            self._process = subprocess.Popen(  # noqa: S603 - argv only, resolved from PATH
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                bufsize=0,
            )
        except OSError:
            self._health = SourceHealth.FAILED
            self._last_error = "decoder_spawn_failed"
            raise FrameSourceError("decoder_spawn_failed") from None
        self._health = SourceHealth.RUNNING

    def frames(self) -> Iterator[SourceFrame]:
        """Yield decoded frames until the clip ends, looping when configured."""
        if self._process is None:
            raise FrameSourceError("source_not_started")
        while True:
            produced_this_loop = 0
            for payload in self._read_jpeg_stream():
                if self._stop_requested.is_set():
                    return
                try:
                    rgb = decode_jpeg(payload)
                except FrameSourceError as exc:
                    # One unreadable frame must not end a demo; a persistently broken
                    # stream still surfaces through decode_failures and the health state.
                    self._decode_failures += 1
                    self._last_error = exc.category
                    continue
                height, width, _ = rgb.shape
                sequence_in_loop = produced_this_loop
                yield SourceFrame(
                    kind=self.kind,
                    stream_instance_id=self._stream_instance_id,
                    sequence=self._frames_emitted,
                    media_timestamp_seconds=sequence_in_loop / self._config.cadence_fps,
                    monotonic_timestamp_seconds=self._frames_emitted / self._config.cadence_fps,
                    loop_index=self._loops_completed,
                    width=width,
                    height=height,
                    rgb=rgb,
                    encoded_jpeg=payload,
                )
                self._frames_emitted += 1
                produced_this_loop += 1
            self._reap()
            if self._stop_requested.is_set() or not self._config.loop:
                self._health = SourceHealth.ENDED
                return
            if produced_this_loop == 0:
                # Looping over a file that yields nothing would spin forever.
                self._health = SourceHealth.FAILED
                self._last_error = "empty_loop"
                raise FrameSourceError("empty_loop")
            self._loops_completed += 1
            self._spawn()

    def _read_jpeg_stream(self) -> Iterator[bytes]:
        """Frame the pipe into JPEGs with a hard cap on any single frame."""
        process = self._process
        assert process is not None and process.stdout is not None
        buffer = bytearray()
        while True:
            chunk = process.stdout.read(self._config.read_chunk_bytes)
            if not chunk:
                break
            buffer.extend(chunk)
            while True:
                start = buffer.find(JPEG_START)
                if start < 0:
                    break
                end = buffer.find(JPEG_END, start + len(JPEG_START))
                if end < 0:
                    break
                frame = bytes(buffer[start : end + len(JPEG_END)])
                del buffer[: end + len(JPEG_END)]
                if len(frame) > self._config.max_frame_bytes:
                    self._fail("frame_too_large")
                yield frame
            # A stream that opens a JPEG and never closes it would otherwise grow the
            # buffer without limit; this is the bound that makes that impossible.
            if len(buffer) > self._config.max_frame_bytes:
                self._fail("frame_too_large")

    def _fail(self, category: str) -> None:
        self._health = SourceHealth.FAILED
        self._last_error = category
        raise FrameSourceError(category)

    def _reap(self) -> None:
        process = self._process
        if process is None:
            return
        try:
            if process.stdout is not None:
                process.stdout.close()
        except OSError:
            pass
        try:
            process.wait(timeout=self._config.stop_timeout_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=self._config.stop_timeout_seconds)
        self._process = None

    def restart(self) -> None:
        """Begin the clip again from the start, keeping monotonic time moving forward."""
        self._reap()
        if self._probe is None:
            raise FrameSourceError("source_not_started")
        self._loops_completed += 1
        self._stop_requested.clear()
        self._spawn()

    def stop(self) -> None:
        self._stop_requested.set()
        process = self._process
        if process is not None and process.poll() is None:
            process.terminate()
        self._reap()
        if self._health is not SourceHealth.FAILED:
            self._health = SourceHealth.STOPPED

    def __enter__(self) -> RecordedVideoSource:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()
