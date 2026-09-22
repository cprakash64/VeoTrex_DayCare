#!/usr/bin/env bash
# user-scope-failure-injection.sh - prove the veotrex-edge unit's supervision semantics without
# root, using a transient copy of the real unit in the operator's systemd user manager.
#
# The system unit is copied verbatim except for what a user manager cannot apply (User=,
# Group=, SupplementaryGroups=, CapabilityBoundingSet=, AmbientCapabilities=,
# ProtectKernelModules=, ProtectClock=, ProtectKernelLogs=) and the three paths that point at
# the production release/config/state, which are redirected into a scratch directory. Restart,
# start-limit, watchdog, kill, hardening and exit-code behaviour are therefore the real ones.
# Nothing touches /etc, /opt, /var, the real service, or any production state.
#
#   $0 --release <dir containing venv/bin/veotrex-edge-agent> --work <scratch dir> [--soak N]
set -euo pipefail

RELEASE= WORK= SOAK=0
while [ $# -gt 0 ]; do
  case "$1" in
    --release) RELEASE=$2; shift;;
    --work) WORK=$2; shift;;
    --soak) SOAK=$2; shift;;
    *) echo "unknown argument $1" >&2; exit 64;;
  esac; shift
done
[ -x "$RELEASE/venv/bin/veotrex-edge-agent" ] || { echo "--release must contain venv/bin/veotrex-edge-agent" >&2; exit 64; }
[ -n "$WORK" ] || { echo "--work required" >&2; exit 64; }
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
UNIT_SRC=$SCRIPT_DIR/veotrex-edge.service
UNIT=veotrex-edge-fi.service
STATE=veotrex-edge-fi
mkdir -p "$WORK"; WORK=$(cd "$WORK" && pwd)

PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); printf 'PASS  %s\n' "$*"; }
bad()  { FAIL=$((FAIL+1)); printf 'FAIL  %s\n' "$*"; }
check(){ if eval "$2"; then ok "$1"; else bad "$1 [$2]"; fi; }
prop() { systemctl --user show -p "$1" --value "$UNIT"; }
wait_prop() { # name value timeout
  local i=0; while [ $i -lt "$3" ]; do [ "$(prop "$1")" = "$2" ] && return 0; sleep 1; i=$((i+1)); done; return 1
}
wait_status_prefix() { # prefix timeout
  local i=0; while [ $i -lt "$2" ]; do case "$(prop StatusText)" in "$1"*) return 0;; esac; sleep 1; i=$((i+1)); done; return 1
}
journal_count() { journalctl --user -u "$UNIT" --since "$1" --no-pager -o cat 2>/dev/null | grep -c -- "$2" || true; }
worker_pids() { pgrep -u "$(id -u)" -f 'gpu_worker/worker.py --fd' || true; }
cleanup() {
  systemctl --user stop "$UNIT" 2>/dev/null || true
  systemctl --user reset-failed "$UNIT" 2>/dev/null || true
  systemctl --user disable "$UNIT" 2>/dev/null || true
  rm -f "$HOME/.config/systemd/user/$UNIT" 2>/dev/null || true
  systemctl --user daemon-reload
}
trap cleanup EXIT

# ---------------------------------------------------------------- derive the user-scope unit
cat >"$WORK/release.env" <<EOF
VEOTREX_EDGE_SOURCE_COMMIT=$(git -C "$SCRIPT_DIR" rev-parse HEAD 2>/dev/null || echo unknown)
VEOTREX_EDGE_VERSION=0.1.0+fi
EOF
cat >"$WORK/edge.env" <<EOF
VEOTREX_EDGE_NODE_ID=$(cat /proc/sys/kernel/random/uuid)
VEOTREX_EDGE_ENVIRONMENT=failure-injection
VEOTREX_EDGE_LOG_LEVEL=INFO
VEOTREX_EDGE_HEARTBEAT_INTERVAL_SECONDS=5
EOF
printf 'VEOTREX_EDGE_ENVIRONMENT=failure-injection\n' >"$WORK/edge-missing-node.env"

