#!/usr/bin/env bash
# Install Clouds Guacamole into the running Omarchy shell.
#
#   ./install.sh             copy this checkout into ~/.config/omarchy/plugins
#   ./install.sh --link      symlink it instead (for development)
#   ./install.sh --uninstall stop the service, disable the widget, remove what this script installed
#   --no-restart             on an update, don't restart the shell and guacd to load the new code
#   --no-enable              install without adding the widget to the bar
#
# The recommended route is `omarchy plugin add <git url> --enable`; this script is for local
# checkouts. It only ever removes or replaces what belongs to Guacamole: a symlink to a
# Guacamole checkout, or a copy it made (marked with .guacamole-install).
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ID="chickymonkeys.guacamole"
PLUGIN_DIR="$HOME/.config/omarchy/plugins/$PLUGIN_ID"
CLI_LINK="$HOME/.local/bin/guac"
MARKER=".guacamole-install"
COPIED=(manifest.json ui bin lib assets LICENSE README.md)
MODE="copy"
ENABLE=true
RESTART=true
UPDATE=false

for arg in "$@"; do
  case "$arg" in
    --copy) MODE="copy" ;;
    --link) MODE="link" ;;
    --no-enable) ENABLE=false ;;
    --no-restart) RESTART=false ;;
    --uninstall) MODE="uninstall" ;;
    -h|--help)
      sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "Unknown option: $arg" >&2; exit 1 ;;
  esac
done

say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m==>\033[0m %s\n' "$*" >&2; }

is_ours() {
  # A symlink to a checkout with our manifest, or a copy this script made
  if [[ -L $PLUGIN_DIR ]]; then
    local target
    target="$(readlink -f "$PLUGIN_DIR")" || return 1
    grep -q "\"id\": \"$PLUGIN_ID\"" "$target/manifest.json" 2>/dev/null
  else
    [[ -f $PLUGIN_DIR/$MARKER ]]
  fi
}

reload_shell() {
  command -v omarchy-shell >/dev/null 2>&1 && omarchy-shell shell rescanPlugins >/dev/null 2>&1 || true
}

stop_daemon() {
  if [[ -x $SRC/bin/guac ]]; then
    python3 "$SRC/bin/guac" stop >/dev/null 2>&1 || true
  fi
}

# Files still uploading or folders syncing in the running guacd (0 when it isn't running)
daemon_busy() {
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$SRC/lib" python3 - <<'PY' 2>/dev/null || echo 0
from guac import client
if not client.is_running():
    print(0)
else:
    conn = client.connect(autostart=False)
    s = conn.first_state()["summary"]
    print(s["uploads"] + s["syncing"])
PY
}

# The shell loads a changed widget only when it restarts (its plugin reload keeps cached
# QML), and guacd runs the Python it started with
restart_after_update() {
  $UPDATE && $RESTART || return 0
  if [[ $(daemon_busy) == 0 ]]; then
    stop_daemon
  else
    warn "guacd is uploading or syncing, so it keeps running the old version. Later: guac stop"
  fi
  if command -v omarchy-restart-shell >/dev/null 2>&1; then
    say "Restarting the shell to load the new widget (guacd starts again with it)"
    omarchy-restart-shell >/dev/null 2>&1 || warn "Could not restart the shell: omarchy restart shell"
  else
    warn "Restart the shell to load the new widget"
  fi
}

if [[ $MODE == "uninstall" ]]; then
  stop_daemon
  if command -v omarchy >/dev/null 2>&1; then
    omarchy plugin disable "$PLUGIN_ID" >/dev/null 2>&1 || true
  fi
  if [[ -e $PLUGIN_DIR || -L $PLUGIN_DIR ]]; then
    if is_ours; then
      if [[ -L $PLUGIN_DIR ]]; then rm "$PLUGIN_DIR"; else rm -rf "$PLUGIN_DIR"; fi
      say "Removed $PLUGIN_DIR"
    else
      warn "Left $PLUGIN_DIR alone: it wasn't installed by this script (use: omarchy plugin remove $PLUGIN_ID)"
    fi
  fi
  if [[ -L $CLI_LINK && "$(readlink -f "$CLI_LINK")" == */bin/guac ]]; then
    rm "$CLI_LINK"
    say "Removed $CLI_LINK"
  fi
  reload_shell
  say "Uninstalled. Your settings (~/.config/guacamole), rclone accounts and local folders were kept."
  exit 0
fi

command -v rclone >/dev/null 2>&1 || warn "rclone is not installed: sudo pacman -S rclone"
command -v fusermount3 >/dev/null 2>&1 || warn "fusermount3 is missing: sudo pacman -S fuse3"

mkdir -p "$(dirname "$PLUGIN_DIR")"
if [[ -e $PLUGIN_DIR || -L $PLUGIN_DIR ]]; then
  UPDATE=true
  if [[ $MODE == "link" && -L $PLUGIN_DIR && "$(readlink -f "$PLUGIN_DIR")" == "$SRC" ]]; then
    say "Already linked to this checkout"
  elif is_ours; then
    if [[ -L $PLUGIN_DIR ]]; then rm "$PLUGIN_DIR"; else rm -rf "$PLUGIN_DIR"; fi
  else
    warn "$PLUGIN_DIR exists and wasn't installed by this script (a git clone from 'omarchy plugin add'?)."
    warn "Update it with: omarchy plugin update $PLUGIN_ID"
    exit 1
  fi
fi

if [[ ! -e $PLUGIN_DIR ]]; then
  if [[ $MODE == "link" ]]; then
    ln -s "$SRC" "$PLUGIN_DIR"
    say "Linked $PLUGIN_DIR -> $SRC"
  else
    mkdir -p "$PLUGIN_DIR"
    for entry in "${COPIED[@]}"; do
      [[ -e $SRC/$entry ]] && cp -a "$SRC/$entry" "$PLUGIN_DIR/"
    done
    find "$PLUGIN_DIR" -name __pycache__ -type d -prune -exec rm -rf {} +
    touch "$PLUGIN_DIR/$MARKER"
    say "Copied to $PLUGIN_DIR"
  fi
fi

mkdir -p "$(dirname "$CLI_LINK")"
if [[ ! -e $CLI_LINK || ( -L $CLI_LINK && "$(readlink -f "$CLI_LINK")" == */bin/guac ) ]]; then
  ln -sfn "$PLUGIN_DIR/bin/guac" "$CLI_LINK"
  say "Linked $CLI_LINK"
else
  warn "Left $CLI_LINK alone (it isn't ours)"
fi

reload_shell
if $ENABLE && command -v omarchy >/dev/null 2>&1; then
  # The shell discovers the plugin asynchronously after the rescan: give it a moment
  enabled=false
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    if omarchy plugin enable "$PLUGIN_ID" >/dev/null 2>&1; then enabled=true; break; fi
    sleep 0.5
  done
  if $enabled; then
    say "Enabled the Clouds widget in the bar"
  else
    warn "Could not enable the widget automatically: omarchy plugin enable $PLUGIN_ID"
  fi
fi
restart_after_update
say "Done. Click the cloud in the bar, or run: guac status"
