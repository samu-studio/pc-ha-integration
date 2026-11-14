#!/usr/bin/env bash
# Install pc-ha-bridge as a systemd user service running in the desktop session.
# Safe to re-run for updates: the unit file and the package are replaced, the config is kept.
#   ./install.sh              install / update (asks for the MQTT broker on first install)
#   ./install.sh --mqtt       (re)configure the MQTT broker interactively
#   ./install.sh --mqtt-host H [--mqtt-port P] [--mqtt-user U]
#                             set the broker non-interactively; the password is asked for when
#                             running in a terminal, or taken from $PC_HA_MQTT_PASSWORD
#   ./install.sh --uninstall  same as ./uninstall.sh (pass --purge to also delete config + .venv)
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
VENV="$REPO/.venv"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/pc-ha-bridge"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="$UNIT_DIR/pc-ha-bridge.service"

if [ "${1:-}" = "--uninstall" ]; then
    shift
    exec "$REPO/uninstall.sh" "$@"
fi

usage() { sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; }

mqtt_reconfigure=0
opt_host="" opt_port="" opt_user="" opt_user_set=0
while [ $# -gt 0 ]; do
    case "$1" in
        --mqtt) mqtt_reconfigure=1 ;;
        --mqtt-host) opt_host="${2:?--mqtt-host needs a value}"; mqtt_reconfigure=1; shift ;;
        --mqtt-port) opt_port="${2:?--mqtt-port needs a value}"; mqtt_reconfigure=1; shift ;;
        --mqtt-user) opt_user="${2-}"; opt_user_set=1; mqtt_reconfigure=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "error: unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done
if [ -n "$opt_port" ] && ! [[ "$opt_port" =~ ^[0-9]+$ && "$opt_port" -ge 1 && "$opt_port" -le 65535 ]]; then
    echo "error: invalid MQTT port: $opt_port" >&2
    exit 2
fi

python3 -c 'import sys; sys.exit(sys.version_info < (3, 12))' \
    || { echo "error: Python >= 3.12 required (found $(python3 --version))"; exit 1; }

# Optional runtime tools; the matching feature is disabled if a tool is missing.
missing=()
for tool in wpctl pactl playerctl; do
    command -v "$tool" >/dev/null || missing+=("$tool")
done
if [ ${#missing[@]} -gt 0 ]; then
    echo "note: not found: ${missing[*]} — the related features will be disabled."
    echo "      Packages: wpctl=wireplumber, pactl=pulseaudio-utils (Debian/Ubuntu: pulseaudio-utils),"
    echo "      playerctl=playerctl"
fi

# System site packages: reuse distro packages (dbus-next, psutil) when available.
# Recreate the venv if it is broken (e.g. the system Python was upgraded).
if [ -d "$VENV" ] && ! "$VENV/bin/python" -c '' 2>/dev/null; then
    echo "note: $VENV is broken, recreating it"
    rm -rf "$VENV"
fi
[ -d "$VENV" ] || python3 -m venv --system-site-packages "$VENV"
"$VENV/bin/pip" install --quiet --upgrade -e "$REPO"

mkdir -p "$CONFIG_DIR" "$UNIT_DIR"
CONFIG="$CONFIG_DIR/config.toml"
if [ ! -f "$CONFIG" ]; then
    install -m 600 "$REPO/config.example.toml" "$CONFIG"
    echo "created $CONFIG"
    mqtt_reconfigure=1
    new_config=1
else
    new_config=0
fi

# Rewrites only the given keys of the [mqtt] section, keeping comments and everything else.
# Values come from the environment (the password must not appear in argv / ps).
# MQTT_SET: space-separated keys to set, MQTT_UNSET: keys to remove.
mqtt_edit() {
    "$VENV/bin/python" - "$CONFIG" <<'PY'
import json, os, re, sys, tempfile, tomllib

path = sys.argv[1]
with open(path, "rb") as f:
    tomllib.load(f)  # refuse to touch a config that is already broken
with open(path, encoding="utf-8") as f:
    lines = f.read().splitlines()

values = {}
for key in os.environ.get("MQTT_SET", "").split():
    raw = os.environ["MQTT_" + key.upper()]
    values[key] = raw if key == "port" else json.dumps(raw, ensure_ascii=False)
unset = os.environ.get("MQTT_UNSET", "").split()

start = next((i for i, l in enumerate(lines) if l.strip() == "[mqtt]"), None)
if start is None:
    lines[0:0] = ["[mqtt]", ""]
    start = 0
end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))

done = set()
for i in range(start + 1, end):
    m = re.match(r"\s*(#\s*)?([A-Za-z_]+)\s*=", lines[i])
    if not m:
        continue
    key, commented = m.group(2), m.group(1) is not None
    if key in values and key not in done:
        lines[i] = f"{key} = {values[key]}"
        done.add(key)
    elif key in unset and not commented:
        lines[i] = None  # drop it, so no stale password lingers in a comment

# New keys go right after the last active key (or the header), before trailing comments.
last = max((i for i in range(start + 1, end)
            if lines[i] is not None and re.match(r"\s*[A-Za-z_]+\s*=", lines[i])),
           default=start)
lines[last + 1:last + 1] = [f"{k} = {v}" for k, v in values.items() if k not in done]

text = "\n".join(l for l in lines if l is not None) + "\n"
tomllib.loads(text)
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".config.toml.")
with os.fdopen(fd, "w", encoding="utf-8") as f:
    f.write(text)
