from drive_service.clock import (
    ClockEstimate,
    ClockSample,
    add_sample,
    estimate,
    is_synced,
    to_server_ms,
)


def test_no_samples_means_no_estimate():
    assert estimate(()) is None


def test_estimate_uses_the_lowest_round_trip_sample():
    samples = (
        ClockSample(rtt_ms=120.0, offset_ms=40.0),
        ClockSample(rtt_ms=20.0, offset_ms=5.0),
        ClockSample(rtt_ms=60.0, offset_ms=25.0),
    )
    best = estimate(samples)
    assert best.offset_ms == 5.0
    assert best.rtt_ms == 20.0
    assert best.samples == 3


def test_add_sample_returns_new_tuple_and_keeps_window():
    original = (ClockSample(10.0, 1.0), ClockSample(11.0, 2.0))
    updated = add_sample(original, ClockSample(12.0, 3.0), window=2)
    assert original == (ClockSample(10.0, 1.0), ClockSample(11.0, 2.0))
    assert updated == (ClockSample(11.0, 2.0), ClockSample(12.0, 3.0))


def test_to_server_ms_applies_offset():
    best = estimate((ClockSample(rtt_ms=20.0, offset_ms=-250.0),))
    assert to_server_ms(1000.0, best) == 750.0


def test_is_synced_needs_an_estimate_with_a_short_round_trip():
    assert is_synced(None, 500.0) is False
    assert is_synced(ClockEstimate(offset_ms=0.0, rtt_ms=500.0, samples=1), 500.0) is True
    assert is_synced(ClockEstimate(offset_ms=0.0, rtt_ms=501.0, samples=1), 500.0) is False
