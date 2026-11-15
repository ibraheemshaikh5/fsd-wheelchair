"""Post-train OmniVLA on our own drives, on a Modal GPU, and serve the result.

    # 1. Samples from a recording (see fsd_ml/posttrain_export.py), into runs/posttrain/dataset.
    # 2. Train. The run carries on after this command is stopped; the log line gives its page.
    uv run --with modal modal run --detach modal_posttrain.py::train
    uv run --with modal modal app logs fsd-posttrain            # watch it, or read its log:
    uv run --with modal modal volume get fsd-weights posttrain/stairs-v0/log.jsonl -

    # 3. Score the saved checkpoint, loaded the way the console's server loads it.
    uv run --with modal modal run modal_posttrain.py::score

    # 4. Serve it at its own URL, next to (not instead of) the original model.
    uv run --with modal modal deploy modal_posttrain.py
    uv run python -m fsd_ml.console --driver remote:https://<the serve url>

The dataset folder is copied into the job when it starts, so step 2 trains on
whatever step 1 last wrote.
"""

from pathlib import Path

import modal

from modal_omnivla import CHECKPOINT, WEIGHTS_PATH, fetch_checkpoint, web_app, weights
from modal_omnivla import image as omnivla_image

RUN = "stairs-v0"
RUNS = f"{WEIGHTS_PATH}/posttrain"
DATASET = "/dataset"
STEPS = 300
# Takes 52 GB of the GPU's 80.
BATCH_SIZE = 8

app = modal.App("fsd-posttrain")

image = omnivla_image.add_local_python_source("modal_omnivla").add_local_dir(
    Path(__file__).parent / "runs/posttrain/dataset", DATASET
)


@app.function(
    image=image,
    gpu=["H100", "A100-80GB"],
    volumes={WEIGHTS_PATH: weights},
    timeout=3 * 3600,
)
def train(
    steps: int = STEPS, batch_size: int = BATCH_SIZE, learning_rate: float = 1e-4, run: str = RUN
) -> None:
    from fsd_ml import posttrain

    fetch_checkpoint()
    result = posttrain.train(
        Path(CHECKPOINT),
        Path(DATASET),
        Path(RUNS, run),
        steps=steps,
        batch_size=batch_size,
        learning_rate=learning_rate,
        on_progress=weights.commit,
    )
    weights.commit()
    print("held-out stretch, after training:", result)


# 48 GB: both models are in memory at the end.
@app.function(image=image, gpu="L40S", volumes={WEIGHTS_PATH: weights}, timeout=3600)
def score(run: str = RUN) -> None:
    """The held-out stretch, by the original model and by a saved post-trained one."""
    from fsd_ml import posttrain
    from fsd_ml.omnivla import OmniVLADriver

    fetch_checkpoint()
    held_out = posttrain.read_samples(Path(DATASET), "val")
    for name, folder in (("original", CHECKPOINT), (run, f"{RUNS}/{run}")):
        print(name, posttrain.evaluate(OmniVLADriver(Path(folder)), held_out))


@app.function(
    image=image,
    gpu="A10G",
    volumes={WEIGHTS_PATH: weights},
    scaledown_window=900,
    timeout=3600,
)
@modal.concurrent(max_inputs=2)
@modal.asgi_app()
def serve():
    return web_app(Path(RUNS, RUN))
