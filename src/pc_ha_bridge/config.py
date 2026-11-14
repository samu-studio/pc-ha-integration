import dataclasses
import os
import socket
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
DEFAULT_CONFIG_PATH = CONFIG_HOME / "pc-ha-bridge" / "config.toml"


class ConfigError(Exception):
    pass


@dataclass
class MqttConfig:
    host: str = "localhost"
    port: int = 1883
    username: str | None = None
    password: str | None = None
    tls: bool = False
    keepalive: int = 60
    discovery_prefix: str = "homeassistant"
    base_topic: str = "pc-ha-bridge"


@dataclass
class DeviceConfig:
    id: str = ""
    name: str = ""


@dataclass
class SystemConfig:
    enabled: bool = True
    interval: float = 10.0
    # "chip" or "chip/label" as shown by psutil.sensors_temperatures(); auto-detected if unset.
    cpu_temp_sensor: str | None = None
    # Query nvidia-smi for GPU temperature/load if it is installed.
    nvidia: bool = True


@dataclass
class BrightnessConfig:
    enabled: bool = True
    show_osd: bool = True


@dataclass
class AudioConfig:
    enabled: bool = True
    max_volume: int = 100
    # Fallback polling interval; changes are normally picked up instantly via `pactl subscribe`.
    poll_interval: float = 30.0
    # Output devices (sink name or description) not to offer in the "Audioausgabe" select.
    hidden_outputs: list[str] = field(default_factory=list)


@dataclass
class MediaConfig:
    enabled: bool = True
    # Restrict to one MPRIS player (playerctl --player), e.g. "spotify". None = most recent player.
    player: str | None = None


@dataclass
class CommandConfig:
    name: str
    command: str
    icon: str = "mdi:console"


@dataclass
class Config:
    mqtt: MqttConfig = field(default_factory=MqttConfig)
    device: DeviceConfig = field(default_factory=DeviceConfig)
    system: SystemConfig = field(default_factory=SystemConfig)
    brightness: BrightnessConfig = field(default_factory=BrightnessConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    media: MediaConfig = field(default_factory=MediaConfig)
    commands: list[CommandConfig] = field(default_factory=list)


def _build[T](cls: type[T], data: Any, section: str) -> T:
    if not isinstance(data, dict):
        raise ConfigError(f"[{section}] must be a table")
    known = {f.name for f in dataclasses.fields(cls)}  # type: ignore[arg-type]
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"[{section}] unknown key(s): {', '.join(sorted(unknown))}")
    try:
        return cls(**data)
    except TypeError as e:
        raise ConfigError(f"[{section}] {e}") from e


def parse_config(data: dict[str, Any]) -> Config:
    sections = {
        "mqtt": MqttConfig,
        "device": DeviceConfig,
        "system": SystemConfig,
        "brightness": BrightnessConfig,
        "audio": AudioConfig,
        "media": MediaConfig,
    }
    unknown = set(data) - set(sections) - {"commands"}
    if unknown:
        raise ConfigError(f"unknown section(s): {', '.join(sorted(unknown))}")

    kwargs: dict[str, Any] = {
        name: _build(cls, data.get(name, {}), name) for name, cls in sections.items()
    }
    commands = data.get("commands", [])
    if not isinstance(commands, list):
        raise ConfigError("commands must be an array of tables ([[commands]])")
    kwargs["commands"] = [_build(CommandConfig, c, "commands") for c in commands]
    cfg = Config(**kwargs)

    hostname = socket.gethostname().split(".")[0]
    cfg.device.id = cfg.device.id or hostname
    cfg.device.name = cfg.device.name or hostname
    return cfg


def load_config(path: Path) -> Config:
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError as e:
        raise ConfigError(f"config file not found: {path}") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e
    return parse_config(data)
