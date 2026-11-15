"""A run's record is summed up without tripping on what a line leaves out."""

from fsd_ml.runlog import summary


def test_a_run_is_summed_up():
    seen = {"target": "drill", "boxes": [{"distance_m": 1.5}]}
    events = [
        {"t": 0.0, "event": "connected"},
        {"t": 1.0, "event": "sending on", "steps": 2},
        {
            "t": 1.0,
            "event": "decision",
            "model": "m",
            "say": "go to the drill",
            "inference_ms": 200,
            "w_radps": 0.2,
            "path": [[0.5, 0.1]],
            "seen": seen,
            "chair": {"sent": True, "x": -4, "y": 12},
        },
        {"t": 2.0, "event": "decision", "model": "m", "w_radps": 0.0, "chair": None},
        {"t": 2.1, "event": "stop", "braked_y": -10},
    ]
    text = "\n".join(summary(events))
    assert "2 decisions over 1 s" in text
    assert "target in view in 1 of 1 looks" in text
    assert "1 commands, forward stick 12 to 12%" in text
    assert "braked_y" in text
    assert summary([{"t": 0.0, "event": "connected"}]) == ["no decisions recorded"]
