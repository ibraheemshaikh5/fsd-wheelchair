# ml

The model side of the chair: the driving pipeline, a simulator to run it in, the model behind one interface, and the tools to watch and measure it. The plan is in [docs/model.md](../docs/model.md) and [docs/driving-pipeline.md](../docs/driving-pipeline.md).

Nothing here touches the driving service, the iPad app or the phone page. The pipeline is built and tested here against a simulated chair, and plugs into the service once it is ready.

## Rules

- **It imports nothing from `service/`.** Recordings and the message shapes in [docs/outputs.md](../docs/outputs.md) are the only contract.
- **No model runs on this machine.** OmniVLA runs on a Modal GPU and its weights live in a Modal volume. Only two stand-ins, `fan` and `straight`, run here.
- **Every model sits behind one interface**, `Driver.propose()` in [`fsd_ml/drivers.py`](fsd_ml/drivers.py), wherever it runs.

## Layout

```
fsd_ml/
  pipeline/      sensor data in, drive command out
    loop.py        one pass: gate, localize, goal, model, choose path, control
    route.py       a route and the chair's progress along it
    footprint.py   pick a path the chair fits along
    control.py     path to speed and turn rate
    outputs.py     the chair command, iPad state and decision record
  sim/           a virtual chair and scenarios to drive the pipeline through
  drivers.py     the Driver interface; "straight" and "fan" stand-ins
  omnivla.py     OmniVLA, the model for everything; runs on a GPU
  viewer/        a page for watching a model decide on a video
  video.py       put any video into the form the models are fed
  chair.py       where the camera sits on the chair, and the chair's size
  live.py        the chair's phone, live, by way of the driving service
  remote.py      call a driver on another machine over HTTP; serve one
  latency.py     measure the round trip to a served driver
  recording.py   read a recorded drive
  bakeoff.py     run a driver over a recording
modal_omnivla.py serve OmniVLA from a Modal GPU
tests/
runs/            output (ignored)
```

## Setup

```bash
cd ml && uv sync
```

```bash
uv run pytest
```

## Simulate

Runs the whole pipeline against a virtual chair and phone. Nine scenarios: clear, pedestrian, crossing, stuck, dropout, joystick, estop, slow-model, dead-model.

```bash
uv run python -m fsd_ml.sim.run
```

```bash
uv run python -m fsd_ml.sim.run crossing
```

Each run prints a timeline and writes to `runs/sim/<scenario>/`:

| File | What |
|------|------|
| `commands.jsonl` | what would go to the chair |
| `drive_state.jsonl` | what would go to the iPad |
| `decisions.jsonl` | the decision record |
| `track.png` | top-down picture of the drive |

The simulated camera is blank. The default `fan` driver ignores the camera and heads for the route, so the simulator tests the pipeline: data flow, stops, timing, path choice. It does not test how well a model drives: a real model fed blank frames wanders off.

## Watch a model on a video

Open the viewer. It needs `ffmpeg`.

```bash
uv run python -m fsd_ml.viewer
```

