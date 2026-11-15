"""Per-monitor brightness via KDE Plasma's org.kde.ScreenBrightness D-Bus API (Plasma >= 6.2).

PowerDevil handles DDC/CI (external monitors) and backlight (laptop panels) behind this API, so
the Plasma brightness slider/OSD stays in sync and no i2c permissions are needed.

Plasma's D-Bus display names (display0, display1, ...) are not stable across hotplug, so entities
are keyed by the monitor label instead. A monitor that disappears is marked unavailable, not
removed, so it keeps its HA entity.

PowerDevil may start after us, restart, or not exist at all (non-Plasma desktop). We watch its bus
name and attach/detach accordingly instead of failing, so this component never crashes the
service and costs nothing while idle (purely signal driven, no polling).
"""

import asyncio
import logging
from dataclasses import dataclass
from functools import partial

from dbus_next.aio import MessageBus, ProxyInterface
from dbus_next.constants import BusType

from ..bridge import Bridge
from ..config import BrightnessConfig
from ..util import slugify, unique_slug
from .base import Component

log = logging.getLogger(__name__)

SERVICE = "org.kde.ScreenBrightness"
ROOT_PATH = "/org/kde/ScreenBrightness"
ROOT_IFACE = "org.kde.ScreenBrightness"
DISPLAY_IFACE = "org.kde.ScreenBrightness.Display"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
FLAG_SUPPRESS_INDICATOR = 0x1
DBUS_TIMEOUT = 10.0


@dataclass
class Display:
    object_id: str
    label: str
    max_brightness: int
    dbus_name: str | None = None  # None while the monitor (or PowerDevil) is gone
    iface: ProxyInterface | None = None


def to_percent(value: int, maximum: int) -> int:
    return round(value * 100 / maximum) if maximum > 0 else 0


def from_percent(percent: float, maximum: int) -> int:
    return round(max(0.0, min(100.0, percent)) * maximum / 100)


