#!/usr/bin/env bash
# Remove the pc-ha-bridge systemd user service.
#   ./uninstall.sh          stop and remove the service (keeps config and .venv)
#   ./uninstall.sh --purge  additionally delete the config directory and .venv
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
VENV="$REPO/.venv"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/pc-ha-bridge"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="$UNIT_DIR/pc-ha-bridge.service"

purge=false
case "${1:-}" in
    "") ;;
    --purge) purge=true ;;
    -h|--help) sed -n '2,4p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "error: unknown option: $1 (use --purge or --help)" >&2; exit 2 ;;
esac

systemctl --user disable --now pc-ha-bridge.service 2>/dev/null || true
rm -f "$UNIT"
systemctl --user daemon-reload
systemctl --user reset-failed pc-ha-bridge.service 2>/dev/null || true
echo "service removed."

if $purge; then
    rm -rf "$CONFIG_DIR" "$VENV"
    echo "deleted $CONFIG_DIR and $VENV"
else
    echo "kept $CONFIG_DIR and $VENV (use --purge to delete them)"
fi
echo "note: the entities stay in Home Assistant (unavailable); delete the device there if no longer needed."
