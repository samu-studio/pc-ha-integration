import asyncio
import logging

from ..bridge import Bridge

log = logging.getLogger(__name__)

COMMAND_TIMEOUT = 10.0


class Component:
    """A feature that exposes a group of entities.

    setup() registers entities and may raise to signal the feature is unavailable on this machine
    (the component is then skipped). run() is a long-running task that is restarted if it crashes.
    """

    name = "component"

    def __init__(self, bridge: Bridge):
        self.bridge = bridge

    async def setup(self) -> None:
        pass

    async def run(self) -> None:
        await asyncio.Event().wait()

    async def close(self) -> None:
        pass


async def run_command(*args: str) -> str:
    """Run a command, return stdout; raise RuntimeError on non-zero exit or timeout.

    The timeout matters: a hung wpctl/playerctl (e.g. PipeWire stuck) must not pile up processes.
    """
    proc = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), COMMAND_TIMEOUT)
    except BaseException as e:  # timeout or cancellation: never leave the child behind
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        if isinstance(e, TimeoutError):
            raise RuntimeError(f"{args[0]} timed out after {COMMAND_TIMEOUT}s") from None
        raise
    if proc.returncode != 0:
        raise RuntimeError(f"{args[0]} exited {proc.returncode}: {err.decode().strip()}")
    return out.decode()


async def stop_process(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        proc.kill()
        await proc.wait()
