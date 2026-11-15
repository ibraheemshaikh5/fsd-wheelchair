"""Serve OmniVLA from a Modal GPU.

The first start downloads a 16 GB checkpoint into a
Modal volume, which takes several minutes; later starts reuse it.

    uv run --with modal modal setup                    # once, to log in
    uv run --with modal modal run modal_omnivla.py::download   # fetch the checkpoint, no GPU
    uv run --with modal modal deploy modal_omnivla.py          # permanent URL

Then, from this folder:

    uv run python -m fsd_ml.console video.mp4 --driver remote:https://<your-url>

The URL is public. To require a token, create a Modal secret named
``fsd-driver`` holding FSD_DRIVER_TOKEN, add ``secrets=[modal.Secret.from_name("fsd-driver")]``
below, and set the same variable where the viewer runs.
"""

import modal

WEIGHTS_PATH = "/weights"
CHECKPOINT = f"{WEIGHTS_PATH}/omnivla-original"
CODE = "/opt/OmniVLA"

app = modal.App("fsd-omnivla")

# Only what running the model needs, at OmniVLA's own pinned versions. Its full
# dependency list (TensorFlow, dataset tooling) is for training and does not
# resolve with pip; four package __init__ files that pull those in are emptied.
image = (
    modal.Image.debian_slim(python_version="3.10")
    .apt_install("git")
    .uv_pip_install(
        "numpy==1.26.4",
        "torch==2.2.0",
        "torchvision==0.17.0",
        "timm==0.9.10",
        "tokenizers==0.19.1",
        "sentencepiece==0.1.99",
        "transformers @ git+https://github.com/moojink/transformers-openvla-oft.git",
        "huggingface_hub==0.29.1",
        "accelerate>=0.25.0",
        "peft==0.11.1",
        "einops",
        "protobuf",
        "rich",
        "pillow>=10",
        "httpx>=0.27",
        "fastapi>=0.110",
        "uvicorn>=0.30",
    )
    .run_commands(
        f"git clone --depth 1 https://github.com/NHirose/OmniVLA.git {CODE}",
        f"cd {CODE}/prismatic && : > __init__.py && : > vla/__init__.py"
        " && : > training/__init__.py && : > models/__init__.py",
    )
    # Parallel downloads: the checkpoint is 16 GB and crawls without this.
    .uv_pip_install("hf_transfer")
    .env(
        {
            "PYTHONPATH": CODE,
            "TOKENIZERS_PARALLELISM": "false",
            "HF_HUB_ENABLE_HF_TRANSFER": "1",
        }
    )
    .add_local_python_source("fsd_ml")
)

# Imported where the handlers below can see it by name: FastAPI looks the types up.
with image.imports():
    from fastapi import FastAPI, HTTPException, Request

weights = modal.Volume.from_name("fsd-weights", create_if_missing=True)


def fetch_checkpoint() -> None:
    """Download whatever of the checkpoint is missing. Quick when it is all there."""
    from huggingface_hub import snapshot_download

    from fsd_ml.omnivla import HF_REPO

    weights.reload()
    snapshot_download(HF_REPO, local_dir=CHECKPOINT)
    weights.commit()


@app.function(image=image, volumes={WEIGHTS_PATH: weights}, timeout=3600)
def download() -> None:
    """Fetch the checkpoint without renting a GPU: ``modal run modal_omnivla.py::download``."""
    fetch_checkpoint()


def web_app(checkpoint_dir):
    """The driver's HTTP face (fsd_ml/remote.py) over the checkpoint in ``checkpoint_dir``."""
    from fsd_ml import remote
    from fsd_ml.omnivla import OmniVLADriver

    driver = OmniVLADriver(checkpoint_dir)
    web = FastAPI()

    def check(request: Request) -> None:
        if not remote.authorized(request.headers.get("Authorization")):
            raise HTTPException(status_code=401, detail="bad or missing token")

    @web.get("/info")
    def info(request: Request) -> dict:
        check(request)
        return remote.info(driver)

    @web.post("/propose")
    async def propose(request: Request) -> dict:
        check(request)
        return remote.handle(driver, await request.json())

    return web


@app.function(
    image=image,
    # 24 GB. The model is about 16 GB in memory; move to "L40S" (48 GB) if it runs out.
    gpu="A10G",
    volumes={WEIGHTS_PATH: weights},
    # Raise to 1 during a demo so a container is always up.
    min_containers=0,
    scaledown_window=900,
    # Covers the first download and loading the model.
    timeout=3600,
)
@modal.concurrent(max_inputs=2)
@modal.asgi_app()
def serve():
    from pathlib import Path

    fetch_checkpoint()
    return web_app(Path(CHECKPOINT))
