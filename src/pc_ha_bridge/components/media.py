"""MPRIS media control via playerctl.

HA's MQTT integration has no media_player platform, so playback is exposed as sensors
(status / title / artist / player) plus buttons.
"""

import asyncio
import logging
import shutil
from functools import partial

from ..bridge import Bridge
from ..config import MediaConfig
from .base import Component, run_command, stop_process

log = logging.getLogger(__name__)

SEP = "\x1f"
FORMAT = SEP.join(["{{status}}", "{{playerName}}", "{{artist}}", "{{title}}"])
STATUSES = ["playing", "paused", "stopped", "idle"]
MAX_STATE_LEN = 255  # HA rejects sensor states longer than this

BUTTONS = [
    ("media_play_pause", "Play/Pause", "play-pause", "mdi:play-pause"),
    ("media_next", "Nächster Titel", "next", "mdi:skip-next"),
    ("media_previous", "Vorheriger Titel", "previous", "mdi:skip-previous"),
]


def parse_playerctl_line(line: str) -> dict[str, str | None]:
    """Parse one line of `playerctl --follow metadata --format FORMAT` output.

    playerctl prints an empty line when no player is left.
    """
    parts = line.rstrip("\n").split(SEP)
    if len(parts) != 4:
        return {"status": "idle", "player": None, "artist": None, "title": None}
    status, player, artist, title = (p[:MAX_STATE_LEN] for p in parts)
    status = status.lower()
    return {
        "status": status if status in STATUSES else "idle",
        "player": player or None,
        "artist": artist or None,
        "title": title or None,
    }


class Media(Component):
    name = "media"

    def __init__(self, bridge: Bridge, cfg: MediaConfig):
        super().__init__(bridge)
        self.cfg = cfg

    def _playerctl(self, *args: str) -> list[str]:
        player = ["--player", self.cfg.player] if self.cfg.player else []
        return ["playerctl", *player, *args]

    async def setup(self) -> None:
        if not shutil.which("playerctl"):
            raise RuntimeError("playerctl not found (dnf install playerctl)")
        b = self.bridge
        b.add_entity("sensor", "media_status", "Wiedergabe", device_class="enum",
                     options=STATUSES, icon="mdi:music")
        b.add_entity("sensor", "media_title", "Titel", icon="mdi:music-note")
        b.add_entity("sensor", "media_artist", "Interpret", icon="mdi:account-music")
        b.add_entity("sensor", "media_player", "Player", icon="mdi:application")
        for object_id, name, action, icon in BUTTONS:
            b.add_entity("button", object_id, name, has_state=False, icon=icon,
                         command_handler=partial(self._action, action))

    async def _action(self, action: str, _payload: str) -> None:
        try:
            await run_command(*self._playerctl(action))
        except RuntimeError as e:
            log.info("media %s: %s", action, e)  # usually "No players found"

    def _publish(self, state: dict[str, str | None]) -> None:
        for key, value in state.items():
            self.bridge.publish_state(f"media_{key}", value)

    async def run(self) -> None:
        self._publish(parse_playerctl_line(""))
        proc = await asyncio.create_subprocess_exec(
            *self._playerctl("--follow", "metadata", "--format", FORMAT),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            assert proc.stdout is not None
            async for line in proc.stdout:
                self._publish(parse_playerctl_line(line.decode(errors="replace")))
            raise RuntimeError(f"playerctl exited with {await proc.wait()}")
        finally:
            await stop_process(proc)
