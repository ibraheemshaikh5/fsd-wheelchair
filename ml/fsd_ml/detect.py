"""Find the thing the rider named in the camera picture, and box it.

This is a second, small model beside the driving model. The driving model only
returns a path; it never says what it is looking at. The detector answers a
different question: is the named thing in the picture, and where?

    detector = Detector()
    detector.find(frame, "drill")     # boxes, best first

Needs the ``detect`` dependency group (torch and transformers); the rest of the
code runs without it.
"""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import math
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

MODEL_ID = "google/owlvit-base-patch32"
# The detector's own confidence; low numbers are normal for it.
THRESHOLD = 0.08
KEEP = 3

_LEAD = re.compile(
    r"^(please\s+)?((go|drive|head|move|navigate|get|walk|roll)\s+((over|up|back|right|straight)\s+)*"
    r"(to(wards?)?|into|near|by)|take\s+me\s+(over\s+)?(to|into|near)|bring\s+me\s+to|find|"
    r"follow|approach|stop\s+(at|by|near))\s+",
    re.IGNORECASE,
)
_ARTICLE = re.compile(r"^(the|a|an|my|that|this)\s+", re.IGNORECASE)
_TAIL = re.compile(r"\s+(please|now|and\s+.*|then\s+.*)$", re.IGNORECASE)


def target_phrase(instruction: str | None) -> str | None:
    """The thing an instruction names: "Go to the drill" gives "drill"."""
    words = (instruction or "").strip().strip("“”\"'.!?").strip()
    if not words:
        return None
    words = _LEAD.sub("", words)
    words = _TAIL.sub("", words)
    words = _ARTICLE.sub("", words).strip()
    return words.lower() or None


@dataclass(frozen=True, slots=True)
class Box:
    """A found thing. Edges are fractions of the picture's width and height."""

    label: str
    score: float
    x0: float
    y0: float
    x1: float
    y1: float
    # Along the floor from the lens to where the box meets the floor, if that can be worked out.
    distance_m: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def floor_distance(
    y_fraction: float, height_m: float, tilt_down_deg: float, hfov_deg: float, aspect: float
) -> float | None:
    """How far away the floor is at a height in the picture (0 top, 1 bottom).

    Right only for something standing on the floor, seen by a camera at a known
    height and tilt. Returns None at or above the horizon.
    """
    half_v = math.atan(math.tan(math.radians(hfov_deg) / 2) / aspect)
    below = math.radians(tilt_down_deg) + math.atan((2 * y_fraction - 1) * math.tan(half_v))
    if below <= math.radians(1.0):
        return None
    return height_m / math.tan(below)


def available() -> bool:
    return all(importlib.util.find_spec(name) for name in ("torch", "transformers"))


class Detector:
    """OWL-ViT: boxes for whatever words it is given. Loads on first use."""

    def __init__(self, model_id: str = MODEL_ID, threshold: float = THRESHOLD) -> None:
        self.model_id, self.threshold = model_id, threshold
        self._model = self._processor = self._torch = None
        self.device = "cpu"

    def load(self) -> None:
        if self._model is not None:
            return
        import torch
        from transformers import OwlViTForObjectDetection, OwlViTProcessor

        self.device = "mps" if torch.backends.mps.is_available() else "cpu"
        self._processor = OwlViTProcessor.from_pretrained(self.model_id)
        self._model = OwlViTForObjectDetection.from_pretrained(self.model_id).to(self.device).eval()
        self._torch = torch

    def find(self, image: np.ndarray, phrase: str) -> list[Box]:
        self.load()
        torch = self._torch
        inputs = self._processor(
            text=[[f"a photo of a {phrase}"]], images=image, return_tensors="pt"
        ).to(self.device)
        with torch.no_grad():
            outputs = self._model(**inputs)
        scores = torch.sigmoid(outputs.logits[0, :, 0]).cpu().numpy()
        # Boxes come as centre and size, as fractions of the picture.
        centres = outputs.pred_boxes[0].cpu().numpy()
        boxes = []
        for index in np.argsort(-scores)[:KEEP]:
            if scores[index] < self.threshold:
                break
            cx, cy, w, h = (float(v) for v in centres[index])
            edges = [cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2]
            x0, y0, x1, y1 = (round(min(1.0, max(0.0, v)), 4) for v in edges)
            boxes.append(Box(phrase, round(float(scores[index]), 3), x0, y0, x1, y1))
        return boxes


