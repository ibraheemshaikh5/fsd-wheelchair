"""OmniVLA (Berkeley, ICRA 2026) as a driver: one camera frame and a goal in, one path out.

The goal can be a point, a spoken instruction, a photo of the destination, or a
point combined with either. That makes it usable outdoors (a point from GPS)
and indoors (an instruction or a photo).

It needs a GPU with about 20 GB of memory and OmniVLA's own code on the Python
path, so it runs on Modal (modal_omnivla.py) and is called through
``remote:<url>``. The loading and forward pass follow OmniVLA's
``inference/run_omnivla.py`` (MIT licence, github.com/NHirose/OmniVLA).

Not yet run: nothing here has been executed against the real model.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
from PIL import Image

from fsd_ml.drivers import Candidate, Goal, Motion

HF_REPO = "NHirose/omnivla-original"
# The model's waypoints and goal positions are in units of this many metres.
WAYPOINT_SPACING_M = 0.1
# Goal points further than this are pulled in, as in OmniVLA's own script.
MAX_GOAL_M = 30.0
NO_INSTRUCTION = "xxxx"

# Which goals the model is told to use. The numbers are OmniVLA's own.
MODALITY = {
    # (point, photo, instruction): id
    (True, False, False): 4,
    (True, True, False): 5,
    (False, True, False): 6,
    (False, False, True): 7,
    (True, False, True): 8,
}


def modality_id(goal: Goal) -> int:
    key = (goal.point_xy is not None, goal.image is not None, bool(goal.instruction))
    if key == (False, False, False):
        raise ValueError("OmniVLA needs a goal: a point, a photo or an instruction")
    if key not in MODALITY:
        # A photo with an instruction was not a combination it was trained on.
        key = (key[0], False, True)
    return MODALITY[key]


def goal_pose(point_xy: tuple[float, float] | None) -> np.ndarray:
    """(ahead, left) metres as the model's [ahead, left, cos, sin], in waypoint units."""
    ahead, left = point_xy or (0.0, 0.0)
    distance = math.hypot(ahead, left)
    if distance > MAX_GOAL_M:
        ahead, left = ahead * MAX_GOAL_M / distance, left * MAX_GOAL_M / distance
    # No preferred heading on arrival: the same as the chair's now.
    return np.array([ahead / WAYPOINT_SPACING_M, left / WAYPOINT_SPACING_M, 1.0, 0.0])


def waypoints_to_path(waypoints: np.ndarray) -> np.ndarray:
    """The model's (8, 4) waypoints as a path in metres, (ahead, left)."""
    return np.asarray(waypoints, dtype=np.float64)[:, :2] * WAYPOINT_SPACING_M


def implied_speed(path_xy: np.ndarray) -> float:
    """The speed the waypoints imply, the way OmniVLA's own script reads them.

    It takes how far ahead the fifth waypoint is and treats that as the distance
    for one decision (a third of a second), up to 0.5 m/s. Waypoints bunched at
    the chair therefore mean "stop".
    """
    ahead_m = float(path_xy[min(4, len(path_xy) - 1), 0])
    return float(np.clip(ahead_m * OmniVLADriver.decision_hz, 0.0, 0.5))


