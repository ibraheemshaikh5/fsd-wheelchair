"""Post-train OmniVLA on our own drives (the samples posttrain_export.py writes).

The recipe is OmniVLA's own ``vla-scripts/train_omnivla.py`` cut down to what a
single small dataset needs: LoRA (rank 32) on every linear layer, the action
head and the goal-position projector trained in full, and the same loss on the
waypoints. Left out: several GPUs, wandb, and MBRA, the model OmniVLA uses to
invent labels for data that has none (ours has its own).

The forward pass is the driver's (``OmniVLADriver.predict``), and what is saved
is a folder the driver loads like the original checkpoint. It needs a GPU and
OmniVLA's code, so it runs on Modal (modal_posttrain.py).
"""

from __future__ import annotations

import json
import random
import shutil
import time
from pathlib import Path

import numpy as np
from PIL import Image

from fsd_ml.omnivla import WAYPOINT_SPACING_M, OmniVLADriver, goal_pose

# OmniVLA's number for "follow the instruction, ignore the goal photo and point".
LANGUAGE_ONLY = 7
LORA_RANK = 32
# How much of the picture a training crop may cut: OmniVLA's own augmentation.
CROP_SIDE, CROP_TOP = 0.1, 0.2
SMOOTH_WEIGHT = 0.1


def read_samples(dataset: Path, split: str) -> list[dict]:
    """The split's samples, each with its picture loaded and its waypoints in model units."""
    samples = []
    for line in (dataset / "samples.jsonl").read_text().splitlines():
        sample = json.loads(line)
        if sample["split"] != split:
            continue
        waypoints = np.array(sample["waypoints"], dtype=np.float32)
        waypoints[:, :2] /= WAYPOINT_SPACING_M
        with Image.open(dataset / sample["frame"]) as image:
            samples.append(
                {"task": sample["task"], "image": image.convert("RGB"), "waypoints": waypoints}
            )
    return samples


def crop(image: Image.Image) -> Image.Image:
    side = int(image.width * CROP_SIDE * random.random())
    top = int(image.height * CROP_TOP * random.random())
    box = (side, top, image.width - side, image.height - top)
    return image.crop(box).resize(image.size)


def batch(driver: OmniVLADriver, samples: list[dict], augment: bool):
    """What ``driver.predict`` takes for these samples, and the waypoints to match."""
    import torch
    from prismatic.vla.constants import IGNORE_INDEX
    from torch.nn.utils.rnn import pad_sequence

    pad = driver._processor.tokenizer.pad_token_id
    tokens = [driver.tokens(sample["task"]) for sample in samples]
    input_ids = pad_sequence([ids for ids, _ in tokens], batch_first=True, padding_value=pad)
    labels = pad_sequence(
        [labels for _, labels in tokens], batch_first=True, padding_value=IGNORE_INDEX
    )
    images = [crop(sample["image"]) if augment else sample["image"] for sample in samples]
    inputs = (
        input_ids,
        input_ids.ne(pad),
        labels,
        # No goal photo: the view now stands in its slot, as it does when driving.
        torch.stack([driver.pixels(image, image) for image in images]),
        torch.full((len(samples),), LANGUAGE_ONLY),
        torch.from_numpy(np.tile(goal_pose(None), (len(samples), 1))),
    )
    return inputs, torch.from_numpy(np.stack([sample["waypoints"] for sample in samples]))


def evaluate(driver: OmniVLADriver, samples: list[dict], batch_size: int = 16) -> dict:
    """How far the predicted waypoints are from the driven ones, over ``samples``.

    ``standing_*`` is the same error for a model that always answers "stay
    where you are": the number to beat.
    """
    import torch

    predicted = []
    with torch.no_grad():
        for i in range(0, len(samples), batch_size):
            inputs, _ = batch(driver, samples[i : i + batch_size], augment=False)
            predicted.append(driver.predict(*inputs).float().cpu().numpy())
    got = np.concatenate(predicted)
    want = np.stack([sample["waypoints"] for sample in samples])

    def off_m(guess: np.ndarray) -> np.ndarray:
        return np.linalg.norm(guess[..., :2] - want[..., :2], axis=-1) * WAYPOINT_SPACING_M

    def heading_deg(way: np.ndarray) -> np.ndarray:
        return np.degrees(np.arctan2(way[..., 3], way[..., 2]))

    still = np.zeros_like(want)
    still[..., 2] = 1.0
    return {
        "samples": len(samples),
        "mse": round(float(np.mean((got - want) ** 2)), 4),
        "waypoint_off_m": round(float(off_m(got).mean()), 3),
        "last_waypoint_off_m": round(float(off_m(got)[:, -1].mean()), 3),
        "last_heading_off_deg": round(
            float(np.abs(heading_deg(got) - heading_deg(want))[:, -1].mean()), 1
        ),
        "standing_mse": round(float(np.mean((still - want) ** 2)), 4),
        "standing_last_waypoint_off_m": round(float(off_m(still)[:, -1].mean()), 3),
        "standing_last_heading_off_deg": round(float(np.abs(heading_deg(want))[:, -1].mean()), 1),
    }


