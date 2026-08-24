# Jetson qualification preflight

Run the read-only inspection before any Ring stream test:

```bash
uv run --package veotrex-edge-agent veotrex-edge qualify-env
```

The JSON reports architecture, OS/kernel, Jetson model, L4T release when detectable, memory and
storage, GStreamer version, `rtspsrc`, H.264/H.265 depayloaders/parsers, NVIDIA and software decoder
plugins, a TLS-backend hint, installed DeepStream paths (informational only), network interfaces, and
time-sync status when the platform exposes it.

Review these prerequisites manually:

- system time is synchronized so TLS and cross-session correlation are meaningful;
- `rtspsrc` supports RTSPS and the GLib TLS backend is present;
- `rtph264depay`, `h264parse`, `rtph265depay`, and `h265parse` are discoverable;
- `nvv4l2decoder` is present for Jetson decode qualification;
- storage has bounded room for JSON reports only—no media;
- the authorized network permits outbound TLS/TCP to Ring's RTSPS endpoint on port 322.

During a run, the collector samples process CPU/RSS, system RAM/load, NIC byte counters, disk, and
`tegrastats` when available. Optional unavailable metrics do not fail the run and their availability
is explicit. Review GPU use, temperatures, power, and throttling indicators alongside media loss.
The collector does not require `jtop`.

This command does not flash or upgrade the device, install JetPack/DeepStream/drivers, change power
mode, modify configuration, or start a live stream. Remediate missing platform components through an
authorized NVIDIA deployment procedure, then repeat inspection. DeepStream discovery does not place
DeepStream in the Stage 1D-A media path.
