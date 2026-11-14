"""Serve a driver model from a Modal GPU.

Not yet run against Modal: check it with ``modal serve`` before relying on it.

    uv run --with modal modal setup                 # once, to log in
    uv run --with modal modal serve modal_app.py    # temporary URL, live reload
    uv run --with modal modal deploy modal_app.py   # permanent URL

Then, from this folder:

    uv run python -m fsd_ml.latency https://<your-url>
    uv run python -m fsd_ml.sim.run clear --driver remote:https://<your-url>

The URL is public. To require a token, create a Modal secret named
``fsd-driver`` holding FSD_DRIVER_TOKEN, add ``secrets=[modal.Secret.from_name("fsd-driver")]``
below, and set the same variable where the pipeline runs.
"""

import modal

DRIVER = "s2e"
WEIGHTS_PATH = "/weights"

app = modal.App("fsd-driver")

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install(
        "fastapi[standard]",
        "numpy>=2.0",
        "pillow>=10",
        "httpx>=0.27",
        "huggingface-hub>=0.25",
        # Falls back to CPU if the CUDA libraries are not found; /info reports which.
        "onnxruntime-gpu>=1.20",
    )
    .env({"FSD_WEIGHTS_DIR": WEIGHTS_PATH})
    .add_local_python_source("fsd_ml")
)

# Checkpoints are downloaded once and kept here between container starts.
# Imported where the handlers below can see it by name: FastAPI looks the types up.
with image.imports():
    from fastapi import FastAPI, HTTPException, Request

weights = modal.Volume.from_name("fsd-weights", create_if_missing=True)


@app.function(
    image=image,
    gpu="T4",
    volumes={WEIGHTS_PATH: weights},
    # Raise to 1 during a demo so a container is always up.
    min_containers=0,
    scaledown_window=600,
)
@modal.concurrent(max_inputs=4)
@modal.asgi_app()
def serve():

    from fsd_ml import remote
    from fsd_ml import weights as checkpoints
    from fsd_ml.drivers import make_driver

    if DRIVER in checkpoints.CHECKPOINTS:
        checkpoints.download(DRIVER)
        weights.commit()
    driver = make_driver(DRIVER)

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