derive_unit() { # env-file -> stdout
  sed -E \
    -e '/^(User|Group|SupplementaryGroups|CapabilityBoundingSet|AmbientCapabilities|ProtectKernelModules|ProtectClock|ProtectKernelLogs)=/d' \
    -e "s#^WorkingDirectory=.*#WorkingDirectory=%S/$STATE#" \
    -e "s#^StateDirectory=.*#StateDirectory=$STATE#" \
    -e "s#^EnvironmentFile=/opt/veotrex-edge/current/release.env#EnvironmentFile=$WORK/release.env#" \
    -e "s#^EnvironmentFile=/etc/veotrex-edge/edge.env#EnvironmentFile=$1#" \
    -e "s#^ExecStart=.*#ExecStart=$RELEASE/venv/bin/veotrex-edge-agent#" \
    -e 's#^WantedBy=.*#WantedBy=default.target#' \
    "$UNIT_SRC"
}
install_unit() {
  mkdir -p "$HOME/.config/systemd/user"
  derive_unit "$1" >"$HOME/.config/systemd/user/$UNIT"
  systemctl --user daemon-reload
}
install_unit "$WORK/edge.env"
echo "== derived unit: $HOME/.config/systemd/user/$UNIT (from $UNIT_SRC)"
systemd-analyze --user verify "$HOME/.config/systemd/user/$UNIT" && ok "systemd-analyze --user verify" || bad "verify"

# ------------------------------------------------------------------------------- scenarios
T0=$(date '+%Y-%m-%d %H:%M:%S')
echo "== A. normal start"
systemctl --user start "$UNIT"
check "A1 active after start" 'wait_prop ActiveState active 60'
check "A2 READY=1 accepted (Type=notify)" 'wait_status_prefix ready 60'
echo "     status: $(prop StatusText)"
check "A3 GPU worker READY in status" '[ -n "$(prop StatusText | grep -o "worker=READY")" ]'
check "A4 exactly one worker child" '[ "$(worker_pids | wc -l)" -eq 1 ]'
MAIN=$(prop MainPID); check "A5 MainPID is the agent" '[ "$MAIN" -gt 0 ] && grep -q veotrex-edge-agent /proc/$MAIN/cmdline'

echo "== B. normal stop"
systemctl --user stop "$UNIT"
check "B1 inactive after stop" 'wait_prop ActiveState inactive 30'
check "B2 clean exit code 0" '[ "$(prop ExecMainStatus)" = 0 ]'
check "B3 no worker left behind" '[ -z "$(worker_pids)" ]'
check "B4 journal shows edge_agent_stopped" '[ "$(journal_count "$T0" edge_agent_stopped)" -ge 1 ]'

echo "== C. normal restart"
systemctl --user start "$UNIT"; wait_prop ActiveState active 60 || true; P1=$(prop MainPID)
systemctl --user restart "$UNIT"
check "C1 active after restart" 'wait_prop ActiveState active 60 && wait_status_prefix ready 60'
check "C2 MainPID changed" '[ "$(prop MainPID)" != "$P1" ]'
check "C3 still exactly one worker" '[ "$(worker_pids | wc -l)" -eq 1 ]'

echo "== D. crash -> systemd restarts"
NR0=$(prop NRestarts); kill -SEGV "$(prop MainPID)"
check "D1 restart scheduled (auto-restart or activating)" 'sleep 2; case "$(prop SubState)" in auto-restart|start|running|auto-restart-queued) true;; *) false;; esac'
check "D2 active again within RestartSec+startup" 'wait_prop ActiveState active 60 && wait_status_prefix ready 60'
check "D3 NRestarts incremented" '[ "$(prop NRestarts)" -gt "$NR0" ]'
check "D4 old worker gone, new worker present" '[ "$(worker_pids | wc -l)" -eq 1 ]'

echo "== J. SIGTERM exits cleanly, no orphans"
TJ=$(date '+%Y-%m-%d %H:%M:%S'); kill -TERM "$(prop MainPID)"
check "J1 inactive (clean exit is not restarted by on-failure)" 'wait_prop ActiveState inactive 40'
check "J2 exit code 0" '[ "$(prop ExecMainStatus)" = 0 ]'
check "J3 no orphan worker" 'sleep 1; [ -z "$(worker_pids)" ]'
check "J4 STOPPING notified and stopped logged" '[ "$(journal_count "$TJ" edge_agent_stopped)" -ge 1 ]'

echo "== I. child worker dies -> agent recovers it"
systemctl --user start "$UNIT"; wait_prop ActiveState active 60 || true; wait_status_prefix ready 60 || true
MAIN=$(prop MainPID); W=$(worker_pids | head -1); TI=$(date '+%Y-%m-%d %H:%M:%S')
kill -KILL "$W"
check "I1 agent MainPID unchanged" 'sleep 8; [ "$(prop MainPID)" = "$MAIN" ]'
check "I2 gpu_worker_health_failed logged" '[ "$(journal_count "$TI" gpu_worker_health_failed)" -ge 1 ]'
check "I3 worker recovered (new pid, status READY)" 'wait_status_prefix "ready" 30 && [ "$(worker_pids | head -1)" != "$W" ] && [ "$(worker_pids | wc -l)" -eq 1 ]'
echo "     status: $(prop StatusText)"
systemctl --user stop "$UNIT"; wait_prop ActiveState inactive 30 || true

