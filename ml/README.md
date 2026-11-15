# ml

The model side of the chair. One program, the **console**, runs on a laptop: it reads the phone's camera from the driving service, asks a model where to go, and sends stick commands to the chair's board.

```
iPhone -> drive_service -> console (this folder) -> OmniVLA on Modal, or the Claude pilot
                              |
                              +-> chair_link.ChairSender -> hardware/wheelchair.py (UDP) -> board
```

It imports nothing from `service/`. The laptop must be on the team Tailscale to reach the board (`WHEELCHAIR_HOST`, default `wheelchair-unoq`).

## Layout

```
fsd_ml/
  console/       the page and its server: watch a model, drive the chair, the bench
  chair_link.py   the one sender of stick commands; the 30% stick cap lives here
  moves.py     supervised runs: a straight, a turn, the pilot's moves
  pilot.py       Claude Sonnet picks moves from the camera picture (pilot_prompt.md)
  gemini_pilot.py  Gemini picks one of the numbered paths drawn on the picture (gemini_pilot_prompt.md)
  candidates.py    those paths: a fan the chair fits along, drawn on the camera's JPEG
  bench.py       the bench's runs, and the settings every way of driving reads
  runlog.py      the record of a run: every command with its camera frame
  live.py        the phone's camera, gyro and iPad goal, from the driving service
  obstacles.py   optional: obstacles from a depth model, current frame only
  pipeline/      model path -> footprint check -> speed and turn rate
  drivers.py     the Driver interface, and the "fan" stand-in
  omnivla.py     OmniVLA; runs on a GPU
  remote.py      call a model on another machine over HTTP
  chair.py       where the camera sits and how big the chair is (chair.json)
  video.py       put any video into the form the models are fed
  recording.py   read a drive recorded by the driving service
modal_omnivla.py serve OmniVLA from a Modal GPU
runs/            output (ignored by git)
```

## Setup

```bash
cd ml && uv sync
```

```bash
uv run pytest
```

The obstacle sense needs torch and is optional. Without it the console runs the same and nothing checks for obstacles.

```bash
uv sync --group detect
```

## OmniVLA on Modal

OmniVLA is too large for a laptop, so it runs on a Modal GPU.

```bash
uv run --with modal modal setup
```

Fetch the 16 GB checkpoint once (no GPU), then deploy:

```bash
uv run --with modal modal run modal_omnivla.py::download
```

```bash
uv run --with modal modal deploy modal_omnivla.py
```

Deploy prints the model's address. Each Modal account gets its own. To require a token, see the top of [`modal_omnivla.py`](modal_omnivla.py) and set `FSD_DRIVER_TOKEN` where the console runs.

A decision that takes longer than 1.5 s is given up, and one whose frame is over 1 s old by the time the reply lands is not driven on. A sleeping container therefore shows "the model did not answer in time" until it has woken.

## Run the console

```bash
uv run --env-file .env python -m fsd_ml.console --driver remote:https://<your-modal-url>
```

`.env` holds `ANTHROPIC_API_KEY=...` for the pilot (git ignores it). Without the pilot, leave `--env-file .env` out. The console remembers the model and the driving service it last used, so later starts need no options. It needs `ffmpeg` only for video files.

On the page (`http://127.0.0.1:8200`):

1. Choose **Live from chair**, enter the driving service's address (`http://127.0.0.1:8000`, or its Tailscale address) and Connect.
2. Pick the goal: a point, a typed instruction, a photo, or whatever the rider set on the iPad.
3. **Send to chair** sends each decision to the chair, optionally for a set number of steps. **Drive with W A S D** drives by hand. **STOP** or Esc stops everything.

To use the console from an iPad or phone on the tailnet (e.g. STOP from a tablet), publish it from the laptop:

```bash
tailscale serve --bg --https=8445 8200      # then open https://<laptop>.<tailnet>.ts.net:8445
tailscale serve --https=8445 off            # to stop publishing
```

Keep it tailnet-only: never use `tailscale funnel` or a public tunnel, since this page drives the chair.

What keeps it safe:

- **A command lasts 600 ms.** If the model, the page or the laptop stops, the chair stops.
- **Old pictures are not driven on.** A frame over 500 ms old stops the chair.
- **The joystick wins.** Pushing the chair's own joystick, or a Wi-Fi dropout, switches sending off until a person switches it on again.
- **Only the console's own page can move the chair.** Requests from other sites, or from scripts, are refused.
- **The stick is capped at 30%**, and the model never reverses.

### How a model's path becomes a stick

One route, in this order: the model's path, the footprint check ([`pipeline/footprint.py`](fsd_ml/pipeline/footprint.py)), `follow` ([`pipeline/control.py`](fsd_ml/pipeline/control.py)), `to_stick` and `send_stick` ([`chair_link.py`](fsd_ml/chair_link.py)).