It starts on the `fan` stand-in. Pick OmniVLA from the Model menu, or start on it with `--driver remote:https://<your-url>` ([OmniVLA on Modal](#omnivla-on-modal)).

Drop a forward-facing video on the page, or pass its path on the command line. For each frame it shows every path the model proposes, the one the footprint check chose drawn at the chair's width, and the speed and turn rate that would go to the chair. Space plays and pauses; the arrow keys step one decision.

- **Any video is converted first** to what the models are fed: 20 frames a second, 16:9 landscape, 640×360, upright. A taller or wider picture has its middle cut out rather than being squashed. The page shows what was done, and warns about portrait or low-frame-rate clips. The step is [`fsd_ml/video.py`](fsd_ml/video.py).
- **The video does not react to the decisions.** You see what the model wants, not what would happen.
- **Nothing is rejected yet.** With no obstacle model, the chosen path is always the model's top one.
- **The goal is a point you set** with the sliders, since a video has no GPS.
- **The camera sliders only line up the drawing.** They do not change what the model sees.
- **The Model menu at the top** shows the model in use and where it is running, and switches between hosted models without losing your place in the video. Its last entry, "Add a hosted model…", takes a hosted model's address. Those are remembered in `runs/viewer/hosted.json`.
- **Goal given as** switches between a point, a typed instruction, and a photo. For a photo, scrub to the place you want to reach and choose "Use this frame as the goal photo". Only OmniVLA reads instructions and photos.

## Watch a model on the chair's live camera

The viewer can also show the phone's stream as it arrives. It reads the driving service's model feed (`/ws/model`), which pushes every frame the moment it lands, with its capture time, so the model sees each frame as soon as it can and picks frames by when they were taken. The service allows two model feeds at once, apart from the iPad's viewers.

1. Start the viewer and choose **Live from chair**.
2. Enter the driving service's address and press Connect: `http://127.0.0.1:8000` if the service runs on this machine, or its Tailscale `https://…ts.net` address if it runs on another.

A banner at the top of the live view shows what the rider asked for on the iPad, in their words or as a map destination, and whether the model is following it.

A new request on the iPad goes to the model straight away: an indoor instruction is passed as words, and an outdoor trip becomes a point 10 m further along the route Google planned, worked out from the chair's GPS and heading. To try something else, set **Goal given as** to another type; the next request from the iPad takes over again.

The same overlays appear on the live picture, plus **Camera delay** (phone to service) and how many decisions a second the model is managing. Frames go through the same conversion as uploaded videos. The feed is 10 frames a second, so a model that wants a 20-per-second history gets the nearest frames instead.

To try it with no phone, run the service and its fake phone from `service/`:

```bash
uv run python -m drive_service
```

```bash
uv run python -m scripts.fake_phone --seconds 120
```

The code is [`fsd_ml/live.py`](fsd_ml/live.py). Its `to_sensors()` turns the same feed into the pipeline's input, which is the first half of driving for real.

**Send to chair** passes each live decision (speed and turn rate only) to the chair through `Wheelchair().set(x, y)` ([hardware/README.md](../hardware/README.md)); the viewer's machine must be on the team Tailscale. A decision is held for 600 ms, so if the model stops deciding, the chair stops. Unticking, reconnecting or leaving the live view stops it at once, and a joystick takeover or Wi-Fi dropout unticks it. Code: [`fsd_ml/chair_out.py`](fsd_ml/chair_out.py).

## Camera and chair measurements

The model is never told where the camera is; it only gets the picture. The measurements are for the code around it, and live in [`chair.json`](chair.json):

- **The model's paths start at the lens.** They are shifted to start at the middle of the chair before the footprint check, so a camera on a front corner does not make the chair hug one side.
- **The viewer draws paths through the same camera**, so the overlay lands on the floor where it should.
- **The footprint check uses the chair's real width and length.**

With the camera mounted and someone sitting in the chair on a flat floor, measure in centimetres and run:

```bash
uv run python -m fsd_ml.chair --height 32 --offset 27 --side left --from-rear 90 --tilt 3 --wall-width 190 --chair-width 62 --chair-length 105
```

| Option | Measure |
|--------|---------|
| `--height` | Floor to the centre of the lens |
| `--offset`, `--side` | From the chair's centre line (midway between the big wheels) to the lens, and which side |
| `--from-rear` | From the chair's rearmost point forward to the lens |
| `--tilt` | Degrees below level; rest a phone's level on top of the camera |
| `--wall-width` | From exactly 1 m, square to a wall: how wide a stretch of wall the picture shows |
| `--chair-width`, `--chair-length` | Widest point across; footplate tip to rearmost point |

Any one can be changed alone later. Run it with no options to see what is saved; it questions numbers that look like a wrong unit. Restart the viewer afterwards. Until this is run the file holds placeholders.

## OmniVLA on Modal

OmniVLA is 8B parameters and 16 GB, too large for a laptop, so it runs on a Modal GPU and is used as a remote driver. Measured on a 24 GB NVIDIA A10 from a laptop: about 255 ms per decision there and back, of which about 200 ms is the model and about 55 ms the network. A stopped container takes about 16 seconds to wake.

```bash
uv run --with modal modal setup
```

Fetch the 16 GB checkpoint once (about three minutes, no GPU), then deploy:

```bash
uv run --with modal modal run modal_omnivla.py::download
```

```bash
uv run --with modal modal deploy modal_omnivla.py
```

Then, with the address it prints:

```bash
uv run python -m fsd_ml.latency https://<your-url>
```

```bash
uv run python -m fsd_ml.viewer --driver remote:https://<your-url>
```

Each Modal account that deploys this gets its own address and downloads the checkpoint once. To use a teammate's account, log in as them (or switch Modal profile), deploy, and add the new address in the viewer. At about $1.40 an hour for this GPU, $30 of credit is roughly 21 hours.

What to expect, from OmniVLA's own script:

- **It decides about 3 times a second** and looks at one frame, not a history.
- **Its paths are short**, around a metre, and its script caps speed at 0.3 m/s. Our controller's 1 m/s cruise is likely too fast for it until we have seen its real output.
- **One path per decision**, so the footprint check can slow or stop the chair but not pick a way round.

## Try the remote interface without a GPU

Serve a stand-in locally, measure it, and drive through it:

```bash
uv run python -m fsd_ml.remote --driver fan --port 8100
```

```bash
uv run python -m fsd_ml.latency http://127.0.0.1:8100
```

```bash
uv run python -m fsd_ml.sim.run clear --driver remote:http://127.0.0.1:8100 --real-timing
```

## Run a model over a recording

```bash
uv run python -m fsd_ml.bakeoff ../recordings/20261114T150000Z --driver straight
```

## Adding a model

1. Write a class with `name`, `context_frames`, `frame_hz` and `propose()` that wraps the model. See [`fsd_ml/omnivla.py`](fsd_ml/omnivla.py).
2. Serve it from a GPU with `remote.info()` and `remote.handle()`, as [`modal_omnivla.py`](modal_omnivla.py) does.
3. Use it as `remote:<url>`. Its runtime is installed where it is served, not here.

## Known gaps

- **No obstacle model.** The simulator hands the pipeline perfect obstacle positions. Stage 6 is still unchosen.
- **No realistic camera in the simulator**, so model quality is untested.
- **Position is trusted as given.** There is no filtering of GPS or heading noise yet.
- **Chair and camera measurements are placeholders** until `fsd_ml.chair` is run with real ones.
- **The baseline driver cuts corners.** It aims straight at a point 6 m along the route.
- **A path's "going around" label can flicker** for a few passes while the chair passes an obstacle.
- **OmniVLA's driving quality is unjudged.** It runs and answers every goal type, but has only been looked at briefly on live frames.