echo "== G. missing config -> deterministic EX_CONFIG, no restart loop"
install_unit "$WORK/edge-missing-node.env"; TG=$(date '+%Y-%m-%d %H:%M:%S')
systemctl --user start "$UNIT" 2>/dev/null || true
check "G1 unit failed" 'wait_prop ActiveState failed 30'
check "G2 exit status 78 (EX_CONFIG)" '[ "$(prop ExecMainStatus)" = 78 ]'
check "G3 not restarted (RestartPreventExitStatus)" 'sleep 12; [ "$(prop NRestarts)" = 0 ] && [ "$(prop ActiveState)" = failed ]'
check "G4 edge_config_invalid names the field" '[ "$(journal_count "$TG" "\"node_id\"")" -ge 1 ]'
check "G5 no value leaked" '[ "$(journal_count "$TG" VEOTREX_EDGE_NODE_ID=)" -eq 0 ]'
systemctl --user reset-failed "$UNIT"
echo "== G'. missing environment file"
install_unit "$WORK/does-not-exist.env"; systemctl --user start "$UNIT" 2>/dev/null || true
check "G6 fails to start without the config file" 'wait_prop ActiveState failed 30 || [ "$(prop ActiveState)" != active ]'
echo "     result: $(prop Result) substate: $(prop SubState)"
systemctl --user stop "$UNIT" 2>/dev/null || true; systemctl --user reset-failed "$UNIT" 2>/dev/null || true
install_unit "$WORK/edge.env"

echo "== H. local GPU runtime unavailable -> degraded, not failed"
# A user manager cannot hide device nodes (InaccessiblePaths=/DevicePolicy= need root), so the
# worker launch is made to fail instead: denying pipe2 makes Popen raise, the same path a
# missing/unusable GPU runtime takes (WorkerFailure -> gpu_worker_start_failed -> degraded).
mkdir -p "$HOME/.config/systemd/user/$UNIT.d"
printf '[Service]\nSystemCallFilter=~pipe2 pipe\nSystemCallErrorNumber=EPERM\n' >"$HOME/.config/systemd/user/$UNIT.d/no-gpu.conf"
systemctl --user daemon-reload; TH=$(date '+%Y-%m-%d %H:%M:%S'); systemctl --user start "$UNIT"
check "H1 active" 'wait_prop ActiveState active 60'
check "H2 READY with degraded status" 'wait_status_prefix degraded 60'
echo "     status: $(prop StatusText)"
check "H3 gpu_worker_start_failed logged, agent alive" '[ "$(journal_count "$TH" gpu_worker_start_failed)" -ge 1 ] && [ "$(prop ActiveState)" = active ]'
systemctl --user stop "$UNIT"; rm -rf "$HOME/.config/systemd/user/$UNIT.d"; systemctl --user daemon-reload

echo "== K. hardening: cannot write outside its state directory"
HARD_ARGS=(-p "StateDirectory=$STATE")
while IFS= read -r line; do HARD_ARGS+=(-p "$line"); done < <(sed -nE '/^(NoNewPrivileges|PrivateTmp|ProtectSystem|ProtectHome|ProtectKernelTunables|ProtectControlGroups|ProtectHostname|ProtectProc|ProcSubset|RestrictSUIDSGID|RestrictRealtime|RestrictNamespaces|LockPersonality|SystemCallArchitectures|RestrictAddressFamilies|UMask|RemoveIPC|SystemCallFilter|SystemCallErrorNumber)=/p' "$UNIT_SRC")
run_hard() { systemd-run --user --quiet --wait --collect "${HARD_ARGS[@]}" /bin/sh -c "$1" >/dev/null 2>&1; }
check "K0 sandbox positive control (a command runs under all ${#HARD_ARGS[@]} properties)" 'run_hard "true"'
check "K1 /usr read-only"  '! run_hard "touch /usr/zz-veotrex-probe"'
check "K2 /etc read-only"  '! run_hard "touch /etc/zz-veotrex-probe"'
check "K3 /opt read-only"  '! run_hard "touch /opt/zz-veotrex-probe"'
check "K4 state directory writable" 'run_hard "touch \$STATE_DIRECTORY/probe && rm \$STATE_DIRECTORY/probe"'
# ProtectHome= and PrivateTmp= are silently inert in a user manager (they need the system
# instance's mount privileges), so they can only be proven on the installed system unit.
echo "SKIP  K5 ProtectHome=yes (system scope only; inert in a user manager)"
echo "SKIP  K6 PrivateTmp=yes (system scope only; inert in a user manager)"