- **Speed is the model's own.** As in OmniVLA's script, it is how far ahead the fifth waypoint is, per third of a second, up to 0.5 m/s. Nothing ramps it here: the board eases the stick itself.
- **Steering has a 4° dead zone.** The joystick's own dead zone makes the smallest turn a real one, so a target nearly straight ahead is driven straight at.
- **With the obstacle sense on**, a path that meets something slows or stops the chair, and no sight of the floor stops it.

### A video file

To check a model away from the chair, drop a forward-facing video on the page or pass its path:

```bash
uv run python -m fsd_ml.console path/to/video.mp4
```

It shows what the model wants on each frame. The video does not react to the decisions.

## The bench

`http://127.0.0.1:8200/bench` has the set runs and the settings.

- **Straight** and **quarter turns** drive a fixed move. A turn is measured by the phone's gyro and stops if the gyro stops reporting.
- **Claude drives to a named thing** is the pilot: Claude Sonnet sees the camera's picture and answers with moves (forward up to 1 m, back up to 0.4 m, turns up to 120°), which are carried out as the same straights and turns. Its instructions are in [`pilot_prompt.md`](pilot_prompt.md), read afresh for every look.
- **Gemini drives to a named thing** is the Gemini pilot ([`gemini_pilot.py`](fsd_ml/gemini_pilot.py)): each look, up to seven paths the chair fits along are drawn and numbered on the camera's picture, and Gemini Robotics-ER picks one, or a turn (60° left or right, or around), or says it has arrived or gives up. It is never asked for metres or degrees. A picked path is followed by the gyro; the chair stands still while Gemini decides. Its instructions are in [`gemini_pilot_prompt.md`](gemini_pilot_prompt.md). To see its picks on recorded frames without the chair: `uv run --env-file .env python -m fsd_ml.gemini_pilot --say "go to the stairs" <frame.jpg>`.
- **A run needs the bench page open.** If the page has not asked after the run for 2 s (closed, asleep, laptop lid shut), the run ends and the chair stops.
- **Settings** are on that page and in `runs/console/bench/settings.json`: stick strengths, ramp, braking, what full stick gives in m/s and rad/s (`v_full_mps`, `w_full_radps`), and how much of the model's stick is sent (`model_speed_pct`). They apply to every way of driving, at once.

## Run logs

Each connection to the driving service makes a folder, `runs/console/live/<date-time>/`, with `log.jsonl` and `frames/`. Every way of driving writes one `command` line per stick command or decision:

| Field | What |
|-------|------|
| `t` | when the line was written, unix seconds |
| `mode` | `manual`, `bench`, `model`, `pilot` or `gemini` |
| `frame`, `frame_n`, `frame_t` | the camera's own JPEG the command was given on (path in the run folder), its number, and when the phone took it |
| `stick` | `[x, y]` percent sent, x right, y forward; `null` if nothing was sent |
| `sent`, `problem` | whether it reached the chair, and why not |
| `model` | what the model said: its path, speed and goal, or the pilot's tool call |
| `board` | the board's telemetry at that moment |

- **Keyboard and bench moves** send a command about ten times a second; their frames are saved at most ten times a second, so neighbouring lines can share one.
- **While a person drives with the live view open**, the model's decisions are recorded too (`mode: model`, `sent: false`), so what it would have done sits beside what they did.
- **Other lines** (`connected`, `sending on`, `stop`, `waiting`, `run`, `run finished`) mark what happened around the commands.

To sum up the newest run:

```bash
uv run python -m fsd_ml.runlog
```

## Camera and chair measurements

[`chair.json`](chair.json) holds where the camera sits and how big the chair is, in metres. The model is never told any of it; it is for the code around the model: paths are shifted from the lens to the middle of the chair before the footprint check, and the page draws paths through the same camera. Edit the file and restart the console.

## Environment variables

| Variable | For |
|----------|-----|
| `ANTHROPIC_API_KEY` | the pilot; put it in `ml/.env` and start with `uv run --env-file .env` |
| `GEMINI_API_KEY` | the Gemini pilot; also in `ml/.env` |
| `GEMINI_PILOT_MODEL`, `GEMINI_PILOT_THINKING` | its model (default `gemini-robotics-er-2-preview`) and thinking level (`low`, `medium`, `high`; default `low`) |
| `FSD_DRIVER_TOKEN` | only if the Modal endpoint was deployed with a token |
| `WHEELCHAIR_HOST` | the board's address, if not `wheelchair-unoq` |

## Adding a model

1. Write a class with `name`, `context_frames`, `frame_hz` and `propose()`. See [`fsd_ml/omnivla.py`](fsd_ml/omnivla.py).
2. Serve it from a GPU with `remote.info()` and `remote.handle()`, as [`modal_omnivla.py`](modal_omnivla.py) does.
3. Use it as `remote:<url>`.
