# VeoTrex macOS live camera demo

A self-contained person-detection and tracking demo for an Apple Silicon Mac, using the
built-in camera or an iPhone through Continuity Camera.

This is an isolated demo utility. It does not touch the VeoTrex control plane, the edge
agent, Ring, or any production service.

## 1. Clone

```bash
git clone https://github.com/cprakash64/VeoTrex_DayCare.git
cd VeoTrex_DayCare
git checkout stage/demo-01-recorded-demo-runtime
```

Already cloned? `git pull` instead.

## 2. Isolated environment

```bash
python3 -m venv .venv-demo
source .venv-demo/bin/activate
python3 -m pip install --upgrade pip
```

## 3. Dependencies

```bash
pip install -r requirements-mac-demo.txt
```

First install pulls PyTorch and takes a few minutes.

## 4. Camera permission

**System Settings → Privacy & Security → Camera → enable Terminal (or iTerm).**

Quit and reopen the terminal completely afterwards, or macOS keeps denying access.

## 5. Find your cameras

```bash
python3 list_mac_cameras.py
```

```
Camera 0: AVAILABLE - 1280x720
Camera 1: AVAILABLE - 1920x1080
```

## 6. Run — Mac built-in camera

```bash
python3 veotrex_mac_demo.py --camera 0
```

## Live room dashboard (local customer demo)

Use two terminals from this repository. In Terminal 1, with `.venv-demo` activated:

```bash
python3 veotrex_mac_demo.py --camera 0 --imgsz 416
```

In Terminal 2:

```bash
python3 demo_dashboard.py
```

Open **http://127.0.0.1:8765**. The dashboard binds only to `127.0.0.1` and uses
in-memory state. The camera sends person count, tracked count, measured FPS, status and
timestamp to that local process. A missing dashboard never stops camera inference.

The count must remain at a new value for at least 0.6 seconds of telemetry before it
becomes stable. Only stable count changes produce entry or exit events. Up to ten recent
events are retained. If telemetry stops for 2.5 seconds, the dashboard reports camera
unavailable and hides the occupancy value until data resumes. Restarting the dashboard
clears its in-memory count and activity.

This page follows the existing VeoTrex dashboard styling but runs separately because the
owner dashboard requires Auth0 and a configured production API identity. No production
credentials or database are needed for this local customer demo. The number is a detected
person count within the camera frame; it is not doorway direction tracking.

## 7. Run — iPhone / alternate camera

```bash
python3 veotrex_mac_demo.py --camera 1
```

Use whichever index `list_mac_cameras.py` reported. It is not always 1 — a USB webcam or
virtual camera can take that slot, so check the list rather than guessing.

## 8. Quit

**Q** or **ESC**, or close the window.

## Useful flags

| Flag | Default | Notes |
|---|---|---|
| `--camera` | `0` | Index from `list_mac_cameras.py` |
| `--imgsz` | `640` | Drop to `416` if the demo feels sluggish |
| `--conf` | `0.35` | Raise to reduce false positives |
| `--device` | `auto` | `auto` prefers Apple GPU (MPS), falls back to CPU |

## Model

Uses Ultralytics `yolov8n.pt`, **downloaded automatically on first run** (~6 MB) into the
working directory. No model file is committed to this repository — the production VeoTrex
detector is a TensorRT engine built for the Jetson and will not run on a Mac.

The first run therefore needs internet once. After that the demo works offline.

## Troubleshooting

**Camera permission denied** — grant access in System Settings, then fully quit and reopen
the terminal. Newly granted access does not apply to an already-running terminal.

**Camera index unavailable** — rerun `list_mac_cameras.py`; indexes shift when devices
connect or disconnect.

**Model download fails** — first run needs internet. Behind a proxy, download `yolov8n.pt`
manually and pass `--model /path/to/yolov8n.pt`.

**MPS unavailable** — the demo prints a notice and continues on CPU. Lower `--imgsz` to 416.

**Continuity Camera not appearing** — unlock the iPhone, keep it near the Mac, ensure both
are on the same Apple ID with Wi-Fi and Bluetooth on, and check
*Settings → General → AirPlay & Continuity → Continuity Camera* on the phone. Then rerun
`list_mac_cameras.py`.

## What this demo shows

Live person detection with persistent track IDs, a live count, and measured processing rate.
It does not classify people, estimate age, or recognise faces.
