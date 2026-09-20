"""Recorded-video source: validation, framing, looping, cleanup and bounds.

The tests that need a decoder build their own clip with GStreamer and skip where GStreamer is
absent (CI runners have none). Everything that can be asserted without a decoder - refusing a
missing or unusable file, pipeline construction, bounded reads - runs everywhere.
"""

from __future__ import annotations

import itertools
import shutil
import subprocess
from pathlib import Path

import pytest

from veotrex_edge_agent.frame_source import (
    FrameSourceError,
    RecordedVideoConfig,
    RecordedVideoSource,
    SourceHealth,
    SourceKind,
    VideoDecoder,
    VideoProbe,
    build_pipeline,
    decode_jpeg,
    probe_video,
)

# Never opened: the pipeline-construction tests only inspect the generated argv.
PLACEHOLDER = Path("unopened-placeholder.mp4")
GSTREAMER = shutil.which("gst-launch-1.0") and shutil.which("gst-discoverer-1.0")
needs_gstreamer = pytest.mark.skipif(not GSTREAMER, reason="GStreamer tools unavailable")
CLIP_SECONDS = 4
CLIP_FPS = 15
CADENCE = 5


@pytest.fixture(scope="module")
def clip(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if not GSTREAMER:
        pytest.skip("GStreamer tools unavailable")
    target = tmp_path_factory.mktemp("recorded") / "clip.mp4"
    subprocess.run(  # noqa: S603 - fixed argv, executable resolved from PATH
        [
            str(shutil.which("gst-launch-1.0")),
            "-q",
            "videotestsrc",
            "pattern=ball",
            f"num-buffers={CLIP_SECONDS * CLIP_FPS}",
            "!",
            f"video/x-raw,width=320,height=240,framerate={CLIP_FPS}/1,format=I420",
            "!",
            "x264enc",
            "speed-preset=veryfast",
            "key-int-max=15",
            "!",
            "video/x-h264,profile=constrained-baseline",
            "!",
            "h264parse",
            "config-interval=-1",
            "!",
            "mp4mux",
            "faststart=true",
            "!",
            "filesink",
            f"location={target}",
        ],
        check=True,
        timeout=180,
        stdin=subprocess.DEVNULL,
        capture_output=True,
    )
    return target


# ---- configuration validation (no decoder needed) ---------------------------------------


@pytest.mark.parametrize("cadence", [0, 7, 99])
def test_unsupported_cadence_is_refused(cadence: int) -> None:
    with pytest.raises(FrameSourceError) as caught:
        RecordedVideoConfig(path=Path("/nonexistent.mp4"), cadence_fps=cadence)
    assert caught.value.category == "unsupported_cadence"


@pytest.mark.parametrize("quality", [0, 101])
def test_invalid_jpeg_quality_is_refused(quality: int) -> None:
    with pytest.raises(FrameSourceError) as caught:
        RecordedVideoConfig(path=Path("/nonexistent.mp4"), jpeg_quality=quality)
    assert caught.value.category == "invalid_jpeg_quality"


def test_missing_file_fails_safely(tmp_path: Path) -> None:
    with pytest.raises(FrameSourceError) as caught:
        probe_video(RecordedVideoConfig(path=tmp_path / "absent.mp4"))
    assert caught.value.category == "source_missing"


def test_a_directory_is_not_a_source(tmp_path: Path) -> None:
    target = tmp_path / "clip.mp4"
    target.mkdir()
    with pytest.raises(FrameSourceError) as caught:
        probe_video(RecordedVideoConfig(path=target))
    assert caught.value.category == "source_not_a_file"


def test_symlinks_are_refused(tmp_path: Path) -> None:
    real = tmp_path / "real.mp4"
    real.write_bytes(b"\x00" * 64)
    link = tmp_path / "link.mp4"
    link.symlink_to(real)
    with pytest.raises(FrameSourceError) as caught:
        probe_video(RecordedVideoConfig(path=link))
    assert caught.value.category == "source_is_symlink"


def test_unexpected_container_is_refused_by_name(tmp_path: Path) -> None:
    target = tmp_path / "clip.avi"
    target.write_bytes(b"\x00" * 64)
    with pytest.raises(FrameSourceError) as caught:
        probe_video(RecordedVideoConfig(path=target))
    assert caught.value.category == "unsupported_container"


def test_an_empty_file_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "clip.mp4"
    target.write_bytes(b"")
    with pytest.raises(FrameSourceError) as caught:
        probe_video(RecordedVideoConfig(path=target))
    assert caught.value.category == "source_empty"


def test_an_oversized_file_is_refused_before_decoding(tmp_path: Path) -> None:
    target = tmp_path / "clip.mp4"
    target.write_bytes(b"\x00" * 2048)
    with pytest.raises(FrameSourceError) as caught:
        probe_video(RecordedVideoConfig(path=target, max_file_bytes=1024))
    assert caught.value.category == "source_too_large"


@needs_gstreamer
def test_a_corrupt_file_fails_safely(tmp_path: Path) -> None:
    target = tmp_path / "clip.mp4"
    target.write_bytes(b"\x00\x00\x00\x20ftypmp42" + b"\xff" * 8192)
    with pytest.raises(FrameSourceError) as caught:
        probe_video(RecordedVideoConfig(path=target))
    assert caught.value.category == "source_undecodable"


def test_a_failure_category_never_leaks_the_path(tmp_path: Path) -> None:
    secret = tmp_path / "parents-private-footage.mp4"
    with pytest.raises(FrameSourceError) as caught:
        probe_video(RecordedVideoConfig(path=secret))
    assert "parents-private-footage" not in str(caught.value)


def test_starting_an_unusable_source_marks_it_failed(tmp_path: Path) -> None:
    source = RecordedVideoSource(RecordedVideoConfig(path=tmp_path / "absent.mp4"))
    with pytest.raises(FrameSourceError):
        source.start()
    status = source.status()
    assert status.health is SourceHealth.FAILED
    assert status.last_error_category == "source_missing"


def test_frames_before_start_are_refused(tmp_path: Path) -> None:
    source = RecordedVideoSource(RecordedVideoConfig(path=tmp_path / "absent.mp4"))
    with pytest.raises(FrameSourceError) as caught:
        next(source.frames())
    assert caught.value.category == "source_not_started"


# ---- pipeline construction ---------------------------------------------------------------


@needs_gstreamer
def test_software_decoding_is_the_default_and_hardware_is_opt_in() -> None:
    config = RecordedVideoConfig(path=PLACEHOLDER, cadence_fps=CADENCE)
    software = build_pipeline(config, VideoProbe(codec="H.264", duration_seconds=1.0))
    assert "openh264dec" in software
    assert "nvv4l2decoder" not in software
    assert f"video/x-raw,framerate={CADENCE}/1" in software
    hardware = build_pipeline(
        RecordedVideoConfig(path=PLACEHOLDER, decoder=VideoDecoder.NVIDIA),
        VideoProbe(codec="H.264", duration_seconds=1.0),
    )
    assert "nvv4l2decoder" in hardware


@needs_gstreamer
def test_h265_selects_its_own_parser_and_decoder() -> None:
    command = build_pipeline(
        RecordedVideoConfig(path=PLACEHOLDER),
        VideoProbe(codec="H.265", duration_seconds=1.0),
    )
    assert "h265parse" in command and "libde265dec" in command


@needs_gstreamer
def test_the_queue_is_bounded_so_a_slow_consumer_backpressures() -> None:
    command = build_pipeline(
        RecordedVideoConfig(path=PLACEHOLDER),
        VideoProbe(codec="H.264", duration_seconds=1.0),
    )
    assert "max-size-buffers=8" in command
    assert "leaky=no" in command


# ---- decoding ------------------------------------------------------------------------------


def test_decode_rejects_an_empty_payload() -> None:
    with pytest.raises(FrameSourceError) as caught:
        decode_jpeg(b"")
    assert caught.value.category == "empty_frame"


def test_decode_rejects_a_malformed_payload() -> None:
    with pytest.raises(FrameSourceError) as caught:
        decode_jpeg(b"\xff\xd8\xff" + b"garbage" * 40 + b"\xff\xd9")
    assert caught.value.category == "malformed_frame"


# ---- streaming behaviour (decoder required) ------------------------------------------------


@needs_gstreamer
def test_a_valid_source_probes_and_streams(clip: Path) -> None:
    config = RecordedVideoConfig(path=clip, loop=False, cadence_fps=CADENCE)
    probe = probe_video(config)
    assert probe.codec == "H.264"
    assert probe.duration_seconds == pytest.approx(CLIP_SECONDS, abs=0.5)
    source = RecordedVideoSource(config)
    source.start()
    try:
        frames = list(source.frames())
    finally:
        source.stop()
    assert frames, "the clip produced no frames"
    assert all(frame.kind is SourceKind.RECORDED_DEMO for frame in frames)
    assert frames[0].rgb.shape == (240, 320, 3)
    assert frames[0].encoded_jpeg.startswith(b"\xff\xd8\xff")


@needs_gstreamer
def test_end_of_file_without_loop_ends_cleanly(clip: Path) -> None:
    source = RecordedVideoSource(RecordedVideoConfig(path=clip, loop=False, cadence_fps=CADENCE))
    source.start()
    try:
        count = sum(1 for _ in source.frames())
    finally:
        source.stop()
    assert count == pytest.approx(CLIP_SECONDS * CADENCE, abs=2)
    assert source.status().loops_completed == 0


@needs_gstreamer
def test_looping_continues_past_the_end_and_keeps_time_monotonic(clip: Path) -> None:
    """Media time restarts each loop; the tracker's clock must not go backwards with it."""
    source = RecordedVideoSource(RecordedVideoConfig(path=clip, loop=True, cadence_fps=CADENCE))
    source.start()
    try:
        frames = list(itertools.islice(source.frames(), CLIP_SECONDS * CADENCE + 5))
    finally:
        source.stop()
    assert source.status().loops_completed >= 1
    monotonic = [frame.monotonic_timestamp_seconds for frame in frames]
    assert all(later > earlier for earlier, later in itertools.pairwise(monotonic))
    assert min(frame.media_timestamp_seconds for frame in frames[-3:]) < max(
        frame.media_timestamp_seconds for frame in frames[: CLIP_SECONDS * CADENCE]
    )
    assert frames[-1].loop_index >= 1


@needs_gstreamer
def test_stopping_reaps_the_decoder_process(clip: Path) -> None:
    source = RecordedVideoSource(RecordedVideoConfig(path=clip, loop=True, cadence_fps=CADENCE))
    source.start()
    next(source.frames())
    process = source._process
    assert process is not None
    source.stop()
    assert source._process is None
    assert process.poll() is not None, "decoder process outlived the source"
    assert source.status().health is SourceHealth.STOPPED


@needs_gstreamer
def test_the_context_manager_cleans_up(clip: Path) -> None:
    with RecordedVideoSource(
        RecordedVideoConfig(path=clip, loop=False, cadence_fps=CADENCE)
    ) as source:
        assert source.status().health is SourceHealth.RUNNING
    assert source.status().health is SourceHealth.STOPPED


@needs_gstreamer
def test_an_undersized_frame_bound_fails_safely_rather_than_buffering(clip: Path) -> None:
    source = RecordedVideoSource(
        RecordedVideoConfig(path=clip, loop=False, cadence_fps=CADENCE, max_frame_bytes=64)
    )
    source.start()
    try:
        with pytest.raises(FrameSourceError) as caught:
            list(source.frames())
    finally:
        source.stop()
    assert caught.value.category == "frame_too_large"
