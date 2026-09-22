#!/usr/bin/env bash
# veotrex-edge-ctl.sh - install, build, activate, roll back and inspect the VeoTrex edge agent
# service on a Jetson (V1-00B, lifecycle corrected in V1-00B-R1). Source of truth for the
# production supervision workflow.
#
#   preflight            read-only host checks (any user)
#   install              root: converge service identity, directories, config (created once,
#                        never overwritten) and the unit; daemon-reload. Never starts and
#                        never enables: a unit whose ExecStart target does not exist yet must
#                        not be wired into boot. Safe to re-run on any partial state.
#   build [<ref>]        operator: detached non-editable release of the checked-out commit
#                        into /opt/veotrex-edge/releases/<sha> (refuses a dirty tracked tree)
#   activate <sha> [--restart]
#                        root: atomically point /opt/veotrex-edge/current at a release, run
#                        the strict systemd-analyze verify, daemon-reload, ENABLE for boot;
#                        with --restart also restart and check health. A failed verify
#                        restores the previous current (or removes it) and leaves boot
#                        enablement untouched.
#   rollback [<sha>]     root: activate the previous (or given) release and restart
#   update [<ref>]       operator: fast-forward-only pull, then build; prints the activate step
#   status               any user: release, unit state, status line, recent journal
#
# Nothing here hard-resets the checkout, installs the package editable, uses containers, or
# wraps the agent in a shell. The service user never owns code; the operator never runs the
# agent as root.
#
# VEOTREX_EDGE_ROOT (default empty) prefixes every host path and VEOTREX_EDGE_CTL_TEST_MODE=1
# skips ownership changes and the root requirement. Both exist ONLY for the unprivileged
# lifecycle tests in services/edge-agent/tests, which run this script against a temporary
# prefix with stub systemctl/useradd commands on PATH. Test mode refuses to run as root.
set -euo pipefail

PREFIX=${VEOTREX_EDGE_ROOT:-}
TEST_MODE=${VEOTREX_EDGE_CTL_TEST_MODE:-0}
ROOT_DIR=$PREFIX/opt/veotrex-edge
RELEASES_DIR=$ROOT_DIR/releases
CURRENT_LINK=$ROOT_DIR/current
PREVIOUS_LINK=$ROOT_DIR/previous
CONF_DIR=$PREFIX/etc/veotrex-edge
CONF_FILE=$CONF_DIR/edge.env
STATE_DIR=$PREFIX/var/lib/veotrex-edge
UNIT_NAME=veotrex-edge.service
UNIT_DST=$PREFIX/etc/systemd/system/$UNIT_NAME
SERVICE_USER=veotrex-edge
DEVICE_GROUPS=video,render
PACKAGE=veotrex-edge-agent
EXEC_TARGET=$CURRENT_LINK/venv/bin/$PACKAGE
# The path exactly as the unit file states it (no test prefix): what systemd-analyze reports.
UNIT_EXEC=/opt/veotrex-edge/current/venv/bin/$PACKAGE
HEALTH_TIMEOUT=${VEOTREX_EDGE_HEALTH_TIMEOUT:-90}
HEALTH_POLL=${VEOTREX_EDGE_HEALTH_POLL:-3}

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_DIR=$(cd "$SCRIPT_DIR/../.." && pwd)
UNIT_SRC=$SCRIPT_DIR/veotrex-edge.service
ENV_TEMPLATE=$SCRIPT_DIR/edge.env.example

log() { printf '%s\n' "veotrex-edge-ctl: $*"; }
die() { printf '%s\n' "veotrex-edge-ctl: ERROR: $*" >&2; exit 1; }
is_root() { [ "$(id -u)" -eq 0 ]; }
require_root() {
  if [ "$TEST_MODE" = 1 ]; then
    is_root && die "VEOTREX_EDGE_CTL_TEST_MODE is for unprivileged tests only, never root"
    return 0
  fi
  is_root || die "'$1' must run as root (sudo)"
}
require_operator() { is_root && die "'$1' must run as the operator, not root"; return 0; }