class YoloDetector:
    """YOLO-World: the same job as OWL-ViT, several times faster and surer of common things."""

    def __init__(
        self, weights: Path = Path("runs/weights/yolov8s-worldv2.pt"), threshold: float = 0.1
    ) -> None:
        self.weights, self.threshold = weights, threshold
        self._model: Any = None
        self._phrase: str | None = None
        self.device = "cpu"

    def find(self, image: np.ndarray, phrase: str) -> list[Box]:
        if self._model is None:
            import torch
            from ultralytics import YOLOWorld

            self.weights.parent.mkdir(parents=True, exist_ok=True)
            self._model = YOLOWorld(str(self.weights))
            self.device = "mps" if torch.backends.mps.is_available() else "cpu"
        if phrase != self._phrase:  # the words are encoded once, then reused
            self._model.set_classes([phrase])
            self._phrase = phrase
        # The model takes BGR, as OpenCV gives it.
        found = self._model.predict(
            image[:, :, ::-1], conf=self.threshold, verbose=False, device=self.device
        )[0].boxes
        boxes = []
        for index in np.argsort(-found.conf.cpu().numpy())[:KEEP]:
            x0, y0, x1, y1 = (round(float(v), 4) for v in found.xyxyn[index])
            boxes.append(Box(phrase, round(float(found.conf[index]), 3), x0, y0, x1, y1))
        return boxes


CLAUDE_MODEL = "claude-sonnet-5-5"
ENV_FILE = Path(".env")
_FOUND = {
    "type": "object",
    "properties": {
        "visible": {"type": "boolean"},
        "box": {"type": "array", "items": {"type": "integer"}},
        "confidence": {"type": "number"},
        "note": {"type": "string"},
    },
    "required": ["visible", "box", "confidence", "note"],
    "additionalProperties": False,
}
_ASK = """This is the view from a camera mounted low on the front of a powered wheelchair. \
The picture is {width} pixels wide and {height} pixels high.

The rider asked: "{said}"
The thing to find: {phrase}

Say whether that thing is visible in the picture. If it is, give one tight box around it as \
[left, top, right, bottom] in pixels of this picture, and your confidence from 0 to 1. If \
several things match, box the one the rider most likely means. If it is not visible, set \
visible to false, box to [0, 0, 0, 0] and confidence to 0.

In note, write one short sentence: what you boxed and how you recognised it, or, if it is not \
visible, where in this scene it would be most worth looking next (for example "through the \
doorway on the left")."""


def claude_key() -> str | None:
    """The Claude API key: from the environment, or from ml/.env (which git ignores)."""
    if key := os.environ.get("ANTHROPIC_API_KEY"):
        return key
    try:
        for line in ENV_FILE.read_text().splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "ANTHROPIC_API_KEY" and value.strip():
                return value.strip().strip("\"'")
    except OSError:
        pass
    return None


class ClaudeDetector:
    """Claude looks at the frame: slower than a detector, but it knows what almost anything is.

    About a second or two a look, so it runs on its own clock beside the driving model. As well
    as a box it says, when the thing is not in view, where it would look next.
    """

    period_s = 1.0

    def __init__(self, model: str = CLAUDE_MODEL, api_key: str | None = None) -> None:
        self.model, self._api_key = model, api_key
        self._client: Any = None
        self.note: str | None = None
        # The whole request, when the caller has it; the phrase alone otherwise.
        self.said: str | None = None
        # The camera's own JPEG for this frame, when there is one: sharper than the model's copy.
        self.jpeg: bytes | None = None

    def find(self, image: np.ndarray, phrase: str) -> list[Box]:
        import anthropic

        if self._client is None:
            self._client = anthropic.Anthropic(
                api_key=self._api_key or claude_key(), timeout=20.0, max_retries=1
            )
        sent, (width, height) = original_or_copy(self.jpeg, image)
        picture = base64.standard_b64encode(sent).decode()
        ask = _ASK.format(width=width, height=height, said=self.said or phrase, phrase=phrase)
        response = self._client.messages.create(
            model=self.model,
            max_tokens=1024,
            # A quick look, not a long think: this runs about once a second.
            thinking={"type": "between_tools"},
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": _FOUND}},
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": picture,
                            },
                        },
                        {"type": "text", "text": ask},
                    ],
                }
            ],
        )
        if response.stop_reason != "end_turn":
            self.note = f"Claude gave no answer ({response.stop_reason})"
            return []
        found = json.loads(next(b.text for b in response.content if b.type == "text"))
        self.note = found["note"]
        return boxes_from_claude(found, phrase, width, height)