def train(
    base: Path,
    dataset: Path,
    out: Path,
    steps: int,
    batch_size: int,
    learning_rate: float = 1e-4,
    eval_every: int = 50,
    on_progress=lambda: None,
) -> dict:
    """Train from the checkpoint in ``base`` and leave one the driver can load in ``out``.

    ``on_progress`` is called after each line added to ``out/log.jsonl``.
    """
    import torch
    from peft import LoraConfig, get_peft_model

    random.seed(0)
    torch.manual_seed(0)
    train_set, val_set = read_samples(dataset, "train"), read_samples(dataset, "val")
    driver = OmniVLADriver(base)
    out.mkdir(parents=True, exist_ok=True)
    started = time.time()

    def log(row: dict) -> None:
        row = {"seconds": round(time.time() - started), **row}
        print(json.dumps(row), flush=True)
        with (out / "log.jsonl").open("a") as file:
            file.write(json.dumps(row) + "\n")
        on_progress()

    log({"step": 0, "train_samples": len(train_set), "val": evaluate(driver, val_set)})

    linear = [
        name for name, module in driver._vla.named_modules() if isinstance(module, torch.nn.Linear)
    ]
    driver._vla = get_peft_model(
        driver._vla,
        LoraConfig(
            r=LORA_RANK,
            lora_alpha=min(LORA_RANK, 16),
            lora_dropout=0.0,
            target_modules=linear,
            init_lora_weights="gaussian",
        ),
    )
    parts = (driver._vla, driver._action_head, driver._pose_projector)
    trainable = [p for part in parts for p in part.parameters() if p.requires_grad]
    optimizer = torch.optim.AdamW(trainable, lr=learning_rate)
    mse = torch.nn.functional.mse_loss

    order: list[dict] = []
    recent: list[float] = []
    for step in range(1, steps + 1):
        for part in parts:
            part.train()
        if len(order) < batch_size:
            order += random.sample(train_set, len(train_set))
        chosen, order = order[:batch_size], order[batch_size:]
        inputs, want = batch(driver, chosen, augment=True)
        got = driver.predict(*inputs).float()
        loss = mse(got, want.to(got.device)) + SMOOTH_WEIGHT * mse(got[:, :-1], got[:, 1:])
        loss.backward()
        optimizer.step()
        optimizer.zero_grad()
        recent.append(loss.item())

        if step % 10 == 0:
            row = {"step": step, "loss": round(float(np.mean(recent)), 4)}
            recent = []
            if step == 10:
                row["gpu_gb"] = round(torch.cuda.max_memory_allocated() / 2**30, 1)
            if step % eval_every == 0 or step == steps:
                for part in parts:
                    part.eval()
                row["val"] = evaluate(driver, val_set)
            log(row)

    for part in parts:
        part.eval()
    driver._vla = driver._vla.merge_and_unload()
    final = evaluate(driver, val_set)
    # How well it fits what it was shown, to set beside how well it carries over.
    seen = evaluate(driver, train_set[:: max(1, len(train_set) // 64)])
    driver._vla.save_pretrained(out)
    driver._processor.save_pretrained(out)
    # The checkpoint's config names its own model code; that has to come along.
    for code in base.glob("*.py"):
        shutil.copyfile(code, out / code.name)
    torch.save(driver._action_head.state_dict(), out / f"action_head--{steps}_checkpoint.pt")
    torch.save(driver._pose_projector.state_dict(), out / f"pose_projector--{steps}_checkpoint.pt")
    log({"step": steps, "saved": str(out), "val": final, "train": seen})
    return final