# Ownership is applied only when we actually are root; the unprivileged tests exercise the
# same code path with modes only.
install_dir() { # mode owner group path
  if is_root; then install -d -m "$1" -o "$2" -g "$3" "$4"; else install -d -m "$1" "$4"; fi
}
install_file() { # mode owner group src dst
  if is_root; then install -m "$1" -o "$2" -g "$3" "$4" "$5"; else install -m "$1" "$4" "$5"; fi
}
own() { # owner group path
  if is_root; then chown "$1:$2" "$3"; fi
}

find_uv() {
  if [ -n "${UV_BIN:-}" ] && [ -x "$UV_BIN" ]; then printf '%s' "$UV_BIN"; return; fi
  if command -v uv >/dev/null 2>&1; then command -v uv; return; fi
  [ -x "$HOME/.local/bin/uv" ] && { printf '%s' "$HOME/.local/bin/uv"; return; }
  die "uv not found; install it for the operator account or set UV_BIN"
}

# A path inside a directory this user cannot search is neither "present" nor "absent": say so
# instead of guessing, and never weaken the directory's mode to find out.
describe_path() { # path -> one line
  local path=$1 parent
  parent=$(dirname "$path")
  if [ -e "$path" ] || [ -L "$path" ]; then
    stat -c '%A %U:%G %n' "$path" 2>/dev/null || printf 'present (metadata not readable by %s) %s' "$(id -un)" "$path"
  elif [ -d "$parent" ] && [ ! -x "$parent" ]; then
    printf 'protected (presence not observable as %s; %s is not searchable)' "$(id -un)" "$parent"
  elif [ -d "$parent" ]; then
    printf 'absent'
  else
    printf 'absent (%s does not exist)' "$parent"
  fi
}

unit_enabled_state() { systemctl is-enabled "$UNIT_NAME" 2>/dev/null || true; }
unit_active_state() { systemctl is-active "$UNIT_NAME" 2>/dev/null || true; }

# ------------------------------------------------------------------------------ preflight
cmd_preflight() {
  local ok=0
  log "architecture: $(uname -m)"; [ "$(uname -m)" = aarch64 ] || { log "  not aarch64"; ok=1; }
  log "os: $(. /etc/os-release && echo "$PRETTY_NAME")"
  [ -r /etc/nv_tegra_release ] && log "l4t: $(head -1 /etc/nv_tegra_release)"
  [ -r /proc/device-tree/model ] && log "model: $(tr -d '\0' </proc/device-tree/model)"
  log "systemd: $(systemctl --version | head -1)"
  [ -x /usr/bin/python3.12 ] && log "system python: /usr/bin/python3.12" || { log "  /usr/bin/python3.12 missing (GPU worker requires it)"; ok=1; }
  if /usr/bin/python3 -c 'import tensorrt' 2>/dev/null; then
    log "tensorrt (system python): $(/usr/bin/python3 -c 'import tensorrt;print(tensorrt.__version__)')"
  else
    log "  tensorrt not importable by /usr/bin/python3: agent will run DEGRADED"
  fi
  for node in /dev/nvmap /dev/nvgpu/igpu0/ctrl /dev/dri/renderD128; do
    [ -e "$node" ] && log "device: $(stat -c '%A %U:%G %n' "$node")" || log "  device $node absent"
  done
  log "service user: $(id "$SERVICE_USER" 2>/dev/null || echo 'not created')"
  log "unit: $(describe_path "$UNIT_DST")"
  log "enabled: $(unit_enabled_state)"
  log "active: $(unit_active_state)"
  log "current release: $(readlink "$CURRENT_LINK" 2>/dev/null || describe_path "$CURRENT_LINK")"
  log "config: $(describe_path "$CONF_FILE")"
  log "state dir: $(describe_path "$STATE_DIR")"
  log "repo: $REPO_DIR @ $(git -C "$REPO_DIR" rev-parse --short HEAD 2>/dev/null || echo '?')"
  log "free on /opt: $(df -h /opt 2>/dev/null | awk 'NR==2{print $4}')  memory: $(free -h | awk '/^Mem/{print $7" available"}')"
  return $ok
}

