# Jetson edge-agent supervision runbook (V1-00B)

How the VeoTrex edge agent runs as a supervised, non-root systemd service on the NVIDIA Jetson
Orin Nano Super. Source of truth: this directory. Nothing here touches the Hostinger control
plane.

| File | Purpose |
|---|---|
| `veotrex-edge.service` | the production unit, installed verbatim to `/etc/systemd/system/` |
| `veotrex-edge-ctl.sh` | the only supported install / build / activate / rollback / status tool |
| `edge.env.example` | template for `/etc/veotrex-edge/edge.env` (non-secret config) |
| `user-scope-failure-injection.sh` | root-less proof of the unit's restart, kill, hardening and exit-code semantics |

## What is supervised

`veotrex-edge-agent` (`veotrex_edge_agent.main:main`): a foreground asyncio process that
validates its configuration, starts one child (the TensorRT GPU worker, `/usr/bin/python3 -I
.../gpu_worker/worker.py`), health-checks it every heartbeat, restarts it with bounded backoff,
and exits cleanly on SIGTERM after stopping the worker. It opens no camera, media or network
connection today; that is the R3D lifecycle shell the repository ships. systemd owns the agent
PID directly (`Type=notify`): no shell wrapper, no daemonising, no tmux.

## Layout

| Class | Path | Owner : mode | Notes |
|---|---|---|---|
| code | `/opt/veotrex-edge/releases/<commit>/venv` | operator : 0755 | detached, non-editable `uv sync --frozen --no-dev --no-editable --package veotrex-edge-agent`, built at its final path |
| provenance | `/opt/veotrex-edge/releases/<commit>/release.env` | operator : 0644 | `VEOTREX_EDGE_SOURCE_COMMIT`, `VEOTREX_EDGE_VERSION`, build time |
| active release | `/opt/veotrex-edge/current` -> release | root | atomic symlink switch; `previous` remembers the last one |
| config | `/etc/veotrex-edge/edge.env` | root : veotrex-edge, 0640 | node id, environment, log level, heartbeat; created once, never overwritten |
| secrets | `/etc/veotrex-edge/` via `LoadCredential=` | root : 0600 | none exist for the edge agent today; never `Environment=` |
| state | `/var/lib/veotrex-edge` | veotrex-edge : 0750 | `StateDirectory=`; the only writable path besides a private `/tmp` |
| logs | journald (`journalctl -u veotrex-edge`) | | structured JSON lines on stdout; no flat file |
| models | none loaded in V1-00B | | `artifacts/models/**/manifest.json` carries sha256/licence/platform for every candidate; a production model store with manifest verification (`veotrex_edge_agent.model_artifacts`) is a later stage |

The working tree under `/home/cprakash64` is never executed by the service: `/home/cprakash64`
is mode 0750 and `ProtectHome=yes` hides it. Releases are built from a clean checkout of an
exact commit and `build` refuses a dirty tracked tree.

## Service identity

`veotrex-edge`: system account, `/usr/sbin/nologin`, home `/var/lib/veotrex-edge`, no sudo, not
in `docker`. Supplementary groups exactly `video` and `render`, measured on this host: the
worker opens `/dev/nvmap` and `/dev/nvgpu/igpu0/ctrl` (group `video`) and `/dev/dri/renderD128`
(group `render`). `install` sets the groups with `usermod -G`, so anything extra is removed.

## First installation (operator, on the Jetson)

```bash
cd /home/cprakash64/Documents/VeoTrex/VeoTrex_DayCare/VeoTrex_ChildCare
infra/jetson/veotrex-edge-ctl.sh preflight
```

```bash
sudo infra/jetson/veotrex-edge-ctl.sh install
```

`preflight` is read-only and runs as any user. `/etc/veotrex-edge` is `0750 root:veotrex-edge`,
so the operator cannot see whether `edge.env` exists inside it; preflight and status then
report `config: protected (presence not observable as <user>; /etc/veotrex-edge is not
searchable)` rather than guessing. Only a privileged run reports the file's metadata. The
directory mode is never weakened to make the report prettier.

`install` is idempotent and safe on any partial state: identity (created once, groups
converged to exactly `video render`), directories, `edge.env` (generated node id, preserved
byte for byte if present), the unit, a pre-release `systemd-analyze verify`, `daemon-reload`.
It never starts and never enables the service: before the first `activate` there is no
`/opt/veotrex-edge/current`, so the ExecStart target does not exist by design, and a unit whose
executable is known to be absent must not be wired into boot. `install` ends with

```
host infrastructure installed: account, directories, config, unit (daemon reloaded)
release not yet activated: /opt/veotrex-edge/current absent
service intentionally disabled and inactive until 'activate <sha>'
```

