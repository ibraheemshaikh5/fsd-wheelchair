# Driving pipeline

What happens between receiving sensor data and sending a command. Stages 1–5 and 7–9 are built in [`ml/`](../ml) and run against a simulated chair; they are not yet connected to the driving service, the phone or the real chair. For which models and why, see [model.md](model.md). For what comes out, see [outputs.md](outputs.md).

## Stages

One pass runs 20 times a second. Any stage can end the pass with **stop**.

```
 phone stream ──▶ 1 Gate ──▶ 2 Localize ──▶ 3 Goal ──▶ 4 Frames ──▶ 5 Model
                                                                       │
 chair ◀── 9 Send ◀── 8 Control ◀── 7 Choose path ◀── 6 Obstacles ◀───┘
 iPad  ◀──────┘
```

| # | Stage | Does | In | Out |
|---|-------|------|----|-----|
| 1 | Gate | Refuses to drive on bad data | Sensor snapshot, chair status | go, or stop with a reason |
| 2 | Localize | Works out where the chair is and how it is moving | GPS, heading, motion tracking, gyro | Position, heading, speed, turn rate |
| 3 | Goal | Turns the trip into something the model can follow | Route or instruction, chair position | Outdoors: the route ahead, relative to the chair. Indoors: the instruction or goal image |
| 4 | Frames | Prepares camera input | JPEG frames | Rolling buffer of resized frames |
| 5 | Model | Proposes where to drive | Frames, goal, speed | Candidate paths with scores |
| 6 | Obstacles | Finds what is in the way and how far | Latest frame | Obstacles in the chair's frame, with distance |
| 7 | Choose path | Picks a path the chair physically fits through | Candidate paths, obstacles, chair footprint | One path, or none, plus status and reason |
| 8 | Control | Turns the path into motion | Chosen path, current speed | Speed and turn rate, within limits |
| 9 | Send | Publishes the result | Everything above | Chair command, iPad state, log record |

### 1. Gate

Stops unless all of these hold:

- Phone connected, clock synced, camera frame and position fresh. The service already computes this as `sensors_ok` ([phone protocol](phone-protocol.md)).
- Chair reports self-driving is on and not taken back: `Wheelchair.stats()` has `takeover` and `dropout_latched` false. The e-stop is the chair's power button, so a pressed one shows as the board going quiet.
- The model's last result is recent enough (stage 5).

Indoors there is no GPS, so the freshness rule there uses motion tracking in place of a GPS fix.

### 2. Localize

Combines the sources into one estimate: GPS for position, gyro for moment-to-moment turning, GPS direction or compass to correct drift, motion tracking for speed. The service already picks a best heading; this stage extends that to a full estimate.

### 3. Goal

- **Outdoors**: finds the chair's progress along the route, then converts the next stretch from latitude/longitude into metres ahead and to the side of the chair. Heading errors matter most here.
- **Indoors or last metres**: passes the instruction ("go to the elevator") or a goal image.
- **Which one**: outdoors while GPS is good and the route has distance left; otherwise indoors. The rider can override on the iPad.

### 4. Frames

Decodes each JPEG, resizes to the model's input size, and keeps the last few seconds in a buffer. Frames arriving out of order or late are dropped.

### 5. Model

Runs on the GPU in its own worker so a slow inference never stalls the loop. Each result is tagged with the time of the frame it was computed from. Both models sit behind one interface:

```
propose(frames, goal, motion) -> candidate paths, each a list of (ahead_m, left_m) points with a score
```

- **FlowPilot** returns several scored candidates per call.
- **OmniVLA** returns one. It is slower, so its results are older when used.

If the newest result is older than a set limit, stage 1 stops the chair.

### 6. Obstacles

Estimates distance to what the camera sees, and labels people, vehicles and objects. This is a separate model from the drivers and is **not chosen yet**. It exists so the footprint check does not depend on the driver model being right, and it feeds the obstacle markers on the iPad.

### 7. Choose path

Sweeps the chair's rectangle, plus a margin, along each candidate in score order. Takes the first that touches nothing.

| Result | Meaning | Path status |
|--------|---------|-------------|
| Best candidate is clear | Drive normally | `clear` |
| Clear, but something is close or moving into the way | Reduce speed | `slowing` |
| No candidate is clear | Stop and wait; ask the rider to take over if it lasts | `blocked` |

