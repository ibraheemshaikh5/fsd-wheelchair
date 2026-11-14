# Outputs

What is sent out on each pass of the [driving pipeline](driving-pipeline.md). All three are produced today by the pipeline in [`ml/`](../ml) running against a simulated chair ([`outputs.py`](../ml/fsd_ml/pipeline/outputs.py)). None is connected to the real chair or the iPad yet.

There are three outputs. Only the first moves the chair.

| Output | To | Rate | Purpose |
|--------|----|------|---------|
| Drive command | Chair | 20 Hz | Move the chair |
| Drive state | iPad | 10–20 Hz | Show what the chair is doing and why |
| Decision record | Disk | 20 Hz | Debugging and post-training |

## 1. Drive command (to the chair)

A speed and a turn rate. The model's paths never leave the service; the chair only ever receives these two numbers.

```json
{
  "type": "drive",
  "seq": 48213,
  "t": 1794650000123,
  "v_mps": 0.6,
  "w_radps": -0.15,
  "ttl_ms": 200
}
```

| Field | Meaning |
|-------|---------|
| `seq` | Counts up by one. The chair ignores a command older than the last one it applied |
| `t` | When the service made the decision, ms |
| `v_mps` | Forward speed in metres per second. Never negative for now (no reversing) |
| `w_radps` | Turn rate in radians per second. Positive turns left |
| `ttl_ms` | How long the command is valid. After that with nothing newer, the chair stops |

Rules:

- **Stopping is a command too**: `v_mps` 0 and `w_radps` 0, sent every pass while stopped.
- **Silence means stop.** The chair enforces `ttl_ms` itself.
- **The chair caps speed** on its own, whatever the service sends.
- **The chair turns the two numbers into whatever its controller takes**, such as a joystick signal. That mapping lives on the chair.

The chair reports back so the service knows it is being obeyed:

```json
{
  "type": "chair",
  "t": 1794650000140,
  "mode": "auto",
  "joystick_active": false,
  "estop": false,
  "battery_pct": 82,
  "applied_seq": 48213
}
```

`mode` is `auto` or `manual`. Touching the joystick switches to `manual` on the chair first, then reports it.

The transport (serial, Bluetooth or WebSocket) is undecided until the chair is opened up.

## 2. Drive state (to the iPad)

The iPad already defines the shape it renders: `DriveState` in [`app/src/types.ts`](../app/src/types.ts). Today the app derives most of it from the sensor status and never shows a planned path or obstacles. Once the pipeline runs, the service fills these fields itself:

| Field | From stage | Meaning |
|-------|-----------|---------|
| `mode` | Trip state | `idle`, `ready`, `driving`, `arrived`, `manual` |
| `speedMps`, `headingDeg`, `position` | Localize | Where the chair is and how it is moving |
| `path` | Choose path | The chosen path as points `{x, z}`: metres right and metres ahead of the chair |
| `pathStatus` | Choose path | `clear`, `slowing` or `blocked` |
| `obstacles` | Obstacles | `{id, kind, x, z}` with kind `pedestrian`, `vehicle` or `object` |
| `yieldingToId` | Choose path | The obstacle the chair is slowing or waiting for |
| `reason` | Choose path | One short line, e.g. "Waiting for pedestrian" |
| `takeOver` | Gate, Choose path | Set when the rider must take the joystick. The chair is stopped while set |
| `engageBlocked` | Gate | Why self-driving cannot be started right now |
| `progressM`, `remainingM`, `etaSeconds`, `nextStep` | Goal | Trip progress |
| `batteryPct` | Chair status | |

It is sent as a `drive` block on the existing `/ws/viewer` status message, so the iPad keeps one connection.

One direction difference to handle in code: the pipeline works in "ahead and left", the iPad draws "right and ahead". The conversion happens once, where the state is built.

## 3. Decision record (to disk)

One line per pass, written alongside the existing recording. This is what makes a drive replayable and what post-training learns from.

| Field | Why |
|-------|-----|
| Frame `seq` and time the decision used | Ties the decision to the exact camera frame |
| Position, heading, speed, turn rate | What the chair believed about itself |
| Goal given to the model | What it was asked to do |
| Model name and version, inference time, result age | Which model decided, and how stale it was |
| All candidate paths with scores | What the model proposed |
| Which path was chosen, and why others were rejected | What the footprint check did |
| Obstacles | What the service thought was there |
| The drive command sent | What actually went to the chair |
| Chair status | Whether the chair obeyed |
| Stop or take-over reason | Why it stopped, if it did |

Every moment the rider takes over is marked. Those moments, with the path the rider then drove, are the corrections the driver model is fine-tuned on.

## Commands the iPad sends in

For completeness. These already exist as the `DrivingService` interface in [`app/src/services/drivingService.ts`](../app/src/services/drivingService.ts).

| Command | Effect |
|---------|--------|
| Set destination | Outdoor trip. Needs to carry the route, not only the end point |
| Set language goal | Indoor trip from a spoken instruction |
| Engage | Start or resume self-driving |
| Disengage | Stop and hold; the trip is kept |
| Clear destination | End the trip |
