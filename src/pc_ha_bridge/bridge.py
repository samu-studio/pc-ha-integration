"""MQTT connection, Home Assistant discovery and state publishing.

paho-mqtt runs its network loop in its own thread; everything else runs in the asyncio loop.
Command callbacks from MQTT are handed to the asyncio loop via call_soon_threadsafe, and the
shared registries are guarded by a lock.

Commands are coalesced per topic: at most one handler per entity runs at a time, and while it
runs only the newest payload is kept. A dragged HA slider (or a flood of messages) therefore
never queues up work or spawns unbounded tasks/processes.
"""

import asyncio
import json
import logging
import ssl
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import paho.mqtt.client as mqtt

from . import __version__
from .config import Config
from .util import slugify

log = logging.getLogger(__name__)

CommandHandler = Callable[[str], Awaitable[None]]


@dataclass
class Entity:
    component: str  # HA MQTT platform: sensor, number, switch, button, ...
    object_id: str
    discovery: dict[str, Any]
    command_handler: CommandHandler | None


class Bridge:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.device_id = slugify(cfg.device.id)
        self.base_topic = f"{cfg.mqtt.base_topic}/{self.device_id}"
        self.availability_topic = f"{self.base_topic}/availability"
        self.ha_status_topic = f"{cfg.mqtt.discovery_prefix}/status"

        self._lock = threading.Lock()
        self._entities: dict[str, Entity] = {}
        self._states: dict[str, str] = {}  # topic -> last payload, republished on reconnect
        self._handlers: dict[str, CommandHandler] = {}  # command topic -> handler
        self._connected = False
        self._loop: asyncio.AbstractEventLoop | None = None
        # Command coalescing state; only touched from the asyncio loop.
        self._busy: set[str] = set()
        self._pending: dict[str, str] = {}
        self._tasks: set[asyncio.Task] = set()  # strong refs so running handlers aren't GC'd

        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id=f"pc-ha-bridge-{self.device_id}"
        )
        if cfg.mqtt.username:
            client.username_pw_set(cfg.mqtt.username, cfg.mqtt.password)
        if cfg.mqtt.tls:
            client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
        client.will_set(self.availability_topic, "offline", qos=1, retain=True)
        client.reconnect_delay_set(min_delay=1, max_delay=60)
        client.max_queued_messages_set(200)  # bound memory if the broker is slow/unreachable
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        self._client = client

    # ---- topics -------------------------------------------------------------------------------

    def state_topic(self, object_id: str) -> str:
        return f"{self.base_topic}/{object_id}/state"

    def command_topic(self, object_id: str) -> str:
        return f"{self.base_topic}/{object_id}/set"

    def entity_availability_topic(self, object_id: str) -> str:
        return f"{self.base_topic}/{object_id}/availability"

    def discovery_topic(self, component: str, object_id: str) -> str:
        return f"{self.cfg.mqtt.discovery_prefix}/{component}/{self.device_id}/{object_id}/config"

    # ---- lifecycle ----------------------------------------------------------------------------

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        log.info("connecting to MQTT broker %s:%d", self.cfg.mqtt.host, self.cfg.mqtt.port)
        self._client.connect_async(self.cfg.mqtt.host, self.cfg.mqtt.port, self.cfg.mqtt.keepalive)
        self._client.loop_start()

    async def stop(self) -> None:
        if self._connected:
            info = self._client.publish(self.availability_topic, "offline", qos=1, retain=True)
            await asyncio.to_thread(info.wait_for_publish, 2.0)
        self._client.disconnect()
        self._client.loop_stop()

    # ---- entity registry ----------------------------------------------------------------------

    def add_entity(
        self,
        component: str,
        object_id: str,
        name: str,
        *,
        command_handler: CommandHandler | None = None,
        has_state: bool = True,
        own_availability: bool = False,
        **extra: Any,
    ) -> None:
        """Register an entity and announce it to Home Assistant.

        `own_availability` adds a per-entity availability topic (see set_available) on top of the
        device-wide one, for things that can disappear while the bridge keeps running (monitors).
        """
        availability = [{"topic": self.availability_topic}]
        if own_availability:
            availability.append({"topic": self.entity_availability_topic(object_id)})
        discovery: dict[str, Any] = {
            "name": name,
            "unique_id": f"{self.device_id}_{object_id}",
            "availability": availability,
            "availability_mode": "all",
            "device": {
                "identifiers": [f"pc-ha-bridge_{self.device_id}"],
                "name": self.cfg.device.name,
                "manufacturer": "pc-ha-bridge",
                "model": "Linux PC (KDE Plasma)",
                "sw_version": __version__,
            },
            "origin": {"name": "pc-ha-bridge", "sw_version": __version__},
        }
        if has_state:
            discovery["state_topic"] = self.state_topic(object_id)
        if command_handler is not None:
            discovery["command_topic"] = self.command_topic(object_id)
        discovery.update(extra)

        entity = Entity(component, object_id, discovery, command_handler)
        with self._lock:
            self._entities[object_id] = entity
            if command_handler is not None:
                self._handlers[self.command_topic(object_id)] = command_handler
            connected = self._connected
        if connected:
            self._announce(entity)

    def publish_state(self, object_id: str, value: Any) -> None:
        if isinstance(value, bool):
            payload = "ON" if value else "OFF"
        elif value is None:
            payload = "None"  # HA MQTT sensors interpret this as "unknown"
        else:
            payload = str(value)
        self._publish_retained(self.state_topic(object_id), payload)

    def set_available(self, object_id: str, available: bool) -> None:
        self._publish_retained(
            self.entity_availability_topic(object_id), "online" if available else "offline"
        )

    # ---- internals ----------------------------------------------------------------------------

    def _publish_retained(self, topic: str, payload: str) -> None:
        with self._lock:
            if self._states.get(topic) == payload:
                return
            self._states[topic] = payload
            connected = self._connected
        if connected:
            self._client.publish(topic, payload, qos=0, retain=True)

    def _announce(self, entity: Entity) -> None:
        self._client.publish(
            self.discovery_topic(entity.component, entity.object_id),
            json.dumps(entity.discovery),
            qos=1,
            retain=True,
        )
        if entity.command_handler is not None:
            self._client.subscribe(self.command_topic(entity.object_id), qos=1)

    def _announce_all(self) -> None:
        with self._lock:
            entities = list(self._entities.values())
            states = dict(self._states)
        for entity in entities:
            self._announce(entity)
        for topic, payload in states.items():
            self._client.publish(topic, payload, qos=0, retain=True)

    # ---- paho callbacks (network thread) ------------------------------------------------------

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            log.error("MQTT connection refused: %s", reason_code)
            return
        log.info("connected to MQTT broker")
        with self._lock:
            self._connected = True
        client.subscribe(self.ha_status_topic, qos=1)
        client.publish(self.availability_topic, "online", qos=1, retain=True)
        self._announce_all()

    def _on_disconnect(self, client, userdata, flags, reason_code, properties) -> None:
        with self._lock:
            self._connected = False
        if reason_code != 0:
            log.warning("MQTT connection lost (%s), reconnecting", reason_code)

    def _on_message(self, client, userdata, msg: mqtt.MQTTMessage) -> None:
        payload = msg.payload.decode(errors="replace")
        if msg.topic == self.ha_status_topic:
            if payload == "online":
                log.info("Home Assistant restarted, re-announcing entities")
                client.publish(self.availability_topic, "online", qos=1, retain=True)
                self._announce_all()
            return
        with self._lock:
            handler = self._handlers.get(msg.topic)
        if handler is None or self._loop is None:
            return
        log.debug("command %s <- %r", msg.topic, payload)
        self._loop.call_soon_threadsafe(self._dispatch, msg.topic, handler, payload)

    # ---- command dispatch (asyncio loop) ------------------------------------------------------

    def _dispatch(self, topic: str, handler: CommandHandler, payload: str) -> None:
        if topic in self._busy:
            self._pending[topic] = payload  # replaces any older unprocessed payload
            return
        self._busy.add(topic)
        task = asyncio.create_task(self._drain(topic, handler, payload))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _drain(self, topic: str, handler: CommandHandler, payload: str) -> None:
        try:
            while True:
                try:
                    await handler(payload)
                except Exception as e:
                    log.warning("command %s (payload %r) failed: %s", topic, payload, e)
                if topic not in self._pending:
                    return
                payload = self._pending.pop(topic)
        finally:
            self._busy.discard(topic)