os.chmod(tmp, 0o600)
os.replace(tmp, path)
PY
}

# Current [mqtt] values (tab-separated host, port, username) as prompt defaults.
mqtt_current() {
    "$VENV/bin/python" - "$CONFIG" <<'PY'
import sys, tomllib
with open(sys.argv[1], "rb") as f:
    m = tomllib.load(f).get("mqtt", {})
print(m.get("host", "localhost"), m.get("port", 1883), m.get("username") or "", sep="\t")
PY
}

if [ "$mqtt_reconfigure" = 1 ]; then
    IFS=$'\t' read -r cur_host cur_port cur_user < <(mqtt_current)
    if [ "$new_config" = 1 ]; then
        cur_user=""  # the example's placeholder user is not a sensible default
    fi
    host="${opt_host:-$cur_host}" port="${opt_port:-$cur_port}"
    if [ "$opt_user_set" = 1 ]; then user="$opt_user"; else user="$cur_user"; fi
    password="${PC_HA_MQTT_PASSWORD-}"
    keep_password=0
    interactive=0
    [ -t 0 ] && interactive=1

    if [ "$interactive" = 1 ]; then
        echo "MQTT broker (Enter keeps the value in brackets):"
        [ -n "$opt_host" ] || { read -r -p "  Host [$host]: " ans; host="${ans:-$host}"; }
        if [ -z "$opt_port" ]; then
            while :; do
                read -r -p "  Port [$port]: " ans; ans="${ans:-$port}"
                [[ "$ans" =~ ^[0-9]+$ && "$ans" -ge 1 && "$ans" -le 65535 ]] && { port="$ans"; break; }
                echo "  invalid port"
            done
        fi
        if [ "$opt_user_set" = 0 ]; then
            read -r -p "  Username ('-' = no login) [$user]: " ans
            if [ "$ans" = "-" ]; then user=""; else user="${ans:-$user}"; fi
        fi
        if [ -n "$user" ] && [ -z "${PC_HA_MQTT_PASSWORD+x}" ]; then
            prompt="  Password"
            [ "$new_config" = 0 ] && [ "$user" = "$cur_user" ] && prompt+=" (empty = unchanged)"
            read -r -s -p "$prompt: " password; echo
            [ -z "$password" ] && [ "$new_config" = 0 ] && [ "$user" = "$cur_user" ] && keep_password=1
        fi
    elif [ -n "$user" ] && [ -z "${PC_HA_MQTT_PASSWORD+x}" ]; then
        if [ "$new_config" = 0 ] && [ "$user" = "$cur_user" ]; then
            keep_password=1
        else
            echo "warning: no password given for MQTT user '$user' (set \$PC_HA_MQTT_PASSWORD)" >&2
        fi
    fi
    if [ "$interactive" = 0 ] && [ "$new_config" = 1 ] && [ -z "$opt_host" ]; then
        echo "note: not running in a terminal — edit the [mqtt] section of $CONFIG, then run:"
        echo "  systemctl --user restart pc-ha-bridge"
        mqtt_reconfigure=0
    fi
fi

if [ "$mqtt_reconfigure" = 1 ]; then
    export MQTT_HOST="$host" MQTT_PORT="$port" MQTT_USERNAME="$user" MQTT_PASSWORD="$password"
    if [ -z "$user" ]; then
        MQTT_SET="host port" MQTT_UNSET="username password" mqtt_edit
    elif [ "$keep_password" = 1 ]; then
        MQTT_SET="host port username" MQTT_UNSET="" mqtt_edit
    else
        MQTT_SET="host port username password" MQTT_UNSET="" mqtt_edit
    fi
    unset MQTT_PASSWORD password PC_HA_MQTT_PASSWORD
    echo "MQTT: ${user:+$user@}$host:$port → $CONFIG"
fi

sed "s|@VENV@|$VENV|g" "$REPO/systemd/pc-ha-bridge.service" > "$UNIT"
systemctl --user daemon-reload
systemctl --user enable pc-ha-bridge.service
systemctl --user restart pc-ha-bridge.service
echo "installed. logs: journalctl --user -u pc-ha-bridge -f"