# ------------------------------------------------------------------------- verification
# systemd-analyze verify is the one gate on the final service, and it is strict after a
# release is active. Before the first activation the ExecStart target does not exist by
# design, so exactly that finding, and nothing else, is tolerated.
verify_unit() { # strict | pre-release
  local mode=$1 output rc=0 findings
  output=$(systemd-analyze verify "$UNIT_DST" 2>&1) || rc=$?
  findings=$(printf '%s\n' "$output" | grep -F "$UNIT_NAME:" || true)
  if [ "$mode" = pre-release ] && [ ! -e "$CURRENT_LINK" ]; then
    findings=$(printf '%s\n' "$findings" | grep -vF "Command $UNIT_EXEC is not executable" || true)
    if [ -z "$findings" ]; then
      log "unit verified (pre-release: ExecStart target $UNIT_EXEC is intentionally absent until activate)"
      return 0
    fi
  fi
  if [ -n "$findings" ]; then
    printf '%s\n' "$findings" >&2
    return 1
  fi
  if [ "$mode" = strict ] && [ "$rc" -ne 0 ]; then
    printf '%s\n' "$output" >&2
    return 1
  fi
  log "unit verified ($mode)"
}

# -------------------------------------------------------------------------------- install
validate_config() {
  local file=$1 node
  grep -q '^VEOTREX_EDGE_NODE_ID=' "$file" || die "$file has no VEOTREX_EDGE_NODE_ID"
  node=$(sed -n 's/^VEOTREX_EDGE_NODE_ID=//p' "$file" | head -1)
  case "$node" in
    00000000-0000-0000-0000-000000000000|"") die "$file: VEOTREX_EDGE_NODE_ID is the placeholder";;
  esac
  [[ "$node" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]] \
    || die "$file: VEOTREX_EDGE_NODE_ID is not a UUID"
  if grep -qiE '^[A-Z_]*(TOKEN|SECRET|PASSWORD|DSN|KEY)[A-Z_]*=' "$file"; then
    die "$file: contains a secret-looking key; secrets are delivered with LoadCredential=, never here"
  fi
}

