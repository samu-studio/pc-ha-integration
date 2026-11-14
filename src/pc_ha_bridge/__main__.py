import argparse
import asyncio
import logging
import os
import signal
import socket
import sys
import time
from pathlib import Path

from .bridge import Bridge
from .components.audio import Audio
from .components.base import Component
from .components.brightness import Brightness
from .components.commands import Commands
from .components.media import Media
from .components.system import SystemSensors
from .config import DEFAULT_CONFIG_PATH, Config, ConfigError, load_config

log = logging.getLogger("pc_ha_bridge")

RESTART_DELAY_MIN = 10
RESTART_DELAY_MAX = 300
SETUP_TIMEOUT = 20


def sd_notify(message: str) -> None:
    """Minimal sd_notify(3): talk to systemd (Type=notify / WatchdogSec) without dependencies."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(addr)
            sock.sendall(message.encode())
    except OSError as e:
        log.debug("sd_notify failed: %s", e)


async def watchdog() -> None:
    """Ping systemd's watchdog from the event loop: if the loop ever hangs, systemd restarts us."""
    usec = int(os.environ.get("WATCHDOG_USEC", "0"))
    if not usec:
        return
    interval = usec / 1_000_000 / 3
    while True:
        sd_notify("WATCHDOG=1")
        await asyncio.sleep(interval)


def build_components(bridge: Bridge, cfg: Config) -> list[Component]:
    components: list[Component] = []
    if cfg.system.enabled:
        components.append(SystemSensors(bridge, cfg.system))
    if cfg.brightness.enabled:
        components.append(Brightness(bridge, cfg.brightness))
    if cfg.audio.enabled:
        components.append(Audio(bridge, cfg.audio))
    if cfg.media.enabled:
        components.append(Media(bridge, cfg.media))
    if cfg.commands:
        components.append(Commands(bridge, cfg.commands))
    return components


async def supervise(component: Component) -> None:
    """Restart a crashed component with exponential backoff (keeps logs and CPU quiet)."""
    delay = RESTART_DELAY_MIN
    while True:
        started = time.monotonic()
        try:
            await component.run()
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            if time.monotonic() - started > RESTART_DELAY_MAX:
                delay = RESTART_DELAY_MIN  # it ran fine for a while; start backoff over
            log.exception("%s crashed, restarting in %ds", component.name, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, RESTART_DELAY_MAX)


async def run(cfg: Config) -> None:
    bridge = Bridge(cfg)
    active: list[Component] = []
    for component in build_components(bridge, cfg):
        try:
            await asyncio.wait_for(component.setup(), SETUP_TIMEOUT)
            active.append(component)
        except Exception as e:
            log.warning("disabling %s: %s", component.name, e or type(e).__name__)

    await bridge.start()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    tasks = [asyncio.create_task(supervise(c), name=c.name) for c in active]
    tasks.append(asyncio.create_task(watchdog(), name="watchdog"))
    sd_notify("READY=1")
    await stop.wait()
    log.info("shutting down")
    sd_notify("STOPPING=1")
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    for c in active:
        await c.close()
    await bridge.stop()


def main() -> None:
    parser = argparse.ArgumentParser(prog="pc-ha-bridge", description=__doc__)
    parser.add_argument("-c", "--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        sys.exit(f"config error: {e}")
    asyncio.run(run(cfg))


if __name__ == "__main__":
    main()
