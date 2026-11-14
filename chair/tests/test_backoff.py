import pytest

from chair_link.backoff import Backoff


def test_doubles_from_base_with_no_jitter_at_top():
    b = Backoff(base_s=0.25, cap_s=5.0)
    assert [b.delay(n, rand=lambda: 1.0) for n in range(4)] == [0.25, 0.5, 1.0, 2.0]


def test_is_capped():
    b = Backoff(base_s=0.25, cap_s=5.0)
    assert b.delay(10, rand=lambda: 1.0) == 5.0
    assert b.delay(10_000, rand=lambda: 1.0) == 5.0


def test_jitter_scales_down_to_half():
    b = Backoff(base_s=1.0, cap_s=5.0)
    assert b.delay(0, rand=lambda: 0.0) == 0.5


def test_rejects_bad_settings():
    with pytest.raises(ValueError):
        Backoff(base_s=0, cap_s=5)
    with pytest.raises(ValueError):
        Backoff(base_s=2, cap_s=1)
