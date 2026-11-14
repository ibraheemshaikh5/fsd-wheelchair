import asyncio
import contextlib
import json
import threading

from chair_link.backoff import Backoff
from chair_link.client import ChairLink
from chair_link.config import LinkConfig
from chair_link.protocol import DriveCommand
from fakes import TOKEN, FakeMcu, FakeService, wait_until

FAST = Backoff(base_s=0.01, cap_s=0.05)
NOW_MS = 1_794_650_000_000
SPACING_S = 0.05


def drive_msg(seq: int, v: float = 0.2, w: float = 0.0) -> str:
    return json.dumps(
        {"type": "drive", "seq": seq, "t": NOW_MS, "v_mps": v, "w_radps": w, "ttl_ms": 200}
    )


def make_link(url: str, mcu: FakeMcu, token: str = TOKEN, delays=None) -> ChairLink:
    config = LinkConfig(service_url=url, token=token, firmware="test-fw", status_hz=20.0)

    async def recording_sleep(seconds: float) -> None:
        if delays is not None:
            delays.append(seconds)
        await asyncio.sleep(seconds)

    return ChairLink(
        config, mcu, backoff=FAST, rand=lambda: 1.0, clock_ms=lambda: NOW_MS, sleep=recording_sleep
    )


@contextlib.asynccontextmanager
async def running(link: ChairLink):
    task = asyncio.create_task(link.run_forever())
    try:
        yield task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def run(coro) -> None:
    asyncio.run(asyncio.wait_for(coro, 10))


def test_sends_bearer_token_and_hello_first():
    async def scenario():
        async with FakeService() as svc:
            mcu = FakeMcu()
            async with running(make_link(svc.url, mcu)):
                await wait_until(lambda: len(svc.received) >= 2)
            assert svc.auth_headers[0] == f"Bearer {TOKEN}"
            assert svc.received[0] == {
                "type": "hello",
                "role": "chair",
                "firmware": "test-fw",
                "max_speed_mps": 0.5,
                "max_turn_radps": 0.5,
            }
            # A new session starts disarmed.
            assert mcu.names()[0] == "stop"

    run(scenario())


def test_forwards_drive_commands_in_order():
    async def script(ws, svc):
        for seq in range(1, 4):
            await ws.send(drive_msg(seq, v=0.1 * seq))
            await asyncio.sleep(SPACING_S)  # 20 Hz-like spacing: nothing to coalesce
        await asyncio.Future()

    async def scenario():
        async with FakeService(script) as svc:
            mcu = FakeMcu()
            async with running(make_link(svc.url, mcu)):
                await wait_until(lambda: mcu.names().count("drive") == 3)
            drives = [arg for name, arg in mcu.events if name == "drive"]
            assert [d.seq for d in drives] == [1, 2, 3]
            assert drives[0] == DriveCommand(seq=1, v_mps=0.1, w_radps=0.0, ttl_ms=200)

    run(scenario())


def test_forwards_only_the_newest_command_after_a_stall():
    gate = threading.Event()
    mcu = FakeMcu(drive_gate=gate)

    async def script(ws, svc):
        await ws.send(drive_msg(1))
        await wait_until(lambda: mcu.drive_seqs() == [1])  # MCU call now blocked
        for seq in range(2, 8):
            await ws.send(drive_msg(seq, v=0.4))
        await asyncio.sleep(SPACING_S)
        gate.set()
        await asyncio.Future()

    async def scenario():
        async with FakeService(script) as svc, running(make_link(svc.url, mcu)):
            await wait_until(lambda: len(mcu.drive_seqs()) >= 2)
            await asyncio.sleep(SPACING_S)
            assert mcu.drive_seqs() == [1, 7]

    run(scenario())


def test_drops_commands_not_newer_than_the_last_forwarded():
    async def script(ws, svc):
        for seq in (5, 3, 5, 6):
            await ws.send(drive_msg(seq))
            await asyncio.sleep(SPACING_S)
        await asyncio.Future()

    async def scenario():
        async with FakeService(script) as svc:
            mcu = FakeMcu()
            async with running(make_link(svc.url, mcu)):
                await wait_until(lambda: 6 in mcu.drive_seqs())
            assert mcu.drive_seqs() == [5, 6]

    run(scenario())


def test_reports_chair_status_periodically():
    async def scenario():
        async with FakeService() as svc:
            async with running(make_link(svc.url, FakeMcu())):
                await wait_until(lambda: len(svc.of_type("chair")) >= 3)
            msg = svc.of_type("chair")[0]
            assert msg == {
                "type": "chair",
                "t": NOW_MS,
                "mode": "auto",
                "joystick_active": False,
                "estop": False,
                "battery_pct": None,
                "applied_seq": 7,
                "output": {"v_mps": 0.3, "w_radps": -0.1},
                "stopped_reason": None,
            }

    run(scenario())