which is the intended state, not a failure. If an earlier attempt left the unit enabled
without a release, `install` disables it.

```bash
infra/jetson/veotrex-edge-ctl.sh build
```

```bash
sudo infra/jetson/veotrex-edge-ctl.sh activate <sha> --restart
```

`activate` is where boot enablement happens: it switches `current` atomically, checks the
ExecStart target is executable, runs the **strict** `systemd-analyze verify` on the installed
unit, `daemon-reload`s and only then `systemctl enable`s. If verify fails it restores the
previous `current` (or removes the symlink on a first activation), enables nothing, and
exits 1. `--restart` then restarts and waits up to 90 s for `active` with a status line
beginning `ready`; it exits 2 on `degraded` (agent up, GPU worker not READY) and 1 on
failure, printing the last journal lines and the rollback command.

### systemd verify policy

| Phase | `systemd-analyze verify` | Tolerated finding |
|---|---|---|
| `install`, no `current` yet | pre-release | exactly `Command /opt/veotrex-edge/current/venv/bin/veotrex-edge-agent is not executable: No such file or directory`; anything else about the unit fails the install |
| `install` with a `current` | strict | none |
| `activate` (every time) | strict | none; failure rolls the symlink back and never enables |

No placeholder executable and no wrapper is ever installed to satisfy the tool.

### Recovering a partial first install

An interrupted `install` (for example, the shell closing after `sudo`) leaves a safe,
convergent state: account and groups, directories, `edge.env`, the unit; no `current`, service
disabled and inactive, no processes. Re-running `sudo infra/jetson/veotrex-edge-ctl.sh install`
reuses the account, preserves `edge.env` and its node id byte for byte, re-asserts modes,
reloads systemd, and reports the lines above. Then continue with `build` and `activate`.

## Update

```bash
infra/jetson/veotrex-edge-ctl.sh update
```

Fast-forward-only merge of `origin/main`, then `build`; it prints the exact `activate` command.
Only the edge service restarts. Never `git reset --hard` on the Jetson.

## Rollback

```bash
sudo infra/jetson/veotrex-edge-ctl.sh rollback
```

Re-activates `/opt/veotrex-edge/previous` (or a given `<sha>`) and restarts. Old releases stay
on disk (about 185 MB each); remove them by hand when there are more than three.

## Health and status

```bash
systemctl status veotrex-edge
```

`Status:` is the agent's own line, updated live over `sd_notify`:

```
ready commit=9deae867020f version=0.1.0+9deae867020f worker=READY worker_restarts=0 uptime_s=120 last_error=none
```

| Concept | Meaning | Where |
|---|---|---|
| liveness | event loop answers the systemd watchdog every 30 s (`WatchdogSec=60s`); silence means SIGABRT and restart | `systemctl show -p WatchdogTimestamp veotrex-edge` |
| readiness | configuration valid, startup complete: `READY=1` sent, unit `active (running)` | `systemctl is-active veotrex-edge` |
| degraded | alive and ready, GPU worker not READY; `last_error=` names the safe category | `Status:` line starts with `degraded` |
| version | `commit=` and `version=` in the status line; `/opt/veotrex-edge/current/release.env` | `infra/jetson/veotrex-edge-ctl.sh status` |

