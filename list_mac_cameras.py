#!/usr/bin/env python3
"""Scan a bounded range of macOS camera indexes and report which ones deliver a frame.

Use this to find the index of an iPhone attached through Continuity Camera; it is not always
1. Every capture handle is released, including on failure, so repeated runs stay reliable.

    python3 list_mac_cameras.py
    python3 list_mac_cameras.py --max-index 7
"""

from __future__ import annotations

import argparse
import sys

DEFAULT_MAX_INDEX = 5


def _backend(cv2_module: object) -> int:
    """AVFoundation on macOS; anything else falls back to the platform default."""
    if sys.platform == "darwin":
        return int(getattr(cv2_module, "CAP_AVFOUNDATION", 0))
    return int(getattr(cv2_module, "CAP_ANY", 0))


def probe(index: int, cv2_module: object) -> tuple[bool, int, int]:
    """Open one index, read a single frame, and always release the handle."""
    capture = cv2_module.VideoCapture(index, _backend(cv2_module))  # type: ignore[attr-defined]
    try:
        if not capture.isOpened():
            return False, 0, 0
        ok, frame = capture.read()
        if not ok or frame is None:
            return False, 0, 0
        height, width = frame.shape[:2]
        return True, int(width), int(height)
    finally:
        capture.release()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="List macOS cameras that return a frame (built-in, USB, Continuity Camera)."
    )
    parser.add_argument(
        "--max-index",
        type=int,
        default=DEFAULT_MAX_INDEX,
        help=f"Highest camera index to probe (default: {DEFAULT_MAX_INDEX}).",
    )
    args = parser.parse_args()
    if args.max_index < 0 or args.max_index > 20:
        parser.error("--max-index must be between 0 and 20")

    try:
        import cv2
    except ImportError:
        print("OpenCV is not installed. Run: pip install -r requirements-mac-demo.txt")
        return 1

    print(f"Scanning camera indexes 0-{args.max_index} ...\n")
    found = 0
    for index in range(args.max_index + 1):
        available, width, height = probe(index, cv2)
        if available:
            found += 1
            print(f"Camera {index}: AVAILABLE - {width}x{height}")
        else:
            print(f"Camera {index}: not available")

    print()
    if found == 0:
        print("No cameras returned a frame.")
        print("  - Check System Settings > Privacy & Security > Camera and allow your terminal.")
        print("  - For an iPhone, unlock it and keep it near the Mac with Continuity Camera on.")
        return 1
    print(f"{found} camera(s) available. Run the demo with, for example:")
    print("  python3 veotrex_mac_demo.py --camera 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
