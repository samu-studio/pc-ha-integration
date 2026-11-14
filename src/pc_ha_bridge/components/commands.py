"""User-defined shell commands exposed as HA buttons.

Only commands from the local config file are ever executed; MQTT payloads are ignored, so nobody
with broker access can run arbitrary commands.

Under systemd, commands are started as transient user units via `systemd-run --user` so they do
NOT inherit this service's cgroup limits (MemoryMax, CPUQuota, Nice, ...) — otherwise a launched
browser would be throttled or killed together with the bridge.
"""

import asyncio
import logging
import os
import shutil
import time
from functools import partial

from ..bridge import Bridge
from ..config import CommandConfig
from ..util import slugify, unique_slug
from .base import Component

log = logging.getLogger(__name__)

MIN_INTERVAL = 2.0  # ignore repeated presses of the same button within this many seconds
MAX_RUNNING = 16  # hard cap on simultaneously running command processes


class Commands(Component):
    name = "commands"

    def __init__(self, bridge: Bridge, commands: list[CommandConfig]):
        super().__init__(bridge)
        self.commands = commands
        self._tasks: set[asyncio.Task] = set()
        self._last_press: dict[str, float] = {}
        use_systemd = "INVOCATION_ID" in os.environ and shutil.which("systemd-run")
        self._prefix = (
            ["systemd-run", "--user", "--collect", "--quiet", "--wait", "--"] if use_systemd else []
        )

    async def setup(self) -> None:
        taken: set[str] = set()
        for cmd in self.commands:
            object_id = unique_slug("cmd_" + slugify(cmd.name), taken)
            taken.add(object_id)
            self.bridge.add_entity("button", object_id, cmd.name, has_state=False, icon=cmd.icon,
                                   command_handler=partial(self._run, object_id, cmd))

    async def _run(self, object_id: str, cmd: CommandConfig, _payload: str) -> None:
        now = time.monotonic()
        if now - self._last_press.get(object_id, -MIN_INTERVAL) < MIN_INTERVAL:
            log.info("ignoring repeated press of %r", cmd.name)
            return
        if len(self._tasks) >= MAX_RUNNING:
            log.warning("too many running commands, ignoring %r", cmd.name)
            return
        self._last_press[object_id] = now

        log.info("running command %r: %s", cmd.name, cmd.command)
        proc = await asyncio.create_subprocess_exec(
            *self._prefix, "/bin/sh", "-c", cmd.command,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE, start_new_session=True,
        )
        # Don't block the handler on long-running programs (e.g. GUI apps); just log the result.
        task = asyncio.create_task(self._wait(cmd, proc))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @staticmethod
    async def _wait(cmd: CommandConfig, proc: asyncio.subprocess.Process) -> None:
        # Keep only the tail of stderr: a chatty long-running program must not grow our memory.
        assert proc.stderr is not None
        tail = b""
        while chunk := await proc.stderr.read(4096):
            tail = (tail + chunk)[-500:]
        await proc.wait()
        if proc.returncode != 0:
            log.warning("command %r exited %s: %s", cmd.name, proc.returncode,
                        tail.decode(errors="replace").strip())