class Brightness(Component):
    name = "brightness"

    def __init__(self, bridge: Bridge, cfg: BrightnessConfig):
        super().__init__(bridge)
        self.cfg = cfg
        self.bus: MessageBus | None = None
        self.root: ProxyInterface | None = None
        self.displays: dict[str, Display] = {}  # object_id -> Display
        self._tasks: set[asyncio.Task] = set()

    async def setup(self) -> None:
        await self._connect()

    async def run(self) -> None:
        while True:
            assert self.bus is not None
            try:
                await self.bus.wait_for_disconnect()
            except Exception as e:
                log.debug("session bus disconnected: %s", e)
            log.warning("lost session bus connection, reconnecting")
            self._detach()
            await asyncio.sleep(5)
            await self._connect()

    async def close(self) -> None:
        if self.bus:
            self.bus.disconnect()

    # ---- connection handling ------------------------------------------------------------------

    async def _connect(self) -> None:
        self.bus = await MessageBus(bus_type=BusType.SESSION).connect()
        intro = await self.bus.introspect("org.freedesktop.DBus", "/org/freedesktop/DBus")
        dbus = self.bus.get_proxy_object(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", intro
        ).get_interface("org.freedesktop.DBus")
        dbus.on_name_owner_changed(self._on_name_owner_changed)
        if await dbus.call_name_has_owner(SERVICE):
            await self._attach()
        else:
            log.info("%s not available yet, waiting for it to appear", SERVICE)

    def _on_name_owner_changed(self, name: str, old_owner: str, new_owner: str) -> None:
        if name != SERVICE:
            return
        if old_owner:
            log.info("PowerDevil left the bus")
            self._detach()
        if new_owner:
            self._spawn(self._attach())

    async def _attach(self) -> None:
        assert self.bus is not None
        try:
            async with asyncio.timeout(DBUS_TIMEOUT):
                intro = await self.bus.introspect(SERVICE, ROOT_PATH)
                obj = self.bus.get_proxy_object(SERVICE, ROOT_PATH, intro)
                root = obj.get_interface(ROOT_IFACE)
                props = obj.get_interface(PROPS_IFACE)
                root.on_brightness_changed(self._on_brightness_changed)
                root.on_brightness_range_changed(self._on_range_changed)
                root.on_display_added(self._on_display_added)
                root.on_display_removed(self._on_display_removed)
                self.root = root
                names = (await props.call_get(ROOT_IFACE, "DisplaysDBusNames")).value
        except Exception as e:
            log.warning("could not attach to %s: %s", SERVICE, e)
            return
        if not names:
            log.warning("Plasma reports no brightness-controllable displays (DDC/CI disabled?)")
        for name in names:
            await self._add_display(name)

    def _detach(self) -> None:
        if self.root is not None:
            try:
                self.root.off_brightness_changed(self._on_brightness_changed)
                self.root.off_brightness_range_changed(self._on_range_changed)
                self.root.off_display_added(self._on_display_added)
                self.root.off_display_removed(self._on_display_removed)
            except Exception as e:  # bus may already be gone
                log.debug("detach: %s", e)
            self.root = None
        for display in self.displays.values():
            if display.dbus_name is not None:
                display.dbus_name = display.iface = None
                self.bridge.set_available(display.object_id, False)

    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ---- displays -----------------------------------------------------------------------------

    def _by_dbus_name(self, dbus_name: str) -> Display | None:
        return next((d for d in self.displays.values() if d.dbus_name == dbus_name), None)

    def _on_display_added(self, dbus_name: str) -> None:
        self._spawn(self._add_display(dbus_name))

    async def _add_display(self, dbus_name: str) -> None:
        assert self.bus is not None
        path = f"{ROOT_PATH}/{dbus_name}"
        try:
            async with asyncio.timeout(DBUS_TIMEOUT):
                intro = await self.bus.introspect(SERVICE, path)
                obj = self.bus.get_proxy_object(SERVICE, path, intro)
                iface = obj.get_interface(DISPLAY_IFACE)
                props = obj.get_interface(PROPS_IFACE)
                label = (await props.call_get(DISPLAY_IFACE, "Label")).value or dbus_name
                maximum = (await props.call_get(DISPLAY_IFACE, "MaxBrightness")).value
                current = (await props.call_get(DISPLAY_IFACE, "Brightness")).value
        except Exception as e:
            log.warning("could not read display %s: %s", dbus_name, e)
            return

        # Reuse the entity of a disconnected monitor with the same label, otherwise make a new one.
        display = next(
            (d for d in self.displays.values() if d.dbus_name is None and d.label == label), None
        )
        if display is None:
            object_id = unique_slug("brightness_" + slugify(label), set(self.displays))
            display = Display(object_id, label, maximum)
            self.displays[object_id] = display
            self.bridge.add_entity(
                "number", object_id, f"Brightness {label}",
                command_handler=partial(self._set_brightness, object_id),
                own_availability=True,
                min=0, max=100, step=1, mode="slider", unit_of_measurement="%",
                icon="mdi:brightness-6",
            )
        display.dbus_name, display.iface, display.max_brightness = dbus_name, iface, maximum
        log.info("display %s (%s) -> %s", dbus_name, label, display.object_id)
        self.bridge.set_available(display.object_id, True)
        self.bridge.publish_state(display.object_id, to_percent(current, maximum))

    def _on_display_removed(self, dbus_name: str) -> None:
        display = self._by_dbus_name(dbus_name)
        if display:
            log.info("display %s (%s) removed", dbus_name, display.label)
            display.dbus_name = display.iface = None
            self.bridge.set_available(display.object_id, False)

    # dbus-next requires signal handlers to take exactly the signal's arguments (no *args).
    def _on_brightness_changed(
        self, dbus_name: str, brightness: int, _source_name: str, _source_context: str
    ) -> None:
        display = self._by_dbus_name(dbus_name)
        if display:
            self.bridge.publish_state(
                display.object_id, to_percent(brightness, display.max_brightness)
            )

    def _on_range_changed(self, dbus_name: str, max_brightness: int, brightness: int) -> None:
        display = self._by_dbus_name(dbus_name)
        if display:
            display.max_brightness = max_brightness
            self.bridge.publish_state(display.object_id, to_percent(brightness, max_brightness))

    async def _set_brightness(self, object_id: str, payload: str) -> None:
        display = self.displays[object_id]
        if display.iface is None:
            log.warning("ignoring brightness command for disconnected display %s", display.label)
            return
        value = from_percent(float(payload), display.max_brightness)
        flags = 0 if self.cfg.show_osd else FLAG_SUPPRESS_INDICATOR
        async with asyncio.timeout(DBUS_TIMEOUT):
            await display.iface.call_set_brightness(value, flags)
        # Plasma emits BrightnessChanged, which publishes the confirmed state.
