"""Tiny 3-vector helpers on plain tuples, so values stay immutable."""

from __future__ import annotations

import math

type Vec3 = tuple[float, float, float]


def sub(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def scale(a: Vec3, k: float) -> Vec3:
    return (a[0] * k, a[1] * k, a[2] * k)


def dot(a: Vec3, b: Vec3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def cross(a: Vec3, b: Vec3) -> Vec3:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def norm(a: Vec3) -> float:
    return math.sqrt(dot(a, a))


def low(a: Vec3, b: Vec3) -> Vec3:
    """Element-wise minimum."""
    return (min(a[0], b[0]), min(a[1], b[1]), min(a[2], b[2]))


def high(a: Vec3, b: Vec3) -> Vec3:
    """Element-wise maximum."""
    return (max(a[0], b[0]), max(a[1], b[1]), max(a[2], b[2]))
