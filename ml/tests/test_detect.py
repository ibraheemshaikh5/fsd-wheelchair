"""The detector's plain parts: what to look for, how far it is, and the background loop."""

import io

import numpy as np
import pytest
from PIL import Image

from fsd_ml.detect import (
    Box,
    Watcher,
    boxes_from_claude,
    floor_distance,
    original_or_copy,
    target_phrase,
)


@pytest.mark.parametrize(
    ("said", "thing"),
    [
        ("Go to the drill", "drill"),
        ("“Take me to the couch”", "couch"),
        ("go over to the red chair please", "red chair"),
        ("Drive to the door and stop", "door"),
        ("kitchen", "kitchen"),
        ("", None),
        (None, None),
    ],
)
def test_the_thing_an_instruction_names(said, thing):
    assert target_phrase(said) == thing


def test_floor_distance_grows_toward_the_horizon_and_stops_there():
    near = floor_distance(0.95, 0.34, 3.0, 80.0, 16 / 9)
    far = floor_distance(0.65, 0.34, 3.0, 80.0, 16 / 9)
    assert 0.3 < near < far
    assert floor_distance(0.4, 0.34, 3.0, 80.0, 16 / 9) is None


class FakeDetector:
    def find(self, image, phrase):
        return [Box(phrase, 0.4, 0.1, 0.2, 0.3, 0.9)]


def test_the_watcher_boxes_the_target_and_measures_it():
    frame = np.zeros((360, 640, 3), np.uint8)
    look = {"frame": frame, "target": "drill"}
    watcher = Watcher(
        lambda: (look["frame"], look["target"], None),
        FakeDetector(),
        measure=lambda box, aspect: round(box.y1 * 2, 2),
        background=False,
    )
    watcher.once()
    seen = watcher.latest()
    assert seen["target"] == "drill"
    assert seen["boxes"][0]["label"] == "drill" and seen["boxes"][0]["distance_m"] == 1.8
    look["target"] = None
    watcher.once()
    assert watcher.latest()["boxes"] == []


def test_claudes_answer_becomes_a_box_in_fractions():
    found = {"visible": True, "box": [64, 36, 320, 180], "confidence": 0.8, "note": "a drill"}
    (box,) = boxes_from_claude(found, "drill", 640, 360)
    assert (box.x0, box.y0, box.x1, box.y1, box.score) == (0.1, 0.1, 0.5, 0.5, 0.8)
    assert boxes_from_claude({**found, "visible": False}, "drill", 640, 360) == []
    assert boxes_from_claude({**found, "box": [0, 0, 0, 0]}, "drill", 640, 360) == []


def test_the_cameras_own_picture_is_sent_when_it_shows_the_same_view():
    def jpeg(width, height):
        out = io.BytesIO()
        Image.new("RGB", (width, height)).save(out, format="JPEG")
        return out.getvalue()

    copy = np.zeros((360, 640, 3), np.uint8)
    wide = jpeg(1280, 720)
    assert original_or_copy(wide, copy) == (wide, (1280, 720))
    # A portrait frame is cropped for the model, so its boxes would not line up: send the copy.
    assert original_or_copy(jpeg(720, 1280), copy)[1] == (640, 360)
    assert original_or_copy(None, copy)[1] == (640, 360)


class Scene:
    """A textured picture with a bright block in it, which can be moved or taken away."""

    def __init__(self) -> None:
        self.noise = np.random.default_rng(0).integers(0, 90, (360, 640, 3), dtype=np.uint8)
        self.at, self.there = 300, True

    def frame(self) -> np.ndarray:
        image = self.noise.copy()
        if self.there:
            image[150:210, self.at : self.at + 80] = 255
            image[165:195, self.at + 20 : self.at + 60] = 0
        return image

    def box(self) -> Box:
        return Box("block", 0.9, self.at / 640, 150 / 360, (self.at + 80) / 640, 210 / 360)


class CountingDetector:
    def __init__(self, scene: Scene) -> None:
        self.scene, self.calls = scene, 0

    def find(self, image, phrase):
        self.calls += 1
        return [self.scene.box()] if self.scene.there else []


def test_a_sure_find_locks_and_the_box_follows_without_asking_again():
    pytest.importorskip("cv2")
    scene, now = Scene(), [100.0]
    detector = CountingDetector(scene)
    watcher = Watcher(
        lambda: (scene.frame(), "block"), detector, background=False, clock=lambda: now[0]
    )
    watcher.once()
    assert watcher.latest()["locked"] is True and detector.calls == 1
    for _ in range(5):  # the block slides right; the tracker follows, the detector rests
        scene.at += 12
        now[0] += 0.1
        watcher.once()
    seen = watcher.latest()
    assert seen["locked"] is True and detector.calls == 1
    assert abs(seen["boxes"][0]["x0"] - scene.at / 640) < 0.02
    # Out of sight for over a second: the lock is dropped and the search starts again.
    scene.there = False
    for _ in range(14):
        now[0] += 0.1
        watcher.once()
    assert watcher.latest()["locked"] is False and detector.calls > 1


def test_a_new_request_drops_the_lock():
    pytest.importorskip("cv2")
    scene, asked = Scene(), ["block"]
    watcher = Watcher(lambda: (scene.frame(), asked[0]), CountingDetector(scene), background=False)
    watcher.once()
    assert watcher.latest()["locked"] is True
    asked[0] = None
    watcher.once()
    assert watcher.latest()["boxes"] == [] and not watcher.latest().get("locked")