cmd_install() {
  require_root install
  local operator=${SUDO_USER:-}
  while [ $# -gt 0 ]; do
    case "$1" in
      --operator) operator=$2; shift;;
      --start) die "install never starts the service; use 'activate <sha> --restart'";;
      *) die "install: unknown argument $1";;
    esac; shift
  done
  [ -n "$operator" ] && id "$operator" >/dev/null 2>&1 || die "install: --operator <user> required (owner of releases)"
  [ "$operator" != root ] || die "install: the operator must not be root"

  # Identity: create once, converge the attributes that matter every time.
  getent group "$SERVICE_USER" >/dev/null || groupadd --system "$SERVICE_USER"
  if id "$SERVICE_USER" >/dev/null 2>&1; then
    log "service user exists: $SERVICE_USER"
  else
    useradd --system --gid "$SERVICE_USER" --home-dir "$STATE_DIR" --no-create-home \
      --shell /usr/sbin/nologin --comment "VeoTrex edge agent" "$SERVICE_USER"
    log "created system user $SERVICE_USER"
  fi
  # -G sets the supplementary groups exactly: the measured device groups, nothing else, so a
  # stray sudo/docker membership is removed rather than tolerated.
  usermod -G "$DEVICE_GROUPS" "$SERVICE_USER"
  log "service identity: $(id "$SERVICE_USER")"

  # Directories: converge modes/owners; existing content is never touched.
  install_dir 0755 root root "$ROOT_DIR"
  install_dir 0755 "$operator" "$operator" "$RELEASES_DIR"
  install_dir 0750 root "$SERVICE_USER" "$CONF_DIR"
  install_dir 0750 "$SERVICE_USER" "$SERVICE_USER" "$STATE_DIR"

  # Config: generated exactly once. An existing file is preserved byte for byte, including
  # its node identity; only ownership and mode are re-asserted.
  if [ -e "$CONF_FILE" ]; then
    log "config preserved: $CONF_FILE (existing content and node id kept)"
  else
    local tmp; tmp=$(mktemp)
    sed "s/^VEOTREX_EDGE_NODE_ID=.*/VEOTREX_EDGE_NODE_ID=$(cat /proc/sys/kernel/random/uuid)/" \
      "$ENV_TEMPLATE" >"$tmp"
    install_file 0640 root "$SERVICE_USER" "$tmp" "$CONF_FILE"; rm -f "$tmp"
    log "config created: $CONF_FILE with a generated node id"
  fi
  own root "$SERVICE_USER" "$CONF_FILE"; chmod 0640 "$CONF_FILE"
  validate_config "$CONF_FILE"

  # Unit: converge to the repository source.
  [ -d "$(dirname "$UNIT_DST")" ] || install_dir 0755 root root "$(dirname "$UNIT_DST")"
  if [ -f "$UNIT_DST" ] && cmp -s "$UNIT_SRC" "$UNIT_DST"; then
    log "unit unchanged: $UNIT_DST"
  else
    install_file 0644 root root "$UNIT_SRC" "$UNIT_DST"
    log "unit installed: $UNIT_DST"
  fi
  verify_unit pre-release || die "installed unit failed verification"
  systemctl daemon-reload || die "daemon-reload failed; unit changes are on disk but not loaded"

  # Boot enablement is activate's job. Converge a wrong earlier state: enabled with nothing
  # to execute must not survive a reboot.
  local enabled; enabled=$(unit_enabled_state)
  if [ ! -e "$CURRENT_LINK" ] && [ "$enabled" = enabled ]; then
    systemctl disable "$UNIT_NAME" >/dev/null
    log "disabled $UNIT_NAME: it was enabled with no release to execute"
    enabled=disabled
  fi

  log "host infrastructure installed: account, directories, config, unit (daemon reloaded)"
  if [ -e "$CURRENT_LINK" ]; then
    log "current release: $(readlink "$CURRENT_LINK") (enabled: ${enabled:-unknown}, active: $(unit_active_state))"
  else
    log "release not yet activated: $CURRENT_LINK absent"
    log "service intentionally ${enabled:-disabled} and $(unit_active_state) until 'activate <sha>'"
    log "next: '$0 build' as $operator, then 'sudo $0 activate <sha> --restart'"
  fi
}

