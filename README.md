# pc-ha-bridge

Control a Linux PC running KDE Plasma 6 from Home Assistant – via MQTT with auto-discovery.
All entities show up in HA automatically under a single device (name = hostname).

| Feature | HA entities | Backend |
|---|---|---|
| Screen brightness per monitor | one `number` (slider) per monitor | KDE `org.kde.ScreenBrightness` (D-Bus, DDC/CI) |
| Volume / mute | `number`, `switch` | `wpctl` / `pactl subscribe` |
| Audio output (all output devices, switch the default device) | `select` | `pactl` |
| Media | sensors for status/title/artist/player, buttons for play/pause/next/previous | `playerctl` (MPRIS) |
| Custom commands | one `button` per command | shell commands from the config |
| System values | CPU/RAM usage, CPU/GPU temperature, GPU load | `psutil`, sysfs hwmon, NVML |
| Online status | all entities become "unavailable" when the PC is off | MQTT Last Will |

If a tool is missing (e.g. no Plasma, no `playerctl`, no NVIDIA GPU), only the affected
feature is disabled – everything else keeps running.

## Requirements

- Home Assistant with an MQTT broker (e.g. the Mosquitto add-on) and the MQTT integration set up
- Linux with systemd and PipeWire, KDE Plasma ≥ 6.2 (for brightness), Python ≥ 3.12
- Packages: `wireplumber` (`wpctl`), `pulseaudio-utils` (`pactl`), `playerctl`
- For external monitors: DDC/CI enabled in the monitor's menu. Only monitors whose brightness
  can also be changed with the Plasma slider appear in HA.

## Installation

```bash
git clone https://github.com/samu-studio/pc-ha-integration.git pc-ha-bridge && cd pc-ha-bridge
./install.sh
```

This creates `.venv`, copies `config.example.toml` to `~/.config/pc-ha-bridge/config.toml`,
asks for the MQTT broker (host, port, user, password) and enables the systemd user service
`pc-ha-bridge` (starts with the desktop session).

To change the MQTT broker later (interactively, or without prompts, e.g. from a script):

```bash
./install.sh --mqtt
```

```bash
PC_HA_MQTT_PASSWORD='secret' ./install.sh --mqtt-host 192.168.1.10 --mqtt-port 1883 --mqtt-user pc
```

Without a terminal and without `--mqtt-host`, the example config is left unchanged; in that
case edit `[mqtt]` in the config by hand and run `systemctl --user restart pc-ha-bridge`.

Updating: run `git pull` and then `./install.sh` again. This replaces the package and the
service file and restarts the service; the config is left untouched.

View logs:

```bash
journalctl --user -u pc-ha-bridge -f
```

Uninstall (config and `.venv` are kept; `--purge` deletes them as well):

```bash
./uninstall.sh
```

The entities then remain in Home Assistant as "unavailable"; if needed, delete the device in
HA under MQTT.

## Stability and resource usage

The service is built for continuous operation and puts hardly any load on the system
(measured ~60–70 MB RAM including helper processes, ~0.2 % CPU; changes are detected through
events instead of polling).
In addition, the systemd unit enforces hard limits (`MemoryMax=200M`, `CPUQuota=20%`,
`TasksMax=64`, `Nice=10`, idle I/O) and restarts it via a watchdog if it hangs. A bug in the
service therefore cannot slow down or crash the machine.
Custom commands run as separate systemd units and are not subject to these limits.

## License

MIT – see [LICENSE](LICENSE).
