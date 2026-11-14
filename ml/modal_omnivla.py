"""Serve OmniVLA from a Modal GPU.

Not yet run against Modal. The first start downloads a 16 GB checkpoint into a
Modal volume, which takes several minutes; later starts reuse it.

    uv run --with modal modal setup                    # once, to log in
    uv run --with modal modal serve modal_omnivla.py   # temporary URL, live reload
    uv run --with modal modal deploy modal_omnivla.py  # permanent URL

Then, from this folder:

    uv run python -m fsd_ml.latency https://<your-url>
    uv run python -m fsd_ml.viewer video.mp4 --driver remote:https://<your-url>

The URL is public. To require a token, create a Modal secret named
``fsd-driver`` holding FSD_DRIVER_TOKEN, add ``secrets=[modal.Secret.from_name("fsd-driver")]``
below, and set the same variable where the pipeline runs.
"""

from __future__ import annotations

import modal

WEIGHTS_PATH = "/weights"
CHECKPOINT = f"{WEIGHTS_PATH}/omnivla-original"
CODE = "/opt/OmniVLA"

app = modal.App("fsd-omnivla")

# OmniVLA's own pins (its SETUP.md and pyproject.toml). Its packaging has a typo
# that breaks a plain install, so the dependencies are listed here instead.
image = (
    modal.Image.debian_slim(python_version="3.10")
    .apt_install("git")
    .pip_install("numpy==1.26.4", "torch==2.2.0", "torchvision==0.17.0", "torchaudio==2.2.0")
    .pip_install(
        "accelerate>=0.25.0",
        "draccus==0.8.0",
        "einops",
        "huggingface_hub==0.29.1",
        "json-numpy",
        "jsonlines",
        "peft==0.11.1",
        "protobuf",
        "rich",
        "sentencepiece==0.1.99",
        "timm==0.9.10",
        "tokenizers==0.19.1",
        "transformers @ git+https://github.com/moojink/transformers-openvla-oft.git",
        "tensorflow==2.15.0",
        "tensorflow_datasets==4.9.3",
        "tensorflow_graphics==2021.12.3",
        "dlimp @ git+https://github.com/moojink/dlimp_openvla",
        "diffusers==0.11.1",
        "wandb",
        "matplotlib",
        "imageio",
        "utm",
        "fastapi[standard]",
        "httpx>=0.27",
        "pillow>=10",
    )
    .run_commands(
        f"git clone --depth 1 https://github.com/NHirose/OmniVLA.git {CODE}",
        f"pip install --no-deps -e {CODE}",
    )
    .env({"HF_HUB_ENABLE_HF_TRANSFER": "0", "TOKENIZERS_PARALLELISM": "false"})
    .add_local_python_source("fsd_ml")
)

weights = modal.Volume.from_name("fsd-weights", create_if_missing=True)


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

    from fastapi import FastAPI, HTTPException, Request
    from huggingface_hub import snapshot_download

    from fsd_ml import remote
    from fsd_ml.omnivla import HF_REPO, OmniVLADriver

    checkpoint = Path(CHECKPOINT)
    if not (checkpoint / "config.json").is_file():
        snapshot_download(HF_REPO, local_dir=checkpoint)
        weights.commit()
    driver = OmniVLADriver(checkpoint)

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
