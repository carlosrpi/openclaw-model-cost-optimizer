#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYSTEMD_USER_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
TIMER_NAME="openclaw-model-cost-optimizer.timer"
SERVICE_NAME="openclaw-model-cost-optimizer.service"
SERVICE_OUTPUT="$PROJECT_DIR/$SERVICE_NAME"
CONFIG_OUTPUT="$PROJECT_DIR/config.toml"
LEGACY_STATE_PATH="$PROJECT_DIR/openclaw-model-cost-optimizer.json"

PURGE_RUNTIME_FILES=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --purge)
      PURGE_RUNTIME_FILES=1
      shift
      ;;
    -h|--help)
      cat <<'EOF'
Usage: ./uninstall.sh [options]

Options:
  --purge                  Also remove config.toml and any legacy local state file.
  -h, --help               Show this help message.
EOF
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 1
      ;;
  esac
done

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

remove_path_if_present() {
  local target="$1"

  if [[ -e "$target" || -L "$target" ]]; then
    rm -f "$target"
    echo "Removed $target"
  fi
}

remove_unit_link_if_matching() {
  local target="$1"
  local expected="$2"

  if [[ -L "$target" ]]; then
    local resolved
    resolved="$(readlink "$target")"
    if [[ "$resolved" == "$expected" ]]; then
      rm -f "$target"
      echo "Removed symlink $target"
    else
      echo "Keeping $target because it points to $resolved instead of $expected"
    fi
  elif [[ -e "$target" ]]; then
    echo "Keeping $target because it is not a symlink created by this installer"
  fi
}

require_cmd systemctl

if systemctl --user list-unit-files "$TIMER_NAME" "$SERVICE_NAME" >/dev/null 2>&1; then
  systemctl --user disable --now "$TIMER_NAME" >/dev/null 2>&1 || true
  systemctl --user stop "$SERVICE_NAME" >/dev/null 2>&1 || true
fi

remove_unit_link_if_matching "$SYSTEMD_USER_DIR/$SERVICE_NAME" "$SERVICE_OUTPUT"
remove_unit_link_if_matching "$SYSTEMD_USER_DIR/$TIMER_NAME" "$PROJECT_DIR/$TIMER_NAME"

remove_path_if_present "$SERVICE_OUTPUT"

if [[ "$PURGE_RUNTIME_FILES" -eq 1 ]]; then
  remove_path_if_present "$CONFIG_OUTPUT"
  remove_path_if_present "$LEGACY_STATE_PATH"
else
  [[ -f "$CONFIG_OUTPUT" ]] && echo "Keeping $CONFIG_OUTPUT"
  [[ -f "$LEGACY_STATE_PATH" ]] && echo "Legacy file present but no longer used: $LEGACY_STATE_PATH"
fi

systemctl --user daemon-reload
systemctl --user reset-failed "$TIMER_NAME" "$SERVICE_NAME" >/dev/null 2>&1 || true

echo "Done."