def original_or_copy(jpeg: bytes | None, image: np.ndarray) -> tuple[bytes, tuple[int, int]]:
    """The picture to send, with its size: the camera's own JPEG when it shows the same view.

    The model's copy is cropped to 16:9; a camera frame of another shape would put boxes in
    the wrong place, so then the copy is sent, at full JPEG quality.
    """
    height, width = image.shape[:2]
    if jpeg:
        try:
            with Image.open(io.BytesIO(jpeg)) as original:
                size = original.size
            if abs(size[0] / size[1] - width / height) < 0.02:
                return jpeg, size
        except OSError:
            pass
    copy = io.BytesIO()
    Image.fromarray(image).save(copy, format="JPEG", quality=95)
    return copy.getvalue(), (width, height)


def boxes_from_claude(found: dict[str, Any], phrase: str, width: int, height: int) -> list[Box]:
    """Claude's answer as a box in fractions of the picture, or nothing if it is not a real box."""
    box = found.get("box") or []
    if not found.get("visible") or len(box) != 4:
        return []
    x0, x1 = sorted((box[0] / width, box[2] / width))
    y0, y1 = sorted((box[1] / height, box[3] / height))
    x0, y0, x1, y1 = (round(min(1.0, max(0.0, v)), 4) for v in (x0, y0, x1, y1))
    if x1 - x0 < 0.005 or y1 - y0 < 0.005:
        return []
    score = round(min(1.0, max(0.0, float(found.get("confidence", 0.0)))), 3)
    return [Box(phrase, score, x0, y0, x1, y1)]


def best_detector() -> Any:
    """Claude when there is a key for it; otherwise YOLO-World, otherwise OWL-ViT.

    ``FSD_DETECTOR`` (claude, yolo or owl) picks one outright.
    """
    wanted = os.environ.get("FSD_DETECTOR", "").lower()
    has_yolo = importlib.util.find_spec("ultralytics") and importlib.util.find_spec("clip")
    if wanted == "owl":
        return Detector()
    if wanted == "yolo" and has_yolo:
        return YoloDetector()
    if wanted in ("", "claude") and importlib.util.find_spec("anthropic") and claude_key():
        return ClaudeDetector()
    return YoloDetector() if has_yolo else Detector()


# A find this sure locks the target; below this the tracker has lost it.
LOCK_SCORE = 0.5
LOST_SCORE = 0.45
LOST_FOR_S = 1.0
# While locked, the detector is asked again this often, to correct the tracker's drift.
VERIFY_S = 8.0
TRACK_PERIOD_S = 0.1
# Small things are tracked together with some of what is around them.
MIN_PATCH_PX = 36


