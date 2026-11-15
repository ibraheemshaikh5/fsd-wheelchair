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


def test_compass_is_corrected_to_true_north():
    result = best_heading(None, _compass(magnetic_deg=90.0), MOVING, -11.0)
    assert result.deg == 79.0
    assert result.source == "compass"
    assert result.reference == "true"


def test_compass_without_declination_is_never_called_true():
    result = best_heading(None, _compass(magnetic_deg=90.0), MOVING, None)
    assert result.deg == 90.0
    assert result.reference == "magnetic"
