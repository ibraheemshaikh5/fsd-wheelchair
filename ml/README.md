# ml

Offline model work: the bake-off, post-training and evaluation. Nothing here runs while the chair drives; that code lives in [`service/`](../service). The plan is in [docs/model.md](../docs/model.md).

## Rules

- **Recordings are the only input.** This project reads the folders the service writes ([format](../docs/phone-protocol.md#recordings)) and never imports the service.
- **Weights stay out of git.** They download into `ml/weights/`.
- **Every model sits behind one interface**, `Driver.propose()` in [`fsd_ml/drivers.py`](fsd_ml/drivers.py).

## Layout

```
fsd_ml/
  recording.py   read a recorded drive: frames, GPS, heading, IMU
  drivers.py     the Driver interface, and a straight-ahead baseline
  bakeoff.py     run a driver over a recording, save what it proposed
  weights.py     download checkpoints
tests/
weights/         downloaded checkpoints (ignored)
runs/            bake-off output (ignored)
```

Post-training and evaluation get their own modules here when that work starts.

## Use

```bash
cd ml && uv sync
```

```bash
uv run pytest
```

Run a driver over a recording:

```bash
uv run python -m fsd_ml.bakeoff ../recordings/20261114T150000Z --driver straight
```

List the checkpoints that can be downloaded, then fetch one:

```bash
uv run --group weights python -m fsd_ml.weights
```

```bash
uv run --group weights python -m fsd_ml.weights s2e
```

## Adding a model

1. Write a class with `name`, `context_frames` and `propose()` that wraps the model.
2. Register it in `DRIVERS` in `fsd_ml/drivers.py`.
3. Add its runtime as a dependency group in `pyproject.toml`, so the base install stays small.

## Not here yet

- Real model drivers. Only the straight-ahead baseline exists.
- Drawing predicted paths on the video.
- Scoring against the path the chair actually took, which needs motion tracking ([open question 7](../docs/open-questions.md)).