# ---------------------------------------------------------------------------------- build
cmd_build() {
  require_operator build
  local ref=${1:-} sha dest venv uv
  [ -d "$RELEASES_DIR" ] && [ -w "$RELEASES_DIR" ] || die "run 'sudo $0 install' first ($RELEASES_DIR not writable)"
  [ -z "$(git -C "$REPO_DIR" status --porcelain --untracked-files=no)" ] \
    || die "tracked working tree is not clean; a release must be an exact commit"
  sha=$(git -C "$REPO_DIR" rev-parse HEAD)
  if [ -n "$ref" ] && [ "$(git -C "$REPO_DIR" rev-parse "$ref^{commit}")" != "$sha" ]; then
    die "HEAD is $sha but $ref is not; check out the commit you want to build"
  fi
  dest=$RELEASES_DIR/$sha; venv=$dest/venv
  if [ -x "$venv/bin/$PACKAGE" ] && [ -f "$dest/release.env" ]; then
    log "release already built: $dest"; printf '%s\n' "$sha"; return
  fi
  uv=$(find_uv)
  rm -rf "$dest"; install -d -m 0755 "$dest"
  log "building $PACKAGE @ $sha into $venv"
  # Built AT its final path: uv writes absolute interpreter paths into console scripts.
  # --no-editable: nothing in the release points back into the working tree.
  # --package: a separate environment, so the shared developer venv is never touched.
  # --reinstall-package: uv keys its cache of a path dependency on pyproject.toml, so a source
  # edit alone can be served from a STALE cached wheel; force a rebuild of our own package.
  UV_PROJECT_ENVIRONMENT=$venv UV_PYTHON_DOWNLOADS=never UV_PYTHON=/usr/bin/python3.12 \
    "$uv" sync --directory "$REPO_DIR" --frozen --no-dev --no-editable --package "$PACKAGE" \
    --reinstall-package "$PACKAGE" \
    || { rm -rf "$dest"; die "uv sync failed"; }
  [ -x "$venv/bin/$PACKAGE" ] || { rm -rf "$dest"; die "console script missing after build"; }
  if ls "$venv"/lib/python3.*/site-packages/_editable_impl_* >/dev/null 2>&1; then
    rm -rf "$dest"; die "editable install detected in release"
  fi
  "$venv/bin/python" -c 'import veotrex_edge_agent.main' || { rm -rf "$dest"; die "import smoke failed"; }
  local version; version=$("$venv/bin/python" -c 'import importlib.metadata as m;print(m.version("veotrex-edge-agent"))')
  {
    printf 'VEOTREX_EDGE_SOURCE_COMMIT=%s\n' "$sha"
    printf 'VEOTREX_EDGE_VERSION=%s+%s\n' "$version" "${sha:0:12}"
    printf 'VEOTREX_EDGE_RELEASE_BUILT_AT=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } >"$dest/release.env"
  chmod -R a+rX,go-w "$dest"
  log "built $dest ($(du -sh "$venv" | cut -f1))"
  printf '%s\n' "$sha"
}

# ------------------------------------------------------------------------------- activate
verify_health() {
  local waited=0 state status
  while [ "$waited" -lt "$HEALTH_TIMEOUT" ]; do
    state=$(systemctl show -p ActiveState --value "$UNIT_NAME")
    status=$(systemctl show -p StatusText --value "$UNIT_NAME")
    case "$state:$status" in
      active:ready*) log "healthy: $status"; return 0;;
      active:degraded*) log "DEGRADED: $status"; return 2;;
      failed:*) break;;
    esac
    sleep "$HEALTH_POLL"; waited=$((waited + HEALTH_POLL))
  done
  log "NOT HEALTHY after ${waited}s: state=$(systemctl show -p ActiveState,SubState,Result,NRestarts --value "$UNIT_NAME" | paste -sd' ')"
  journalctl -u "$UNIT_NAME" -n 20 --no-pager -o cat || true
  [ -e "$PREVIOUS_LINK" ] && log "rollback: sudo $0 rollback"
  return 1
}

switch_current() { # target (empty = remove)
  if [ -z "$1" ]; then rm -f "$CURRENT_LINK"; return; fi
  # Atomic: a temporary symlink renamed over the old one, never delete-then-create.
  ln -sfn "$1" "$CURRENT_LINK.tmp"; mv -T "$CURRENT_LINK.tmp" "$CURRENT_LINK"
}

