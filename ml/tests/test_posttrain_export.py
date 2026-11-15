import numpy as np

from fsd_ml import posttrain_export as export


def test_waypoints_follow_a_left_arc_in_the_chair_frame() -> None:
    # 0.45 m/s while turning left at 30 deg/s, starting faced along +y.
    t = np.arange(0, 5, 0.01)
    yaw = np.pi / 2 + export.heading(t, np.full_like(t, 30.0))
    xy = export.positions(t, yaw, np.full_like(t, 0.45))

    way = export.waypoints(t, xy, yaw, at_s=1.0)

    radius = 0.45 / np.radians(30.0)
    turned = np.radians(30.0) * export.WAYPOINT_DT_S * np.arange(1, 9)
    assert np.allclose(way[:, 0], radius * np.sin(turned), atol=0.01)
    assert np.allclose(way[:, 1], radius * (1 - np.cos(turned)), atol=0.01)
    assert np.allclose(way[:, 2:], np.column_stack([np.cos(turned), np.sin(turned)]), atol=0.01)


def test_speed_starts_late_and_ramps() -> None:
    t = np.arange(0, 3, 0.01)
    v = export.speed(t, np.array([1.0, 2.0]), np.array([0.45, 0.0]))

    assert v[t < 1.0 + export.RESPONSE_DELAY_S].max() == 0.0
    assert np.isclose(v[np.searchsorted(t, 1.9)], 0.45)
    assert v[-1] == 0.0
