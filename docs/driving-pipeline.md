# Driving pipeline

What the driving service does between receiving sensor data and sending a command. Design only; none of this is built yet. For which models and why, see [model.md](model.md). For what comes out, see [outputs.md](outputs.md).

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
- Chair reports it is in self-driving mode, joystick untouched, e-stop not pressed.
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
| Driver inference | to be measured in the bake-off |
| Oldest model result the loop will act on | to be set from that measurement |
| Chair stops with no fresh command after | a few hundred ms, enforced on the chair |

## What needs building

| Piece | State | Depends on |
|-------|-------|------------|
| Phone stream, freshness, heading, recording | Built | |
| Landscape, ultra-wide, 20 fps capture | In progress | |
| Speed and motion tracking source | In progress | ARKit check |
| Route sent to the service | To build. Today the iPad plans the route and the service only holds the destination point | |
| Localize | To build | Motion tracking |
| Goal conversion and outdoor/indoor switch | To build | Route, localize |
| Frame buffer | To build | |
| Model worker and the shared interface | To build | Bake-off result, a GPU |
| Obstacle and distance model | To choose, then build | |
| Footprint check | To build | Chair dimensions, obstacle model |
| Controller and limits | To build | |
| Trip state machine, engage and disengage | To build | |
| Chair link | Blocked | Opening up the chair |
| Replay: run the whole pipeline on a recording | To build | Recordings |

Replay comes first. It lets every stage be developed and tested against recorded drives before the chair can be commanded at all.

## Where the code goes

Two places. What runs while driving goes in the service. What runs offline (the bake-off, post-training, evaluation) goes in [`ml/`](../ml), which only reads recordings and never imports the service.

Both use the same model interface: frames, a goal and the chair's motion in; scored candidate paths out. It exists today as `Driver.propose()` in [`ml/fsd_ml/drivers.py`](../ml/fsd_ml/drivers.py), so a model wrapped for the bake-off can move into the service unchanged.

Inside the existing `service/drive_service/` package (not created yet):

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
