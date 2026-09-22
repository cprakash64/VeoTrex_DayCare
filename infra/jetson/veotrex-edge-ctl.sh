#!/usr/bin/env bash
# veotrex-edge-ctl.sh - install, build, activate, roll back and inspect the VeoTrex edge agent
# service on a Jetson (V1-00B). Source of truth for the production supervision workflow.
#
#   preflight            read-only host checks (any user)
#   install [--start]    root: service identity, directories, config (never overwritten),
#                        unit file, enable. Starts only with --start.
#   build [<ref>]        operator: detached non-editable release of the checked-out commit
#                        into /opt/veotrex-edge/releases/<sha> (refuses a dirty tracked tree)
#   activate <sha> [--restart]
#                        root: atomically point /opt/veotrex-edge/current at a release,
#                        verify the unit, reload; with --restart also restart and check health
#   rollback [<sha>]     root: activate the previous (or given) release and restart
#   update [<ref>]       operator: fast-forward-only pull, then build; prints the activate step
#   status               any user: release, unit state, status line, recent journal
#
# Nothing here hard-resets the checkout, installs the package editable, uses containers, or
# wraps the agent in a shell. The service user never owns code; the operator never runs the agent as root.
set -euo pipefail

ROOT_DIR=/opt/veotrex-edge
RELEASES_DIR=$ROOT_DIR/releases
CURRENT_LINK=$ROOT_DIR/current
PREVIOUS_LINK=$ROOT_DIR/previous
CONF_DIR=/etc/veotrex-edge
CONF_FILE=$CONF_DIR/edge.env
STATE_DIR=/var/lib/veotrex-edge
UNIT_NAME=veotrex-edge.service
UNIT_DST=/etc/systemd/system/$UNIT_NAME
SERVICE_USER=veotrex-edge
DEVICE_GROUPS=video,render
PACKAGE=veotrex-edge-agent
HEALTH_TIMEOUT=${VEOTREX_EDGE_HEALTH_TIMEOUT:-90}

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_DIR=$(cd "$SCRIPT_DIR/../.." && pwd)
UNIT_SRC=$SCRIPT_DIR/veotrex-edge.service
ENV_TEMPLATE=$SCRIPT_DIR/edge.env.example

log() { printf '%s\n' "veotrex-edge-ctl: $*"; }
die() { printf '%s\n' "veotrex-edge-ctl: ERROR: $*" >&2; exit 1; }
require_root() { [ "$(id -u)" -eq 0 ] || die "'$1' must run as root (sudo)"; }
require_operator() { [ "$(id -u)" -ne 0 ] || die "'$1' must run as the operator, not root"; }

