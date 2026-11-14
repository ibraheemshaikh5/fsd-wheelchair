import pytest

from chair_link.mcu import BRIDGE_TIMEOUT_S, BridgeMcu, McuError
from chair_link.protocol import DriveCommand, McuLimits, McuStatus


class RecordingCall:
    def __init__(self, replies=None, error=None):
        self.calls = []
        self.replies = replies or {}
        self.error = error

    def __call__(self, method, *params, timeout):
        self.calls.append((method, params, timeout))
        if self.error:
            raise self.error
        return self.replies.get(method)


def test_drive_sends_typed_params():
    call = RecordingCall({"drive": True})
    accepted = BridgeMcu(call).drive(DriveCommand(seq=3, v_mps=0.0, w_radps=1.0, ttl_ms=200))
    assert accepted is True
    method, params, timeout = call.calls[0]
    assert method == "drive" and timeout == BRIDGE_TIMEOUT_S
    assert params == (3, 0.0, 1.0, 200)
    assert [type(p) for p in params] == [int, float, float, int]


def test_stop_calls_stop():
    call = RecordingCall()
    BridgeMcu(call).stop()
    assert call.calls[0][0] == "stop"


def test_status_and_limits_are_decoded():
    call = RecordingCall({"status": [1, 0, 0, 5, 100, 0, 0], "limits": [500, 500, 500]})
    mcu = BridgeMcu(call)
    assert mcu.status() == McuStatus.from_array([1, 0, 0, 5, 100, 0, 0])
    assert mcu.limits() == McuLimits(0.5, 0.5, 500)


def test_bad_status_reply_becomes_mcu_error():
    with pytest.raises(McuError, match="status"):
        BridgeMcu(RecordingCall({"status": "garbage"})).status()


@pytest.mark.parametrize("error", [TimeoutError("slow"), RuntimeError("down"), ValueError("no")])
def test_bridge_failures_become_mcu_error(error):
    mcu = BridgeMcu(RecordingCall(error=error))
    with pytest.raises(McuError, match="stop"):
        mcu.stop()
