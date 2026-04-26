#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_OPENCLAW_BIN="$HOME/.npm-global/bin/openclaw"
OPENCLAW_BIN="${OPENCLAW_BIN:-$DEFAULT_OPENCLAW_BIN}"
OPENCLAW_CONFIG_PATH="${OPENCLAW_CONFIG_PATH:-$HOME/.openclaw/openclaw.json}"
SYSTEMD_USER_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
TIMER_NAME="openclaw-model-cost-optimizer.timer"
SERVICE_NAME="openclaw-model-cost-optimizer.service"

FORCE_CONFIG=0
NO_ENABLE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --force-config)
      FORCE_CONFIG=1
      shift
      ;;
    --no-enable)
      NO_ENABLE=1
      shift
      ;;
    --openclaw-bin)
      OPENCLAW_BIN="$2"
      shift 2
      ;;
    --openclaw-config)
      OPENCLAW_CONFIG_PATH="$2"
      shift 2
      ;;
    -h|--help)
      cat <<'EOF'
Usage: ./install.sh [options]

Options:
  --force-config           Re-render config.toml even if it already exists.
  --no-enable              Install files but do not enable/start the timer.
  --openclaw-bin PATH      Override the OpenClaw binary path.
  --openclaw-config PATH   Override the OpenClaw config path.
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

require_cmd python3
require_cmd systemctl

python3 - <<'PY'
import sys
if sys.version_info < (3, 11):
    raise SystemExit("Python 3.11+ is required because the project uses tomllib.")
PY

if [[ ! -x "$OPENCLAW_BIN" ]]; then
  if command -v openclaw >/dev/null 2>&1; then
    OPENCLAW_BIN="$(command -v openclaw)"
  else
    echo "OpenClaw binary not found. Expected: $OPENCLAW_BIN" >&2
    exit 1
  fi
fi

if [[ ! -f "$OPENCLAW_CONFIG_PATH" ]]; then
  echo "OpenClaw config file not found: $OPENCLAW_CONFIG_PATH" >&2
  exit 1
fi

echo "Checking OpenClaw prerequisites..."
STATUS_JSON="$("$OPENCLAW_BIN" status --json --usage)"

STATUS_JSON="$STATUS_JSON" python3 - "$OPENCLAW_CONFIG_PATH" <<'PY'
import json
import os
import sys
from pathlib import Path

status = json.loads(os.environ["STATUS_JSON"])
config_path = Path(sys.argv[1])
config = json.loads(config_path.read_text(encoding="utf-8"))

providers = {item.get("provider") for item in status.get("usage", {}).get("providers", [])}
if "openai-codex" not in providers:
    raise SystemExit(
        "This installer currently supports OpenClaw setups that expose the openai-codex usage provider.\n"
        "It looks like this machine is not currently configured for openai-codex OAuth usage snapshots."
    )

primary_model = (
    config.get("agents", {})
    .get("defaults", {})
    .get("model", {})
    .get("primary")
)
if not isinstance(primary_model, str) or not primary_model.startswith("openai-codex/"):
    raise SystemExit(
        "This installer currently supports OpenClaw setups using openai-codex as the primary model.\n"
        f"Detected primary model: {primary_model!r}"
    )
PY

mkdir -p "$SYSTEMD_USER_DIR"

CONFIG_TEMPLATE="$PROJECT_DIR/config.example.toml"
CONFIG_OUTPUT="$PROJECT_DIR/config.toml"
STATE_PATH="$PROJECT_DIR/openclaw-model-cost-optimizer.json"

if [[ ! -f "$CONFIG_OUTPUT" || "$FORCE_CONFIG" -eq 1 ]]; then
  python3 - "$CONFIG_TEMPLATE" "$CONFIG_OUTPUT" "$OPENCLAW_BIN" "$OPENCLAW_CONFIG_PATH" "$STATE_PATH" <<'PY'
import sys
from pathlib import Path

template_path = Path(sys.argv[1])
output_path = Path(sys.argv[2])
openclaw_bin = sys.argv[3]
openclaw_config = sys.argv[4]
state_path = sys.argv[5]

content = template_path.read_text(encoding="utf-8")
content = content.replace("__OPENCLAW_BIN__", openclaw_bin)
content = content.replace("__OPENCLAW_CONFIG_PATH__", openclaw_config)
content = content.replace("__STATE_PATH__", state_path)
output_path.write_text(content, encoding="utf-8")
PY
  echo "Wrote $CONFIG_OUTPUT"
else
  echo "Keeping existing $CONFIG_OUTPUT"
fi

SERVICE_TEMPLATE="$PROJECT_DIR/openclaw-model-cost-optimizer.service.template"
SERVICE_OUTPUT="$PROJECT_DIR/openclaw-model-cost-optimizer.service"

python3 - "$SERVICE_TEMPLATE" "$SERVICE_OUTPUT" "$HOME" "$PROJECT_DIR" "$(dirname "$OPENCLAW_BIN")" <<'PY'
import sys
from pathlib import Path

template_path = Path(sys.argv[1])
output_path = Path(sys.argv[2])
home_dir = sys.argv[3]
project_dir = sys.argv[4]
openclaw_bin_dir = sys.argv[5]

content = template_path.read_text(encoding="utf-8")
content = content.replace("__HOME_DIR__", home_dir)
content = content.replace("__PROJECT_DIR__", project_dir)
content = content.replace("__OPENCLAW_BIN_DIR__", openclaw_bin_dir)
output_path.write_text(content, encoding="utf-8")
PY
echo "Wrote $SERVICE_OUTPUT"

ln -sfn "$SERVICE_OUTPUT" "$SYSTEMD_USER_DIR/$SERVICE_NAME"
ln -sfn "$PROJECT_DIR/$TIMER_NAME" "$SYSTEMD_USER_DIR/$TIMER_NAME"

systemctl --user daemon-reload

if [[ "$NO_ENABLE" -eq 0 ]]; then
  systemctl --user enable --now "$TIMER_NAME"
  echo "Optimizer service installed and started."
else
  echo "Optimizer files installed. Timer was not enabled because --no-enable was used."
fi

echo "Done."