class TemplateTracker:
    """Follows one boxed thing from frame to frame by matching how it looks.

    Plain template matching in a window around where the thing last was, at three sizes so
    the box can grow as the chair gets closer. The match score says when it has been lost.
    """

    def __init__(self, image: np.ndarray, box: Box) -> None:
        import cv2

        self._cv2 = cv2
        gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
        height, width = gray.shape
        x0, y0, x1, y1 = box.x0 * width, box.y0 * height, box.x1 * width, box.y1 * height
        # The patch that is matched: the box, padded out if the box is small.
        pad_x = max(0.0, (MIN_PATCH_PX - (x1 - x0)) / 2)
        pad_y = max(0.0, (MIN_PATCH_PX - (y1 - y0)) / 2)
        self._patch = [x0 - pad_x, y0 - pad_y, x1 + pad_x, y1 + pad_y]
        # Where the box sits inside the patch, as fractions, to get the box back after a match.
        pw, ph = self._patch[2] - self._patch[0], self._patch[3] - self._patch[1]
        self._inner = (pad_x / pw, pad_y / ph, 1 - pad_x / pw, 1 - pad_y / ph)
        self._template = self._cut(gray, self._patch)
        self.label, self.box = box.label, box
        # The first look after a lock searches the whole picture: the find took a while.
        self._wide = True

    def _cut(self, gray: np.ndarray, patch: list[float]) -> np.ndarray:
        height, width = gray.shape
        x0, y0 = max(0, round(patch[0])), max(0, round(patch[1]))
        x1, y1 = min(width, round(patch[2])), min(height, round(patch[3]))
        return gray[y0:y1, x0:x1].copy()

    def update(self, image: np.ndarray) -> tuple[Box, float]:
        """Where the thing is in this frame, and how well it matched (1 is perfect)."""
        cv2 = self._cv2
        gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
        height, width = gray.shape
        px0, py0, px1, py1 = self._patch
        pw, ph = px1 - px0, py1 - py0
        reach = max(width, height) if self._wide else max(pw, ph) * 0.9 + 12
        sx0, sy0 = max(0, round(px0 - reach)), max(0, round(py0 - reach))
        sx1, sy1 = min(width, round(px1 + reach)), min(height, round(py1 + reach))
        window = gray[sy0:sy1, sx0:sx1]
        best = (-1.0, 0, 0, self._template)
        for scale in (1.0,) if self._wide else (0.94, 1.0, 1.06):
            th = max(8, round(self._template.shape[0] * scale))
            tw = max(8, round(self._template.shape[1] * scale))
            if th > window.shape[0] or tw > window.shape[1]:
                continue
            template = self._template if scale == 1.0 else cv2.resize(self._template, (tw, th))
            scores = cv2.matchTemplate(window, template, cv2.TM_CCOEFF_NORMED)
            _, top, _, at = cv2.minMaxLoc(scores)
            if top > best[0]:
                best = (float(top), at[0], at[1], template)
        score, ax, ay, template = best
        if score < LOST_SCORE:
            return self.box, max(score, 0.0)
        self._wide = False
        th, tw = template.shape
        self._patch = [sx0 + ax, sy0 + ay, sx0 + ax + tw, sy0 + ay + th]
        if score > 0.75:
            # Let the look of the thing change slowly as the chair moves round it.
            fresh = self._cut(gray, self._patch)
            if fresh.shape == template.shape:
                self._template = cv2.addWeighted(template, 0.85, fresh, 0.15, 0)
            else:
                self._template = template
        i0, j0, i1, j1 = self._inner
        x0, y0 = self._patch[0] + i0 * tw, self._patch[1] + j0 * th
        x1, y1 = self._patch[0] + i1 * tw, self._patch[1] + j1 * th
        edges = (x0 / width, y0 / height, x1 / width, y1 / height)
        fx0, fy0, fx1, fy1 = (round(min(1.0, max(0.0, v)), 4) for v in edges)
        self.box = Box(self.label, self.box.score, fx0, fy0, fx1, fy1)
        return self.box, score


