import logging
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from drive_service.config import Settings
from drive_service.declination import (
    DeclinationCache,
    resolve_declination,
    wmm_declination_deg,
)
from drive_service.protocol import Gps
from helpers import gps_dict

COLLEGE_PARK = (38.99, -76.94)
SEATTLE = (47.6205, -122.3493)
MID_2026 = datetime(2026, 7, 1, tzinfo=UTC)
# About 0.009 deg of latitude per kilometre.
KM_LAT = 1.0 / 111.0


def _fix(lat: float, lon: float, **overrides) -> Gps:
    return Gps.model_validate(gps_dict(lat=lat, lon=lon, **overrides))


class _CountingModel:
    """Stand-in for the WMM that records each call."""

    def __init__(self, result: float | None = -11.0) -> None:
        self.calls: list[tuple[float, float]] = []
        self._result = result

    def __call__(self, lat: float, lon: float, altitude_m: float | None, when: datetime):
        self.calls.append((lat, lon))
        return self._result


def _cache(model: _CountingModel) -> DeclinationCache:
    return DeclinationCache(compute=model, now=lambda: MID_2026)


def test_wmm_gives_westerly_declination_at_college_park():
    declination = wmm_declination_deg(*COLLEGE_PARK, None, datetime(2026, 1, 1, tzinfo=UTC))
    assert -12.0 < declination < -10.0


def test_wmm_gives_easterly_declination_in_seattle():
    declination = wmm_declination_deg(*SEATTLE, 50.0, MID_2026)
    assert 14.0 < declination < 16.5


def test_wmm_outside_model_lifespan_is_unknown(caplog):
    with caplog.at_level(logging.WARNING):
        result = wmm_declination_deg(*COLLEGE_PARK, None, datetime(2031, 1, 1, tzinfo=UTC))
    assert result is None
    assert "declination" in caplog.text


def test_wmm_accepts_extreme_altitude():
    assert wmm_declination_deg(*COLLEGE_PARK, 2_000_000.0, MID_2026) is not None
    assert wmm_declination_deg(*COLLEGE_PARK, -5_000.0, MID_2026) is not None


def test_cache_is_empty_without_a_fix():
    model = _CountingModel()
    assert _cache(model).lookup(None) is None
    assert model.calls == []


def test_cache_computes_once_for_nearby_fixes():
    model = _CountingModel()
    cache = _cache(model)
    assert cache.lookup(_fix(*COLLEGE_PARK)) == -11.0
    assert cache.lookup(_fix(COLLEGE_PARK[0] + KM_LAT, COLLEGE_PARK[1])) == -11.0
    assert len(model.calls) == 1


def test_cache_recomputes_after_moving_far():
    model = _CountingModel()
    cache = _cache(model)
    cache.lookup(_fix(*COLLEGE_PARK))
    cache.lookup(_fix(COLLEGE_PARK[0] + 6 * KM_LAT, COLLEGE_PARK[1]))
    assert len(model.calls) == 2


def test_cache_keeps_last_value_when_gps_drops_out():
    model = _CountingModel()
    cache = _cache(model)
    cache.lookup(_fix(*COLLEGE_PARK))
    assert cache.lookup(None) == -11.0
    assert len(model.calls) == 1


def test_cache_does_not_retry_a_failed_computation_in_place():
    model = _CountingModel(result=None)
    cache = _cache(model)
    assert cache.lookup(_fix(*COLLEGE_PARK)) is None
    assert cache.lookup(_fix(*COLLEGE_PARK)) is None
    assert len(model.calls) == 1


def test_cache_clear_forgets_the_value():
    model = _CountingModel()
    cache = _cache(model)
    cache.lookup(_fix(*COLLEGE_PARK))
    cache.clear()
    assert cache.lookup(None) is None


def test_default_cache_uses_the_world_magnetic_model():
    cache = DeclinationCache(now=lambda: MID_2026)
    assert -12.0 < cache.lookup(_fix(*COLLEGE_PARK)) < -10.0


def test_configured_declination_wins_over_the_model():
    model = _CountingModel()
    assert resolve_declination(-3.5, _fix(*COLLEGE_PARK), _cache(model)) == -3.5
    assert model.calls == []


def test_unset_declination_is_computed_from_the_fix():
    model = _CountingModel(result=-10.5)
    assert resolve_declination(None, _fix(*COLLEGE_PARK), _cache(model)) == -10.5


def test_configured_declination_must_be_in_range(tmp_path):
    with pytest.raises(ValidationError):
        Settings(record_dir=tmp_path, magnetic_declination_deg=200.0)
    assert Settings(record_dir=tmp_path).magnetic_declination_deg is None
