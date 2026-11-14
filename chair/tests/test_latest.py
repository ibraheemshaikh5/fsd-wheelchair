import asyncio

import pytest

from chair_link.latest import LatestCommand
from chair_link.protocol import DriveCommand


def cmd(seq: int) -> DriveCommand:
    return DriveCommand(seq=seq, v_mps=0.1, w_radps=0.0, ttl_ms=200)


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 2))


def test_take_returns_only_the_newest_of_several_puts():
    async def scenario():
        slot = LatestCommand()
        for seq in range(1, 6):
            slot.put(cmd(seq))
        assert await slot.take() == cmd(5)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(slot.take(), 0.05)

    run(scenario())


def test_take_waits_for_a_put():
    async def scenario():
        slot = LatestCommand()
        waiter = asyncio.create_task(slot.take())
        await asyncio.sleep(0.01)
        assert not waiter.done()
        slot.put(cmd(7))
        assert await waiter == cmd(7)

    run(scenario())


def test_clear_drops_a_pending_command():
    async def scenario():
        slot = LatestCommand()
        slot.put(cmd(1))
        slot.clear()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(slot.take(), 0.05)
        slot.put(cmd(2))
        assert await slot.take() == cmd(2)

    run(scenario())