def test_stops_immediately_when_service_closes_and_reconnects():
    async def script(ws, svc):
        await ws.send(drive_msg(1))
        await asyncio.sleep(0.05)  # then the server closes the connection

    async def scenario():
        async with FakeService(script) as svc:
            mcu = FakeMcu()
            async with running(make_link(svc.url, mcu)):
                await wait_until(lambda: svc.connections >= 2)
            names = mcu.names()
            first_drive = names.index("drive")
            assert names[first_drive + 1] == "stop"

    run(scenario())


def test_invalid_command_stops_but_keeps_session():
    async def script(ws, svc):
        await ws.send(drive_msg(1))
        await asyncio.sleep(SPACING_S)
        await ws.send(json.dumps({"type": "drive", "seq": 2, "v_mps": -1, "w_radps": 0}))
        await asyncio.sleep(SPACING_S)
        await ws.send(drive_msg(3))
        await asyncio.Future()

    async def scenario():
        async with FakeService(script) as svc:
            mcu = FakeMcu()
            async with running(make_link(svc.url, mcu)):
                await wait_until(lambda: mcu.names().count("drive") == 2)
                assert mcu.names() == ["stop", "drive", "stop", "drive"]
            # Cancelling the link stops the chair once more.
            assert mcu.names()[-1] == "stop"
            assert svc.connections == 1

    run(scenario())


def test_unknown_messages_are_ignored():
    async def script(ws, svc):
        await ws.send(json.dumps({"type": "welcome"}))
        await ws.send(drive_msg(1))
        await asyncio.Future()

    async def scenario():
        async with FakeService(script) as svc:
            mcu = FakeMcu()
            async with running(make_link(svc.url, mcu)):
                await wait_until(lambda: "drive" in mcu.names())
                assert mcu.names() == ["stop", "drive"]

    run(scenario())


def test_rejected_token_keeps_chair_stopped_and_backs_off():
    async def scenario():
        async with FakeService() as svc:
            mcu = FakeMcu()
            delays: list[float] = []
            async with running(make_link(svc.url, mcu, token="w" * 32, delays=delays)):
                await wait_until(lambda: svc.rejected >= 5)
            assert "drive" not in mcu.names() and "stop" in mcu.names()
            assert delays[:4] == [0.01, 0.02, 0.04, 0.05]
            assert max(delays) <= FAST.cap_s

    run(scenario())


def test_backoff_resets_after_a_working_session():
    async def script(ws, svc):
        await ws.send(drive_msg(1))
        await asyncio.sleep(0.02)

    async def scenario():
        async with FakeService(script) as svc:
            delays: list[float] = []
            async with running(make_link(svc.url, FakeMcu(), delays=delays)):
                await wait_until(lambda: svc.connections >= 3)
            reconnect_delays = [d for d in delays if d != 1 / 20.0]
            assert reconnect_delays[:2] == [0.01, 0.01]

    run(scenario())


def test_unreachable_service_retries_and_stops():
    async def scenario():
        mcu = FakeMcu()
        async with running(make_link("ws://127.0.0.1:9/ws/chair", mcu)):
            await wait_until(lambda: mcu.names().count("stop") >= 3)

    run(scenario())


def test_status_failures_do_not_end_the_session():
    async def script(ws, svc):
        await asyncio.sleep(0.2)
        await ws.send(drive_msg(1))
        await asyncio.Future()

    async def scenario():
        async with FakeService(script) as svc:
            mcu = FakeMcu(fail={"status"})
            async with running(make_link(svc.url, mcu)):
                await wait_until(lambda: "drive" in mcu.names())
            assert svc.connections == 1
            assert svc.of_type("chair") == []

    run(scenario())


def test_drive_failure_triggers_stop_and_keeps_session():
    async def script(ws, svc):
        await ws.send(drive_msg(1))
        await asyncio.Future()

    async def scenario():
        async with FakeService(script) as svc:
            mcu = FakeMcu(fail={"drive"})
            async with running(make_link(svc.url, mcu)):
                await wait_until(lambda: mcu.names().count("stop") >= 2)
            assert mcu.names()[:3] == ["stop", "drive", "stop"]
            assert svc.connections == 1

    run(scenario())


def test_limits_failure_ends_session_before_hello():
    async def scenario():
        async with FakeService() as svc:
            mcu = FakeMcu(fail={"limits"})
            async with running(make_link(svc.url, mcu)):
                await wait_until(lambda: svc.connections >= 2)
            assert svc.of_type("hello") == []

    run(scenario())


def test_stop_failure_is_logged_not_raised():
    async def scenario():
        mcu = FakeMcu(fail={"stop"})
        async with running(make_link("ws://127.0.0.1:9/ws/chair", mcu)) as task:
            await wait_until(lambda: mcu.names().count("stop") >= 2)
            assert not task.done()

    run(scenario())


def test_cancel_stops_the_chair():
    async def scenario():
        async with FakeService() as svc:
            mcu = FakeMcu()
            async with running(make_link(svc.url, mcu)):
                await wait_until(lambda: len(svc.of_type("hello")) == 1)
                stops_before = mcu.names().count("stop")
            assert mcu.names().count("stop") == stops_before + 1

    run(scenario())