`systemctl show -p WatchdogUSec` reports the *effective* watchdog of the running instance:
`infinity` whenever the service is not running (never started, or stopped) and `1min` while
it runs. The configured value is always visible with `systemctl cat veotrex-edge | grep
Watchdog`. `infinity` on an inactive unit is therefore expected and is not a unit defect;
the proof that matters is `WatchdogUSec=1min` plus an advancing `WatchdogTimestamp` on the
running system service.
| restarts | `NRestarts` (systemd) and `worker_restarts=` (agent's own worker recoveries) | `systemctl show -p NRestarts veotrex-edge` |

Readiness deliberately does not depend on Ring, the VPS or any network: an offline daycare LAN
still has a live, ready edge agent. Camera/provider connectivity will be a separate `degraded`
reason when the agent gains those responsibilities.

## Failure semantics

| Situation | Class | Behaviour |
|---|---|---|
| no Internet at boot | transient external | starts normally; `After=network.target` only, never `network-online.target` |
| Ring / VPS unavailable | transient external | not contacted by this agent; no effect |
| camera unavailable | transient external | not opened by this agent; future: `degraded`, not failed |
| `/etc/veotrex-edge/edge.env` missing | fatal configuration | unit fails to start (`Result=resources`); `Restart=on-failure` retries within the start limit, then stays failed |
| a required setting missing/invalid | fatal configuration | agent logs `edge_config_invalid` naming the field and exits 78; `RestartPreventExitStatus=78` means **no restart** and no loop |
| GPU runtime / TensorRT unusable | local dependency | agent stays up, status `degraded ... last_error=gpu_worker_start_failed:<category>`; retried by the worker supervisor within its circuit breaker |
| worker process dies | local dependency | next heartbeat: `gpu_worker_health_failed`, bounded backoff restart, status back to `ready`; after 3 failures in 60 s the worker circuit opens (`FAILED`) and the agent remains `degraded` |
| agent crashes or wedges | supervision | `Restart=on-failure` after 10 s (watchdog abort counts); at most 5 starts per 10 min, then `start-limit-hit` until `systemctl reset-failed veotrex-edge` |
| `SIGTERM` (stop, restart, shutdown) | supervision | agent notifies `STOPPING=1`, stops the worker (SHUTDOWN, SIGTERM, SIGKILL, 2 s each), exits 0; `KillMode=mixed` SIGKILLs any survivor at 30 s |

## Logging

The agent writes one JSON object per line to stdout; journald keeps them under the unit. The
worker's stdio is `/dev/null`; its state changes are logged by the agent with safe categories.
Reviewed classes that never appear: access/refresh tokens, authorization codes, DSNs, vault
keys, Ring client secrets, raw secret files. The agent holds none of them; the only
identifiers logged are the node id and the source commit, and the `edge_config_invalid` record
names fields, never values.

```bash
journalctl -u veotrex-edge -b
```

```bash
journalctl -u veotrex-edge -n 100 --no-pager
```

```bash
journalctl -u veotrex-edge -p err --since "2026-09-22 00:00"
```

Retention is the host's journald policy: `/etc/systemd/journald.conf` is at defaults
(`SystemMaxUse` = 10 % of the filesystem, capped at 4 GiB; 356 MB in use at V1-00B), and
`ForwardToSyslog=yes` also feeds rsyslog's `/var/log/syslog` under the host's logrotate policy.
No VeoTrex flat-file log exists.

## Hardening exceptions

Enabled and validated against the real agent and worker: `NoNewPrivileges`, `PrivateTmp`,
`ProtectSystem=strict`, `ProtectHome`, `ProtectKernelTunables`, `ProtectKernelModules`,
`ProtectKernelLogs`, `ProtectControlGroups`, `ProtectClock`, `ProtectHostname`,
`ProtectProc=invisible`, `ProcSubset=pid`, `RestrictSUIDSGID`, `RestrictRealtime`, `RestrictNamespaces`,
`LockPersonality`, `SystemCallArchitectures=native`, empty `CapabilityBoundingSet` and
`AmbientCapabilities`, `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6`, `RemoveIPC`,
`SystemCallFilter=@system-service`, `UMask=0077`, `MemoryMax=3G`, `TasksMax=128`. The
`/proc` and syscall restrictions were each checked against the real worker's CUDA/TensorRT
probe before being enabled.

Not enabled, on purpose:

- `PrivateDevices=` hides `/dev/nvmap` and `/dev/nvgpu`: the worker cannot reach the GPU.
- `DevicePolicy=closed` with a `DeviceAllow=` list could not be validated without root on the
  V1-00B bench (device cgroups are a no-op in a user manager). Tighten in a follow-up once
  proven on the installed service.
- `MemoryDenyWriteExecute=` breaks CUDA/TensorRT, which map W+X pages.

## Verification without root

```bash
systemd-analyze verify infra/jetson/veotrex-edge.service
```

```bash
infra/jetson/user-scope-failure-injection.sh \
  --release /opt/veotrex-edge/current --work /tmp/veotrex-fi --soak 600
```

The harness copies the unit into the operator's user manager (dropping only `User=`,
`Group=`, `SupplementaryGroups=` and the capability directives a user manager cannot apply)
and proves start, stop, restart, crash restart, start-limit, SIGTERM without orphans, worker
death and recovery, `EX_CONFIG` without a loop, degraded-not-failed when the worker cannot be
launched, and the read-only view of `/usr`, `/etc` and `/opt`. `ProtectHome=` and `PrivateTmp=`
are inert in a user manager and are proven only on the installed system unit. It never touches
the real service.

## Do not

- Run the agent as root, from the working tree, or under tmux/nohup.
- Put a secret in `edge.env` or `Environment=`; `install` refuses secret-looking keys.
- `chown -R` the repository to `veotrex-edge`; the service never owns code.
- Reboot the Jetson to test boot start without operator approval; `install` enables the unit
  and `systemctl is-enabled veotrex-edge` proves the link.