Also records what the chair is yielding to and a one-line reason.

### 8. Control

Follows the chosen path by steering toward a point a short distance along it. Then applies, in order: the speed cap, limits on acceleration and turn rate so the ride is smooth, and a slow-down proportional to how old the model result is.

### 9. Send

See [outputs.md](outputs.md).

## Trip state

A small state machine sits above the loop and matches the iPad's `DriveMode`:

```
idle ──set destination──▶ ready ──engage──▶ driving ──reached──▶ arrived
                            ▲                  │
                            └──disengage / stop┘        joystick at any time ──▶ manual
```

The loop only produces motion in `driving`. In every other state it still runs stages 1–7 so the iPad shows what the chair sees, but sends zero speed.

## Timing

| Item | Target |
|------|--------|
| Loop rate | 20 Hz |
| Camera frame to service | about 40–100 ms (measured indoors) |
| Driver inference | S2E: about 240 ms on a Mac CPU. GPU and other models: not yet measured |
| Oldest model result the loop will act on | to be set from that measurement |
| Chair stops with no fresh command after | a few hundred ms, enforced on the chair |

## What needs building

| Piece | State | Depends on |
|-------|-------|------------|
| Phone stream, freshness, heading, recording | Built | |
| Landscape, ultra-wide, 20 fps capture | In progress | |
| Speed and motion tracking source | In progress | ARKit check |
| Route sent to the service | The pipeline accepts the iPad's route shape. Sending it from the iPad is not wired; today the service only holds the destination point | |
| Gate | Built, in simulation | |
| Localize | Built, but trusts GPS and heading as given. No filtering | Motion tracking |
| Goal conversion | Built for routes. The outdoor/indoor switch is not | |
| Frame buffer | Built, thinned to each model's frame rate | |
| Model interface, local and remote | Built. S2E wrapped and running; FlowPilot and OmniVLA are not | A GPU for OmniVLA |
| Modal endpoint | Written, not yet run on Modal | A Modal login |
| Obstacle and distance model | To choose, then build. The simulator supplies perfect obstacles | |
| Footprint check | Built, with placeholder chair dimensions | Chair dimensions, obstacle model |
| Controller and limits | Built | |
| Trip state machine, engage and disengage | Built | |
| Simulated chair and scenarios | Built: nine scenarios | |
| Connection to the driving service | Not started | The service side settling |
| Chair link | Built: the live view drives `Wheelchair.set(x, y)` (`ml/fsd_ml/chair_out.py`). Full-scale speed and turn rate are placeholders | Measuring on the ground |
| Replay: run the whole pipeline on a recording | To build | Recordings |

The simulator came first. It lets every stage be developed and tested before the chair can be commanded at all. See [`ml/README.md`](../ml/README.md) to run it.

## Where the code goes

For now everything is in [`ml/`](../ml), which imports nothing from the service: the pipeline in `ml/fsd_ml/pipeline/`, the simulator in `ml/fsd_ml/sim/`, and the models behind `Driver.propose()` in [`ml/fsd_ml/drivers.py`](../ml/fsd_ml/drivers.py). Keeping it apart let it be built without touching the service while the phone stream was being finished.

To drive for real, the service calls the pipeline once per pass with its sensor snapshot and sends on what comes back. Whether the pipeline then moves into the service package, as sketched below, or stays importable from `ml/` is still open:

```
drive_service/
  pipeline/
    loop.py         the 20 Hz pass and stage order
    gate.py         stage 1
    localize.py     stage 2
    goal.py         stage 3
    frames.py       stage 4
    obstacles.py    stage 6
    footprint.py    stage 7
    control.py      stage 8
    trip.py         trip state machine
  models/
    base.py         the shared propose() interface
    flowpilot.py
    omnivla.py
    worker.py       GPU worker, result timestamps
  chair/
    link.py         commands out, chair status in
  replay.py         run the pipeline on a recording
```

## Settings to decide

Chair width, length and safety margin. Speed cap. Acceleration and turn-rate limits. Oldest usable model result. How long `blocked` lasts before asking the rider to take over. GPS quality below which the service switches to indoor mode.
