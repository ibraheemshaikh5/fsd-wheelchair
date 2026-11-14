"""A single-slot mailbox holding only the newest drive command.

The receiver puts every command from the service here; the forwarder takes
from it. While a Bridge call is slow, or after a network stall delivers a burst
of queued commands, older commands are overwritten instead of being replayed
to the microcontroller one after another (each would get a fresh ttl there).
"""

import asyncio

from .protocol import DriveCommand


class LatestCommand:
    def __init__(self) -> None:
        self._cmd: DriveCommand | None = None
        self._ready = asyncio.Event()

    def put(self, cmd: DriveCommand) -> None:
        """Replaces any command not yet taken."""
        self._cmd = cmd
        self._ready.set()

    def clear(self) -> None:
        """Drops the pending command, e.g. after the chair was told to stop."""
        self._cmd = None
        self._ready.clear()

    async def take(self) -> DriveCommand:
        """Waits for a command and returns the newest one."""
        while True:
            await self._ready.wait()
            self._ready.clear()
            cmd, self._cmd = self._cmd, None
            if cmd is not None:
                return cmd