cmd_activate() {
  require_root activate
  local sha=${1:-} restart=no dest
  [ -n "$sha" ] || die "activate: <sha> required"
  shift
  while [ $# -gt 0 ]; do
    case "$1" in --restart) restart=yes;; --no-restart) restart=no;; *) die "activate: unknown argument $1";; esac; shift
  done
  dest=$RELEASES_DIR/$sha
  [ -d "$dest" ] || die "release $sha does not exist under $RELEASES_DIR (run build first)"
  [ -x "$dest/venv/bin/$PACKAGE" ] || die "release $sha has no executable agent (incomplete build)"
  [ -f "$dest/release.env" ] || die "release $sha has no release.env (incomplete build)"
  grep -q "^VEOTREX_EDGE_SOURCE_COMMIT=$sha\$" "$dest/release.env" || die "release.env does not name $sha"
  [ -f "$UNIT_DST" ] || die "unit not installed; run install first"
  [ -f "$CONF_FILE" ] || die "$CONF_FILE missing; run install first"
  validate_config "$CONF_FILE"

  local previous; previous=$(readlink "$CURRENT_LINK" 2>/dev/null || true)
  switch_current "$dest"
  log "current -> $dest"
  [ -x "$EXEC_TARGET" ] || { switch_current "$previous"; die "ExecStart target $EXEC_TARGET is not executable after switch; restored previous"; }
  if ! verify_unit strict; then
    # Never leave boot enablement pointing at code systemd refuses to load.
    switch_current "$previous"
    if [ -n "$previous" ]; then
      log "verification failed: restored current -> $previous (boot enablement unchanged)"
    else
      log "verification failed: removed $CURRENT_LINK (nothing was enabled)"
    fi
    die "release $sha not activated"
  fi
  if [ -n "$previous" ] && [ "$previous" != "$dest" ]; then
    ln -sfn "$previous" "$PREVIOUS_LINK"
  fi
  systemctl daemon-reload || die "daemon-reload failed after activation; run 'systemctl daemon-reload' and re-run activate"
  # Boot enablement happens here, and only here: the ExecStart target now exists and the
  # final unit passed the strict verify.
  systemctl enable "$UNIT_NAME" >/dev/null
  log "enabled for boot: $(unit_enabled_state)"
  if [ "$restart" = yes ]; then
    systemctl restart "$UNIT_NAME"
    verify_health
  else
    log "activated without restart; run 'sudo systemctl restart $UNIT_NAME' when ready"
  fi
}

cmd_rollback() {
  require_root rollback
  local sha=${1:-}
  if [ -z "$sha" ]; then
    [ -e "$PREVIOUS_LINK" ] || die "no previous release recorded"
    sha=$(basename "$(readlink "$PREVIOUS_LINK")")
  fi
  log "rolling back to $sha"
  cmd_activate "$sha" --restart
}

# --------------------------------------------------------------------------------- update
cmd_update() {
  require_operator update
  local ref=${1:-origin/main}
  git -C "$REPO_DIR" fetch --quiet origin
  git -C "$REPO_DIR" merge --ff-only "$ref"
  local sha; sha=$(cmd_build | tail -1)
  log "next: sudo $0 activate $sha --restart"
}

# --------------------------------------------------------------------------------- status
cmd_status() {
  log "current:  $(readlink "$CURRENT_LINK" 2>/dev/null || describe_path "$CURRENT_LINK")"
  log "previous: $(readlink "$PREVIOUS_LINK" 2>/dev/null || echo none)"
  log "config:   $(describe_path "$CONF_FILE")"
  log "unit:     $(describe_path "$UNIT_DST")"
  [ -r "$CURRENT_LINK/release.env" ] && sed 's/^/  /' "$CURRENT_LINK/release.env"
  systemctl show -p ActiveState,SubState,Result,UnitFileState,MainPID,NRestarts,ExecMainStartTimestamp,WatchdogUSec,StatusText \
    "$UNIT_NAME" 2>/dev/null | sed 's/^/  /'
  journalctl -u "$UNIT_NAME" -n 10 --no-pager -o cat 2>/dev/null | cut -c1-200 | sed 's/^/  /' || true
}

case "${1:-}" in
  preflight) shift; cmd_preflight "$@";;
  install)   shift; cmd_install "$@";;
  build)     shift; cmd_build "$@";;
  activate)  shift; cmd_activate "$@";;
  rollback)  shift; cmd_rollback "$@";;
  update)    shift; cmd_update "$@";;
  status)    shift; cmd_status "$@";;
  *) sed -n '2,26p' "$0"; exit 64;;
esac
