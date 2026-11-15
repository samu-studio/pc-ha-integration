"""Default sink volume / mute via WirePlumber (wpctl), change events via `pactl subscribe`.

With pactl available, all output devices (sinks) are also listed as a `select` entity whose
selection switches the default sink.
"""

import asyncio
import json
import logging
import re
import shutil

from ..bridge import Bridge
from ..config import AudioConfig
from .base import Component, run_command, stop_process

log = logging.getLogger(__name__)

SINK = "@DEFAULT_AUDIO_SINK@"
# "Event 'change' on sink #56" / "... on server #-1" — but not "sink-input" (per-app streams).
SINK_EVENT = re.compile(rb"on (sink|server) #")
# Events that can change the list of outputs or the default one (not plain volume changes).
DEVICE_EVENT = re.compile(rb"'(new|remove)' on sink #|on server #")


def parse_wpctl_volume(output: str) -> tuple[float, bool]:
    """Parse `wpctl get-volume` output like 'Volume: 0.45 [MUTED]' -> (45.0, True)."""
    m = re.search(r"Volume:\s*([\d.]+)", output)
    if not m:
        raise ValueError(f"unexpected wpctl output: {output!r}")
    return round(float(m.group(1)) * 100, 1), "[MUTED]" in output


def parse_pactl_sinks(output: str, hidden: list[str] | None = None) -> dict[str, str]:
    """Parse `pactl -f json list sinks` -> {option label: sink name}, in pactl's order.

    Labels are the human-readable descriptions; duplicates get the sink name appended so every
    option stays unique. Sinks whose name or description is in `hidden` are left out.
    """
    hidden_set = set(hidden or ())
    sinks = []
    for sink in json.loads(output):
        name = sink.get("name")
        if not isinstance(name, str) or not name:
            continue
        desc = sink.get("description")
        desc = desc.strip() if isinstance(desc, str) and desc.strip() else name
        if name in hidden_set or desc in hidden_set:
            continue
        sinks.append((desc, name))
    counts: dict[str, int] = {}
    for desc, _ in sinks:
        counts[desc] = counts.get(desc, 0) + 1
    return {(desc if counts[desc] == 1 else f"{desc} ({name})"): name for desc, name in sinks}


class Audio(Component):
    name = "audio"

    def __init__(self, bridge: Bridge, cfg: AudioConfig):
        super().__init__(bridge)
        self.cfg = cfg
        self._refresh_pending = False
        self._has_pactl = False
        self._devices_dirty = True
        self._outputs: dict[str, str] = {}  # option label -> sink name

    async def setup(self) -> None:
        if not shutil.which("wpctl"):
            raise RuntimeError("wpctl not found (WirePlumber not installed?)")
        self.bridge.add_entity(
            "number", "volume", "Volume", command_handler=self._set_volume,
            min=0, max=self.cfg.max_volume, step=1, mode="slider", unit_of_measurement="%",
            icon="mdi:volume-high",
        )
        self.bridge.add_entity(
            "switch", "mute", "Mute", command_handler=self._set_mute, icon="mdi:volume-off",
        )
        if shutil.which("pactl"):
            try:
                await self._refresh_outputs()
                self._has_pactl = True
                self._devices_dirty = False
            except (RuntimeError, ValueError) as e:  # json.JSONDecodeError is a ValueError
                log.info("output device list disabled (pactl too old?): %s", e)
        else:
            log.info("pactl not found, output device selection disabled")

    async def refresh(self) -> None:
        volume, muted = parse_wpctl_volume(await run_command("wpctl", "get-volume", SINK))
        self.bridge.publish_state("volume", round(volume))
        self.bridge.publish_state("mute", muted)
        if self._has_pactl and self._devices_dirty:
            self._devices_dirty = False
            try:
                await self._refresh_outputs()
            except (RuntimeError, ValueError):
                self._devices_dirty = True
                raise

    async def _refresh_outputs(self) -> None:
        outputs = parse_pactl_sinks(
            await run_command("pactl", "-f", "json", "list", "sinks"), self.cfg.hidden_outputs
        )
        default = (await run_command("pactl", "get-default-sink")).strip()
        if outputs != self._outputs or not self._has_pactl:
            if outputs.keys() != self._outputs.keys():
                log.info("audio outputs: %s", ", ".join(outputs) or "(none)")
            self._outputs = outputs
            # HA requires at least one option; re-registering re-announces the new option list.
            self.bridge.add_entity(
                "select", "audio_output", "Audio output", command_handler=self._set_output,
                options=list(outputs) or ["-"], icon="mdi:speaker",
            )
        label = next((k for k, v in outputs.items() if v == default), None)
        self.bridge.publish_state("audio_output", label)

    def _schedule_refresh(self) -> None:
        """Coalesce bursts of pactl events into a single refresh."""
        if self._refresh_pending:
            return
        self._refresh_pending = True

        async def later() -> None:
            await asyncio.sleep(0.15)
            self._refresh_pending = False
            try:
                await self.refresh()
            except (RuntimeError, ValueError) as e:
                log.warning("volume refresh failed: %s", e)

        self._refresh_task = asyncio.create_task(later())

    async def run(self) -> None:
        await self.refresh()
        poller = asyncio.create_task(self._poll())
        try:
            if shutil.which("pactl"):
                await self._watch_events()
            else:
                log.info("pactl not found, falling back to polling every %ss",
                         self.cfg.poll_interval)
                await poller
        finally:
            poller.cancel()

    async def _poll(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.poll_interval)
            self._devices_dirty = True  # also catches missed device events
            try:
                await self.refresh()
            except (RuntimeError, ValueError) as e:
                log.warning("volume refresh failed: %s", e)

    async def _watch_events(self) -> None:
        proc = await asyncio.create_subprocess_exec(
            "pactl", "subscribe", stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            assert proc.stdout is not None
            async for line in proc.stdout:
                if SINK_EVENT.search(line):
                    if DEVICE_EVENT.search(line):
                        self._devices_dirty = True
                    self._schedule_refresh()
            raise RuntimeError(f"pactl subscribe exited with {await proc.wait()}")
        finally:
            await stop_process(proc)

    async def _set_volume(self, payload: str) -> None:
        percent = max(0.0, min(float(payload), float(self.cfg.max_volume)))
        await run_command("wpctl", "set-volume", SINK, f"{percent / 100:.3f}")
        await self.refresh()

    async def _set_mute(self, payload: str) -> None:
        await run_command("wpctl", "set-mute", SINK, "1" if payload == "ON" else "0")
        await self.refresh()

    async def _set_output(self, payload: str) -> None:
        sink = self._outputs.get(payload)
        if sink is None:
            raise ValueError(f"unknown audio output {payload!r}")
        await run_command("pactl", "set-default-sink", sink)
        self._devices_dirty = True
        await self.refresh()