class Watcher:
    """Finds the target in the background, then locks on and follows it.

    The detector is slow and is asked on its own thread. Once it finds the target surely
    enough, a tracker follows that box on every frame, so the box keeps up with the chair.
    While locked the detector is only asked now and then, to correct drift; if the tracker
    loses the target, or the detector twice says it is gone, the lock is dropped and the
    search starts again. Driving never waits on any of this.
    """

    def __init__(
        self,
        look: Callable[[], tuple[Any, ...]],
        detector: Any = None,
        period_s: float = 0.4,
        measure: Callable[[Box, float], float | None] | None = None,
        background: bool = True,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._look, self._detector = look, detector or best_detector()
        self._period_s = getattr(self._detector, "period_s", period_s)
        self._measure, self._background, self._clock = measure, background, clock
        self._latest: dict[str, Any] = {"target": None, "boxes": [], "ms": None, "problem": None}
        self._stop = threading.Event()
        self._guard = threading.Lock()
        self._phrase: str | None = None
        self._tracker: TemplateTracker | None = None
        self._locked_at = self._asked_at = 0.0
        self._lost_since: float | None = None
        self._asking = False
        self._misses = 0
        self._found: dict[str, Any] = {}

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def latest(self) -> dict[str, Any]:
        return self._latest

    def unlock(self, why: str | None = None) -> None:
        """Drop the lock and look for the target afresh."""
        with self._guard:
            self._tracker, self._lost_since, self._misses = None, None, 0
            self._asked_at = 0.0
            if why:
                self._found = {**self._found, "note": why}

    def _measured(self, box: Box, aspect: float) -> dict[str, Any]:
        distance = self._measure(box, aspect) if self._measure else box.distance_m
        return Box(box.label, box.score, box.x0, box.y0, box.x1, box.y1, distance).as_dict()

    def _publish(self, phrase: str, boxes: list[Box], aspect: float, **more: Any) -> None:
        self._latest = {
            "target": phrase,
            "boxes": [self._measured(b, aspect) for b in boxes],
            "detector": type(self._detector).__name__,
            "at": round(time.time(), 3),
            "problem": None,
            "locked": False,
            **self._found,
            **more,
        }

    def once(self) -> None:
        """One pass: follow the lock if there is one, and ask the detector when it is due."""
        # The frame, the thing to find, and optionally the whole request and the camera's JPEG.
        image, phrase, said, jpeg = (*self._look(), None, None)[:4]
        if image is None or not phrase:
            self.unlock()
            self._phrase, self._found = phrase, {}
            self._latest = {"target": phrase, "boxes": [], "ms": None, "problem": None}
            return
        if phrase != self._phrase:
            self.unlock()
            self._phrase, self._found = phrase, {}
        now, aspect = self._clock(), image.shape[1] / image.shape[0]
        with self._guard:
            tracker = self._tracker
            if tracker is not None:
                box, score = tracker.update(image)
                if score >= LOST_SCORE:
                    self._lost_since = None
                    self._publish(
                        phrase,
                        [box],
                        aspect,
                        locked=True,
                        track_score=round(score, 2),
                        locked_for_s=round(now - self._locked_at, 1),
                    )
                else:
                    self._lost_since = self._lost_since or now
        if tracker is not None and self._lost_since and now - self._lost_since > LOST_FOR_S:
            self.unlock("Lost sight of it; looking again.")
            self._publish(phrase, [], aspect)
        wait = VERIFY_S if self._tracker is not None else self._period_s
        if not self._asking and now - self._asked_at >= wait:
            self._asking, self._asked_at = True, now
            asked = (image, phrase, said, jpeg, aspect)
            if self._background:
                threading.Thread(target=self._ask, args=asked, daemon=True).start()
            else:
                self._ask(*asked)

    def _ask(
        self, image: np.ndarray, phrase: str, said: str | None, jpeg: bytes | None, aspect: float
    ) -> None:
        """Ask the detector about one frame, and lock on if it is sure."""
        started = time.perf_counter()
        try:
            if hasattr(self._detector, "said"):
                self._detector.said, self._detector.jpeg = said, jpeg
            boxes = self._detector.find(image, phrase)
        except Exception as error:  # noqa: BLE001  a broken detector must not stop the viewer
            self._latest = {**self._latest, "problem": str(error)}
            return
        finally:
            self._asking = False
        if phrase != self._phrase:
            return  # the rider has asked for something else since
        self._found = {
            "ms": round((time.perf_counter() - started) * 1000.0, 1),
            "note": getattr(self._detector, "note", None),
        }
        if boxes and boxes[0].score >= LOCK_SCORE:
            with self._guard:
                if self._tracker is None:
                    self._locked_at = self._clock()
                self._tracker = TemplateTracker(image, boxes[0])
                self._misses, self._lost_since = 0, None
            self._publish(phrase, boxes[:1], aspect, locked=True, track_score=1.0, locked_for_s=0.0)
        elif self._tracker is not None:
            self._misses += 1
            if self._misses >= 2:
                self.unlock()
                self._publish(phrase, boxes, aspect)
        else:
            self._publish(phrase, boxes, aspect)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.once()
            except Exception as error:  # noqa: BLE001  tracking must not stop the viewer
                self._latest = {"target": None, "boxes": [], "ms": None, "problem": str(error)}
                self._stop.wait(2.0)
            self._stop.wait(TRACK_PERIOD_S)
