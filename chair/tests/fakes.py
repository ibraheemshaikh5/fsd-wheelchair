"""Test doubles: a fake microcontroller and a fake driving service."""

import asyncio
import json
import threading
from http import HTTPStatus

from websockets.asyncio.server import serve

from chair_link.mcu import McuError
from chair_link.protocol import DriveCommand, McuLimits, McuStatus

TOKEN = "t" * 32
DRIVING_STATUS = [1, 0, 0, 7, 300, -100, 0]


GATE_TIMEOUT_S = 3.0


class FakeMcu:
    """Records every call. Each list entry is a ("method", args) tuple.

    With `drive_gate`, each drive call blocks (in its worker thread, like a slow
    Bridge call) until the gate is set.
    """

    def __init__(self, status=None, fail=(), drive_gate: threading.Event | None = None):
        self.events: list[tuple[str, object]] = []
        self.status_array = status or DRIVING_STATUS
        self.fail = set(fail)
        self.drive_gate = drive_gate

    def _maybe_fail(self, method: str) -> None:
        if method in self.fail:
            raise McuError(f"{method} failed")

    def drive(self, cmd: DriveCommand) -> bool:
        self.events.append(("drive", cmd))
        if self.drive_gate is not None and not self.drive_gate.wait(GATE_TIMEOUT_S):
            raise McuError("drive gate never opened")
        self._maybe_fail("drive")
        return True

    def drive_seqs(self) -> list[int]:
        return [arg.seq for name, arg in self.events if name == "drive"]

    def stop(self) -> None:
        self.events.append(("stop", None))
        self._maybe_fail("stop")

    def status(self) -> McuStatus:
        self._maybe_fail("status")
        return McuStatus.from_array(self.status_array)

    def limits(self) -> McuLimits:
        self._maybe_fail("limits")
        return McuLimits(0.5, 0.5, 500)

    def names(self) -> list[str]:
        return [name for name, _ in self.events]


class FakeService:
    """A localhost WebSocket server standing in for the driving service.

    `script` runs for each accepted connection with (ws, service); when it
    returns, the server closes that connection.
    """

    def __init__(self, script=None, token: str = TOKEN):
        self.script = script
        self.token = token
        self.received: list[dict] = []
        self.auth_headers: list[str | None] = []
        self.connections = 0
        self.rejected = 0
        self._server = None

    @property
    def url(self) -> str:
        port = self._server.sockets[0].getsockname()[1]
        return f"ws://127.0.0.1:{port}/ws/chair"

    async def __aenter__(self):
        self._server = await serve(self._handler, "127.0.0.1", 0, process_request=self._check_auth)
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        await self._server.wait_closed()

    def _check_auth(self, connection, request):
        header = request.headers.get("Authorization")
        self.auth_headers.append(header)
        if header != f"Bearer {self.token}":
            self.rejected += 1
            return connection.respond(HTTPStatus.FORBIDDEN, "forbidden\n")
        return None

    async def _handler(self, ws):
        self.connections += 1
        reader = asyncio.create_task(self._collect(ws))
        script = asyncio.ensure_future(self.script(ws, self) if self.script else asyncio.Future())
        closed = asyncio.create_task(ws.wait_closed())
        try:
            await asyncio.wait([script, closed], return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (reader, script, closed):
                task.cancel()
            await asyncio.gather(reader, script, closed, return_exceptions=True)

    async def _collect(self, ws):
        async for raw in ws:
            self.received.append(json.loads(raw))

    def of_type(self, kind: str) -> list[dict]:
        return [m for m in self.received if m.get("type") == kind]


async def wait_until(predicate, timeout: float = 3.0) -> None:
    async def poll():
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), timeout)
