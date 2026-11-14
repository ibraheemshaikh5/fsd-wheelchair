"""Chair link state: immutable snapshots and the view the API shows."""

from drive_service.chair.commander import BenchCommand
from drive_service.chair.link import ChairLink, ChairLinkStore, chair_view
from drive_service.chair.protocol import ChairHello, ChairStatus, DriveCommand
from test_chair_support import hello_dict, status_dict

STALE_MS = 500.0
HELLO = ChairHello(**hello_dict())
BENCH = BenchCommand(v_mps=0.2, w_radps=0.1, expires_mono=20.0)


def _status(**overrides) -> ChairStatus:
    return ChairStatus.model_validate(status_dict(**overrides))


def _command(seq: int) -> DriveCommand:
    return DriveCommand(seq=seq, t=0, v_mps=0.0, w_radps=0.0, ttl_ms=200)


def test_starts_disconnected():
    link = ChairLink()
    assert not link.connected
    assert link.hello is None
    assert link.status is None
    assert link.bench is None


def test_claim_gives_each_connection_a_new_lease():
    store = ChairLinkStore()
    first = store.claim()
    second = store.claim()
    assert second == first + 1
    assert store.holds(second)
    assert not store.holds(first)
    assert store.state.connected


def test_old_lease_cannot_disconnect_the_new_connection():
    store = ChairLinkStore()
    old = store.claim()
    store.claim()
    store.update(lambda link: link.disconnected(old))
    assert store.state.connected


def test_disconnect_clears_hello_status_and_bench_but_keeps_the_seq():
    store = ChairLinkStore()
    lease = store.claim()
    store.update(lambda link: link.with_hello(HELLO).with_status(_status(), 1.0))
    store.update(lambda link: link.with_bench(BENCH).with_command(_command(9)))
    store.update(lambda link: link.disconnected(lease))
    link = store.state
    assert not link.connected
    assert (link.hello, link.status, link.bench) == (None, None, None)
    assert link.last_command == _command(9)


def test_a_new_connection_never_inherits_a_bench_command():
    link = ChairLink().connected_as(1).with_bench(BENCH).connected_as(2)
    assert link.bench is None


def test_methods_return_new_snapshots():
    link = ChairLink()
    changed = link.connected_as(1).with_hello(HELLO)
    assert link.hello is None and not link.connected
    assert changed is not link


def test_manual_estop_or_joystick_cancels_the_bench_command():
    base = ChairLink().connected_as(1).with_hello(HELLO).with_bench(BENCH)
    assert base.with_status(_status(), 1.0).bench == BENCH
    assert base.with_status(_status(mode="manual"), 1.0).bench is None
    assert base.with_status(_status(estop=True), 1.0).bench is None
    assert base.with_status(_status(joystick_active=True), 1.0).bench is None


def test_rejected_messages_are_counted():
    assert ChairLink().with_rejected().with_rejected().rejected == 2


def test_view_when_disconnected():
    view = chair_view(ChairLink(), 5.0, STALE_MS)
    assert view == {
        "connected": False,
        "hello": None,
        "status": None,
        "status_age_ms": None,
        "status_fresh": False,
        "last_command": None,
        "test": {"active": False, "source": None, "v_mps": 0.0, "w_radps": 0.0, "remaining_ms": 0},
        "rejected": 0,
    }


def test_view_ages_the_status_and_reports_freshness():
    link = ChairLink().connected_as(1).with_hello(HELLO).with_status(_status(), 10.0)
    fresh = chair_view(link, 10.2, STALE_MS)
    assert fresh["connected"] is True
    assert fresh["hello"]["firmware"] == "test-1.0"
    assert fresh["status"]["mode"] == "auto"
    assert fresh["status_age_ms"] == 200
    assert fresh["status_fresh"] is True
    stale = chair_view(link, 10.6, STALE_MS)
    assert stale["status_fresh"] is False


def test_view_shows_the_running_bench_command_and_last_command():
    link = ChairLink().connected_as(1).with_bench(BENCH).with_command(_command(4))
    view = chair_view(link, 19.5, STALE_MS)
    assert view["test"] == {
        "active": True, "source": "test", "v_mps": 0.2, "w_radps": 0.1, "remaining_ms": 500,
    }  # fmt: skip
    assert view["last_command"]["seq"] == 4


def test_view_shows_an_expired_bench_command_as_inactive():
    link = ChairLink().connected_as(1).with_bench(BENCH)
    assert chair_view(link, 25.0, STALE_MS)["test"]["active"] is False


def test_without_bench_clears_it():
    assert ChairLink().with_bench(BENCH).without_bench().bench is None
