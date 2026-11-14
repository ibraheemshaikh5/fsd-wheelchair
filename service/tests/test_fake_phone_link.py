"""The fake phone counts acks, applies the in-flight limit, and reports link stats."""

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from drive_service.app import create_app
from drive_service.config import Settings
from drive_service.protocol import Link
from scripts import fake_phone
from scripts.fake_phone import LinkState, expire, may_send, on_ack, on_sent, on_skipped

NOW_MS = 1_700_000_000_000.0


def _sent(seqs: list[int], at_ms: float = 0.0) -> LinkState:
    state = LinkState()
    for seq in seqs:
        state = on_sent(state, seq, at_ms)
    return state


def test_sending_counts_and_tracks_in_flight():
    state = _sent([0, 1])
    assert state.captured == 2
    assert state.sent == 2
    assert [seq for seq, _ in state.in_flight] == [0, 1]


def test_skipping_counts_a_captured_frame_as_skipped_for_flow():
    state = on_skipped(LinkState())
    assert (state.captured, state.sent, state.skipped_flow) == (1, 0, 1)


def test_ack_counts_measures_round_trip_and_clears_older_frames():
    state = on_ack(_sent([0, 1, 2], at_ms=100.0), seq=1, now_ms=160.0)
    assert state.acked == 1
    assert state.acks_seen is True
    assert state.ack_rtt_ms == pytest.approx(60.0)
    # An ack also covers every earlier frame still waiting.
    assert [seq for seq, _ in state.in_flight] == [2]


def test_round_trip_is_smoothed():
    state = on_ack(_sent([0], at_ms=0.0), seq=0, now_ms=100.0)
    state = on_ack(on_sent(state, 1, 1000.0), seq=1, now_ms=1200.0)
    assert 100.0 < state.ack_rtt_ms < 200.0


def test_ack_for_an_unknown_seq_counts_but_keeps_the_round_trip():
    state = on_ack(_sent([5]), seq=3, now_ms=50.0)
    assert state.acked == 1
    assert state.ack_rtt_ms is None
    assert [seq for seq, _ in state.in_flight] == [5]


def test_without_acks_frames_are_never_held_back():
    state = _sent(list(range(10)))
    assert may_send(state) is True


def test_after_the_first_ack_at_most_three_frames_are_in_flight():
    state = on_ack(_sent([0]), seq=0, now_ms=10.0)
    for seq in (1, 2):
        state = on_sent(state, seq, 20.0)
    assert may_send(state) is True
    state = on_sent(state, 3, 20.0)
    assert may_send(state) is False


def test_a_lost_ack_cannot_stall_the_stream():
    state = on_ack(_sent([0]), seq=0, now_ms=10.0)
    for seq in (1, 2, 3):
        state = on_sent(state, seq, 20.0)
    assert may_send(expire(state, 20.0 + fake_phone.ACK_TIMEOUT_MS)) is False
    assert may_send(expire(state, 20.1 + fake_phone.ACK_TIMEOUT_MS)) is True


def test_link_message_matches_the_service_model():
    state = on_ack(_sent([0, 1], at_ms=0.0), seq=0, now_ms=40.0)
    state = on_skipped(state)
    link = Link.model_validate(json.loads(fake_phone.link(NOW_MS, state)))
    assert (link.captured, link.sent, link.acked, link.skipped_flow) == (3, 2, 1, 1)
    assert (link.skipped_encoder, link.skipped_socket) == (0, 0)
    assert link.in_flight == 1
    assert link.jpeg_quality == fake_phone.JPEG_QUALITY / 100
    assert link.ack_rtt_ms == pytest.approx(40.0)


def test_schedule_sends_link_stats_once_per_second():
    ticks = [due for (_, due), _ in zip(fake_phone.schedule(10.0), range(20), strict=False)]
    assert sum("link" in due for due in ticks) == 2


def test_fake_phone_counts_the_service_acks(tmp_path: Path):
    settings = Settings(
        record_dir=tmp_path / "rec",
        web_dist=tmp_path / "missing",
        allowed_hosts=("testserver",),
    )
    state = LinkState()
    with (
        TestClient(create_app(settings)) as client,
        client.websocket_connect("/ws/phone") as phone,
    ):
        for seq in range(3):
            phone.send_bytes(fake_phone.frame(seq, NOW_MS, 64, 48))
            state = on_sent(state, seq, float(seq))
        for _ in range(3):
            state = fake_phone.apply_reply(json.loads(phone.receive_text()), state, 10.0)
        phone.send_text(fake_phone.link(NOW_MS, state))
        phone.send_text(fake_phone.ping(NOW_MS, None, None))
        assert json.loads(phone.receive_text())["type"] == "pong"
        status = client.get("/api/status").json()["data"]
    assert state.acked == 3
    assert state.in_flight == ()
    assert status["link"]["acked"] == 3
    assert status["link"]["in_flight"] == 0
    assert status["stats"]["rejected"] == 0


def test_replies_other_than_acks_leave_link_stats_alone():
    state = _sent([0])
    assert fake_phone.apply_reply({"type": "pong", "t": 1.0, "server_t": 2.0}, state, 5.0) == state


@pytest.mark.parametrize("reply", [[1, 2], "ack", 7, None])
def test_replies_that_are_not_objects_are_ignored(reply):
    state = _sent([0])
    assert fake_phone.apply_reply(reply, state, 5.0) == state


class _FakeSocket:
    """Yields the given raw replies, then ends like a closed websocket."""

    def __init__(self, replies: list[str]) -> None:
        self._replies = replies

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for raw in self._replies:
            yield raw


def test_reader_skips_non_object_replies_and_counts_acks():
    holder = {"link": _sent([0, 1])}
    clock: dict[str, float | None] = {"rtt_ms": None, "offset_ms": None}
    replies = ["[1, 2]", "null", json.dumps({"v": 1, "type": "ack", "seq": 1, "t": 1.0})]
    asyncio.run(fake_phone.read_replies(_FakeSocket(replies), holder, clock))
    assert holder["link"].acked == 1
    assert holder["link"].in_flight == ()


def test_stopping_a_running_reader_is_quiet():
    async def scenario() -> bool:
        reader = asyncio.create_task(asyncio.sleep(60))
        await asyncio.sleep(0)
        await fake_phone.stop_reader(reader)
        return reader.cancelled()

    assert asyncio.run(scenario()) is True


def test_a_reader_that_failed_fails_the_run():
    async def broken() -> None:
        raise RuntimeError("reader broke")

    async def scenario() -> None:
        reader = asyncio.create_task(broken())
        await asyncio.sleep(0)
        await fake_phone.stop_reader(reader)

    with pytest.raises(RuntimeError, match="reader broke"):
        asyncio.run(scenario())


def test_send_loop_notices_a_dead_reader():
    async def broken() -> None:
        raise RuntimeError("reader broke")

    async def scenario() -> None:
        reader = asyncio.create_task(broken())
        await asyncio.sleep(0)
        fake_phone.check_reader(reader)

    with pytest.raises(RuntimeError, match="reader broke"):
        asyncio.run(scenario())


def test_a_live_reader_passes_the_check():
    async def scenario() -> None:
        reader = asyncio.create_task(asyncio.sleep(60))
        fake_phone.check_reader(reader)
        await fake_phone.stop_reader(reader)

    asyncio.run(scenario())