echo "== E. repeated failure -> start limit"
systemctl --user reset-failed "$UNIT" 2>/dev/null || true
systemctl --user start "$UNIT"; wait_prop ActiveState active 60 || true
BURST=$(sed -n 's/^StartLimitBurst=//p' "$UNIT_SRC"); RSEC=$(sed -n 's/^RestartSec=//p' "$UNIT_SRC" | tr -d s)
n=0
while [ $n -lt $((BURST + 1)) ]; do
  wait_prop ActiveState active 60 || break
  kill -KILL "$(prop MainPID)" 2>/dev/null || true; n=$((n+1)); sleep $((RSEC + 3))
done
# The system manager records Result=start-limit-hit; the user manager keeps the last
# failure's result and logs the refusal instead. Accept either form of the evidence.
check "E1 start limit hit after $BURST crashes" 'wait_prop Result start-limit-hit 30 || [ "$(journal_count "$T0" "Start request repeated too quickly")" -ge 1 ]'
check "E2 unit failed, not restarting" '[ "$(prop ActiveState)" = failed ] && [ "$(prop SubState)" = failed ]'
check "E3 no worker left" '[ -z "$(worker_pids)" ]'
echo "     NRestarts=$(prop NRestarts) Result=$(prop Result)"
systemctl --user reset-failed "$UNIT"

echo "== F. enablement (user scope)"
systemctl --user enable "$UNIT" >/dev/null 2>&1 && check "F1 enable creates the WantedBy link" '[ -L "$HOME/.config/systemd/user/default.target.wants/$UNIT" ]' || bad "F1 enable"
systemctl --user disable "$UNIT" >/dev/null 2>&1 || true

if [ "$SOAK" -gt 0 ]; then
  echo "== soak: ${SOAK}s (samples every 30 s)"
  systemctl --user start "$UNIT"; wait_prop ActiveState active 60 || true; wait_status_prefix ready 60 || true
  TS=$(date '+%Y-%m-%d %H:%M:%S'); D0=$(df --output=used -k / | tail -1); J0=$(journalctl --disk-usage 2>/dev/null | grep -oE '[0-9.]+[MG]' | head -1)
  printf '%-9s %-7s %-9s %-9s %-6s %-6s %-6s %-9s %-8s %-8s\n' elapsed pids agent_rss worker_rss a_fds w_fds tasks cpu_s temp_C mem_avail
  start=$(date +%s)
  while [ $(( $(date +%s) - start )) -lt "$SOAK" ]; do
    A=$(prop MainPID); W=$(worker_pids | head -1)
    ar=$(awk '/VmRSS/{print $2/1024}' /proc/$A/status 2>/dev/null || echo -); wr=$(awk '/VmRSS/{print $2/1024}' /proc/$W/status 2>/dev/null || echo -)
    af=$(ls /proc/$A/fd 2>/dev/null | wc -l); wf=$(ls /proc/$W/fd 2>/dev/null | wc -l)
    tasks=$(prop TasksCurrent); cpu=$(( $(prop CPUUsageNSec) / 1000000000 ))
    temp=$(( $(cat /sys/devices/virtual/thermal/thermal_zone0/temp) / 1000 )); ma=$(awk '/MemAvailable/{print int($2/1024)"M"}' /proc/meminfo)
    printf '%-9s %-7s %-9s %-9s %-6s %-6s %-6s %-9s %-8s %-8s\n' "$(( $(date +%s) - start ))s" "$(( $(pgrep -u $(id -u) -f "veotrex-edge-agent|gpu_worker/worker.py" | wc -l) ))" "${ar}M" "${wr}M" "$af" "$wf" "$tasks" "$cpu" "$temp" "$ma"
    sleep 30
  done
  D1=$(df --output=used -k / | tail -1)
  echo "soak: restarts=$(prop NRestarts) journal_errors=$(journalctl --user -u "$UNIT" --since "$TS" -p err --no-pager -o cat | wc -l) disk_delta_kB=$((D1 - D0)) journal_before=$J0 journal_after=$(journalctl --disk-usage 2>/dev/null | grep -oE '[0-9.]+[MG]' | head -1)"
  check "S1 no restart during soak" '[ "$(prop NRestarts)" = 0 ]'
  systemctl --user stop "$UNIT"
fi

echo "== summary: PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
