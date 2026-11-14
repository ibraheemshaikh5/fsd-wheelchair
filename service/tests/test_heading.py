from drive_service.heading import best_heading
from drive_service.protocol import Gps, Heading
from helpers import gps_dict, heading_dict

MOVING = 0.5


def _gps(**overrides) -> Gps:
    return Gps.model_validate(gps_dict(**overrides))


def _compass(**overrides) -> Heading:
    return Heading.model_validate(heading_dict(**overrides))


def test_gps_course_wins_while_moving():
    result = best_heading(_gps(speed_mps=1.5, course_deg=270.0), _compass(), MOVING)
    assert result.deg == 270.0
    assert result.source == "gps_course"


def test_compass_wins_when_stopped():
    result = best_heading(
        _gps(speed_mps=0.1, course_deg=270.0), _compass(magnetic_deg=45.0), MOVING
    )
    assert result.deg == 45.0
    assert result.source == "compass"


def test_compass_used_when_gps_has_no_course():
    result = best_heading(_gps(speed_mps=2.0, course_deg=None), _compass(magnetic_deg=12.0), MOVING)
    assert result.source == "compass"
    assert result.deg == 12.0


def test_compass_used_when_gps_has_no_speed():
    result = best_heading(_gps(speed_mps=None), _compass(), MOVING)
    assert result.source == "compass"


def test_gps_course_used_without_compass():
    result = best_heading(_gps(speed_mps=2.0, course_deg=10.0), None, MOVING)
    assert result.source == "gps_course"


def test_no_heading_when_stopped_and_no_compass():
    assert best_heading(_gps(speed_mps=0.0), None, MOVING) is None


def test_no_heading_without_any_input():
    assert best_heading(None, None, MOVING) is None


def test_gps_course_is_true_north():
    result = best_heading(_gps(speed_mps=1.5, course_deg=270.0), _compass(), MOVING, -11.0)
    assert result.reference == "true"
    assert result.deg == 270.0


def test_compass_is_corrected_to_true_north():
    result = best_heading(None, _compass(magnetic_deg=90.0), MOVING, -11.0)
    assert result.deg == 79.0
    assert result.source == "compass"
    assert result.reference == "true"


def test_compass_correction_wraps_below_zero():
    result = best_heading(None, _compass(magnetic_deg=5.0), MOVING, -11.0)
    assert result.deg == 354.0


def test_compass_correction_wraps_above_360():
    result = best_heading(None, _compass(magnetic_deg=355.0), MOVING, 11.0)
    assert result.deg == 6.0


def test_heading_is_continuous_when_the_chair_stops():
    moving = best_heading(_gps(speed_mps=0.6, course_deg=79.0), _compass(), MOVING, -11.0)
    stopped = best_heading(
        _gps(speed_mps=0.0, course_deg=None), _compass(magnetic_deg=90.0), MOVING, -11.0
    )
    assert moving.source == "gps_course"
    assert stopped.source == "compass"
    assert moving.deg == stopped.deg == 79.0


def test_compass_without_declination_is_never_called_true():
    result = best_heading(None, _compass(magnetic_deg=90.0), MOVING, None)
    assert result.deg == 90.0
    assert result.reference == "magnetic"


def test_declination_defaults_to_unknown():
    assert best_heading(None, _compass(), MOVING).reference == "magnetic"