class OmniVLADriver:
    name = "omnivla"
    context_frames = 1
    frame_hz = 20.0
    # OmniVLA's own script runs at 3 Hz.
    decision_hz = 3.0

    def __init__(self, checkpoint_dir: Path) -> None:
        import torch
        from prismatic.extern.hf.configuration_prismatic import OpenVLAConfig
        from prismatic.extern.hf.modeling_prismatic import OpenVLAForActionPrediction_MMNv1
        from prismatic.extern.hf.processing_prismatic import (
            PrismaticImageProcessor,
            PrismaticProcessor,
        )
        from prismatic.models.action_heads import L1RegressionActionHead_idcat
        from prismatic.models.projectors import ProprioProjector
        from prismatic.vla.action_tokenizer import ActionTokenizer
        from prismatic.vla.constants import ACTION_DIM, POSE_DIM
        from transformers import (
            AutoConfig,
            AutoImageProcessor,
            AutoModelForVision2Seq,
            AutoProcessor,
        )

        self._torch = torch
        self._device = torch.device("cuda:0")
        self.provider = torch.cuda.get_device_name(0)
        path = str(checkpoint_dir)

        AutoConfig.register("openvla", OpenVLAConfig)
        AutoImageProcessor.register(OpenVLAConfig, PrismaticImageProcessor)
        AutoProcessor.register(OpenVLAConfig, PrismaticProcessor)
        AutoModelForVision2Seq.register(OpenVLAConfig, OpenVLAForActionPrediction_MMNv1)

        self._processor = AutoProcessor.from_pretrained(path, trust_remote_code=True)
        vla = AutoModelForVision2Seq.from_pretrained(
            path, torch_dtype=torch.bfloat16, low_cpu_mem_usage=True
        )
        # Two images go in: the current view and the goal photo.
        vla.vision_backbone.set_num_images_in_input(2)
        self._vla = vla.to(dtype=torch.bfloat16, device=self._device).eval()

        def load(module, stem: str, half: bool):
            (file,) = sorted(checkpoint_dir.glob(f"{stem}--*_checkpoint.pt"))[-1:]
            state = torch.load(file, map_location="cpu")
            module.load_state_dict({k.removeprefix("module."): v for k, v in state.items()})
            if half:
                module = module.to(torch.bfloat16)
            return module.to(self._device).eval()

        projector_stem = (
            "pose_projector"
            if list(checkpoint_dir.glob("pose_projector--*_checkpoint.pt"))
            else "proprio_projector"
        )
        self._pose_projector = load(
            ProprioProjector(llm_dim=vla.llm_dim, proprio_dim=POSE_DIM), projector_stem, False
        )
        self._action_head = load(
            L1RegressionActionHead_idcat(
                input_dim=vla.llm_dim, hidden_dim=vla.llm_dim, action_dim=ACTION_DIM
            ),
            "action_head",
            True,
        )
        self._action_tokenizer = ActionTokenizer(self._processor.tokenizer)
        backbone = vla.vision_backbone
        # One extra token carries the goal position.
        self._patches = backbone.get_num_patches() * backbone.get_num_images_in_input() + 1

    def _batch(self, current: Image.Image, goal_image: Image.Image, instruction: str):
        """The prompt, with placeholder action tokens for the model to fill in."""
        from prismatic.models.backbones.llm.prompting import PurePromptBuilder
        from prismatic.vla.constants import IGNORE_INDEX

        torch = self._torch
        placeholder = np.random.rand(8, 4)
        actions = self._action_tokenizer(placeholder[0]) + "".join(
            self._action_tokenizer(placeholder[1:])
        )
        asked = (
            "No language instruction"
            if instruction == NO_INSTRUCTION
            else f"What action should the robot take to {instruction}?"
        )
        builder = PurePromptBuilder("openvla")
        builder.add_turn("human", asked)
        builder.add_turn("gpt", actions)

        tokenizer = self._processor.tokenizer
        input_ids = torch.tensor(tokenizer(builder.get_prompt(), add_special_tokens=True).input_ids)
        labels = input_ids.clone()
        labels[: -(len(actions) + 1)] = IGNORE_INDEX
        transform = self._processor.image_processor.apply_transform
        pixels = torch.cat((transform(current), transform(goal_image)), dim=0)
        return input_ids[None], labels[None], pixels[None]

    def propose(self, frames: list[np.ndarray], goal: Goal, motion: Motion) -> list[Candidate]:
        from prismatic.training.train_utils import get_current_action_mask, get_next_actions_mask
        from prismatic.vla.constants import ACTION_DIM, NUM_ACTIONS_CHUNK

        torch, device = self._torch, self._device
        current = Image.fromarray(frames[-1])
        # A goal the model is told to ignore still needs something in its slot.
        goal_image = Image.fromarray(goal.image) if goal.image is not None else current
        input_ids, labels, pixels = self._batch(
            current, goal_image, goal.instruction or NO_INSTRUCTION
        )
        modality = torch.as_tensor([modality_id(goal)], dtype=torch.bfloat16, device=device)
        pose = torch.from_numpy(goal_pose(goal.point_xy))[None].to(torch.bfloat16).to(device)

        with torch.no_grad(), torch.autocast("cuda", dtype=torch.bfloat16):
            output = self._vla(
                input_ids=input_ids.to(device),
                attention_mask=torch.ones_like(input_ids, dtype=torch.bool).to(device),
                pixel_values=pixels.to(torch.bfloat16).to(device),
                modality_id=modality,
                labels=labels.to(device),
                output_hidden_states=True,
                proprio=pose,
                proprio_projector=self._pose_projector,
                noisy_actions=None,
                noisy_action_projector=None,
                diffusion_timestep_embeddings=None,
                use_film=False,
            )
            truth = labels[:, 1:].to(device)
            mask = get_current_action_mask(truth) | get_next_actions_mask(truth)
            text_states = output.hidden_states[-1][:, self._patches : -1]
            action_states = (
                text_states[mask].reshape(1, NUM_ACTIONS_CHUNK * ACTION_DIM, -1).to(torch.bfloat16)
            )
            waypoints = self._action_head.predict_action(action_states, modality)

        path = waypoints_to_path(waypoints.float().cpu().numpy()[0])
        return [Candidate(path, score=1.0, speed_mps=implied_speed(path))]