find_uv() {
  if [ -n "${UV_BIN:-}" ] && [ -x "$UV_BIN" ]; then printf '%s' "$UV_BIN"; return; fi
  if command -v uv >/dev/null 2>&1; then command -v uv; return; fi
  [ -x "$HOME/.local/bin/uv" ] && { printf '%s' "$HOME/.local/bin/uv"; return; }
  die "uv not found; install it for the operator account or set UV_BIN"
}

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
  log "unit installed: $([ -f "$UNIT_DST" ] && echo yes || echo no)"
  log "enabled: $(systemctl is-enabled "$UNIT_NAME" 2>/dev/null || echo no)"
  log "active: $(systemctl is-active "$UNIT_NAME" 2>/dev/null || echo inactive)"
  log "current release: $(readlink "$CURRENT_LINK" 2>/dev/null || echo none)"
  log "config: $([ -f "$CONF_FILE" ] && stat -c '%A %U:%G %n' "$CONF_FILE" || echo 'not installed')"
  log "repo: $REPO_DIR @ $(git -C "$REPO_DIR" rev-parse --short HEAD 2>/dev/null || echo '?')"
  log "free on /opt: $(df -h /opt | awk 'NR==2{print $4}')  memory: $(free -h | awk '/^Mem/{print $7" available"}')"
  return $ok
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
  local start=no operator=${SUDO_USER:-}
  while [ $# -gt 0 ]; do
    case "$1" in
      --start) start=yes;;
      --operator) operator=$2; shift;;
      *) die "install: unknown argument $1";;
    esac; shift
  done
  [ -n "$operator" ] && id "$operator" >/dev/null 2>&1 || die "install: --operator <user> required (owner of releases)"
  [ "$operator" != root ] || die "install: the operator must not be root"

  getent group "$SERVICE_USER" >/dev/null || groupadd --system "$SERVICE_USER"
  if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --gid "$SERVICE_USER" --home-dir "$STATE_DIR" --no-create-home \
      --shell /usr/sbin/nologin --comment "VeoTrex edge agent" "$SERVICE_USER"
    log "created system user $SERVICE_USER"
  fi
  # -G sets the supplementary groups exactly: only the measured device groups, never more.
  usermod -G "$DEVICE_GROUPS" "$SERVICE_USER"
  log "service identity: $(id "$SERVICE_USER")"

  install -d -m 0755 -o root -g root "$ROOT_DIR"
  install -d -m 0755 -o "$operator" -g "$operator" "$RELEASES_DIR"
  install -d -m 0750 -o root -g "$SERVICE_USER" "$CONF_DIR"
  install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$STATE_DIR"
  if [ ! -e "$CONF_FILE" ]; then
    local tmp; tmp=$(mktemp)
    sed "s/^VEOTREX_EDGE_NODE_ID=.*/VEOTREX_EDGE_NODE_ID=$(cat /proc/sys/kernel/random/uuid)/" \
      "$ENV_TEMPLATE" >"$tmp"
    install -m 0640 -o root -g "$SERVICE_USER" "$tmp" "$CONF_FILE"; rm -f "$tmp"
    log "created $CONF_FILE with a generated node id"
  else
    log "kept existing $CONF_FILE"
  fi
  chown root:"$SERVICE_USER" "$CONF_FILE"; chmod 0640 "$CONF_FILE"
  validate_config "$CONF_FILE"

  if [ -f "$UNIT_DST" ] && cmp -s "$UNIT_SRC" "$UNIT_DST"; then
    log "unit unchanged: $UNIT_DST"
  else
    install -m 0644 -o root -g root "$UNIT_SRC" "$UNIT_DST"
    log "installed $UNIT_DST"
  fi
  systemd-analyze verify "$UNIT_DST"
  systemctl daemon-reload
  systemctl enable "$UNIT_NAME" >/dev/null
  log "enabled: $(systemctl is-enabled "$UNIT_NAME")"
  if [ "$start" = yes ]; then
    [ -e "$CURRENT_LINK" ] || die "no release activated yet: build, then activate <sha> --restart"
    systemctl restart "$UNIT_NAME"
    verify_health
  else
    log "not started. Next: '$0 build' as $operator, then 'sudo $0 activate <sha> --restart'"
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
    sleep 3; waited=$((waited + 3))
  done
  log "NOT HEALTHY after ${waited}s: state=$(systemctl show -p ActiveState,SubState,Result,NRestarts --value "$UNIT_NAME" | paste -sd' ')"
  journalctl -u "$UNIT_NAME" -n 20 --no-pager -o cat || true
  [ -e "$PREVIOUS_LINK" ] && log "rollback: sudo $0 rollback"
  return 1
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
  [ -x "$dest/venv/bin/$PACKAGE" ] || die "release $sha has no built agent (run build first)"
  grep -q "^VEOTREX_EDGE_SOURCE_COMMIT=$sha\$" "$dest/release.env" || die "release.env does not name $sha"
  [ -f "$UNIT_DST" ] || die "unit not installed; run install first"
  [ -f "$CONF_FILE" ] || die "$CONF_FILE missing; run install first"
  validate_config "$CONF_FILE"
  local current; current=$(readlink "$CURRENT_LINK" 2>/dev/null || true)
  if [ -n "$current" ] && [ "$current" != "$dest" ]; then
    ln -sfn "$current" "$PREVIOUS_LINK"
  fi
  # Atomic switch: a temporary symlink renamed over the old one, never a delete-then-create.
  ln -sfn "$dest" "$CURRENT_LINK.tmp"; mv -T "$CURRENT_LINK.tmp" "$CURRENT_LINK"
  log "current -> $dest"
  systemd-analyze verify "$UNIT_DST"
  systemctl daemon-reload
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
  log "current:  $(readlink "$CURRENT_LINK" 2>/dev/null || echo none)"
  log "previous: $(readlink "$PREVIOUS_LINK" 2>/dev/null || echo none)"
  [ -r "$CURRENT_LINK/release.env" ] && sed 's/^/  /' "$CURRENT_LINK/release.env"
  systemctl show -p ActiveState,SubState,Result,MainPID,NRestarts,ExecMainStartTimestamp,StatusText \
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
  *) sed -n '2,20p' "$0"; exit 64;;
esac
