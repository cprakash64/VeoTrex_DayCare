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
