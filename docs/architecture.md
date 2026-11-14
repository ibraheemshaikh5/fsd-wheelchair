# Architecture

## Loop

1. Phone streams camera frames and GPS fixes to the driving service.
2. Service plans a route to the destination (GPS waypoints).
3. Model takes the latest frames plus the next waypoint and outputs a short trajectory.
4. Service converts the trajectory to linear and angular velocity and sends it to the chair.
5. Service pushes state to the iPad: video, planned path, detected obstacles, speed, route progress.

Stage by stage: [driving pipeline](driving-pipeline.md). What is sent out: [outputs](outputs.md).

## Where it drives

Sidewalks and indoors.

- **Sidewalks**: GPS gives position and the route. The loop above applies as written.
- **Indoors**: no GPS. The destination has to be given another way, which is still [undecided](open-questions.md). The model and chair control stay the same; only steps 2 and 3 change.

## How the models fit together

Two models, one steering at a time. Everything they output passes through the same non-model checks before reaching the chair. See [model.md](model.md) for why these two.

```
iPad: destination ("coffee shop", or "go to the elevator")
        │
        ▼
  Route planner (not a model) ── Google Directions → GPS route
        │
        ▼
 ┌─────────────────── Driving service ───────────────────┐
 │                                                        │
 │  OUTDOORS                      INDOORS / LAST METRES   │
 │  FlowPilot                     OmniVLA                 │
 │  in: camera frames, speed,     in: camera frame +      │
 │      GPS route ahead               language or image   │
 │  out: candidate paths + scores     goal                │
 │                                out: path               │
 │            └──────────┬──────────────┘                 │
 │                       ▼                                │
 │   Footprint check (not a model): sweep the chair's     │
 │   rectangle along the path, reject paths that clip     │
 │                       ▼                                │
 │   Controller: path → speed + turn rate                 │
 └───────────────────────┬────────────────────────────────┘
                         ▼
        Chair interface (watchdog, speed cap, e-stop)
```

## Pieces

### Phone (sensors)
- Sends: camera video, GPS, heading (compass/IMU).
- Mounted forward-facing at a fixed, known height and angle.
- **Built:** an iPhone running a Safari page ([`web/capture`](../web/capture)), reaching the service over Tailscale. JPEG frames and JSON sensor messages over one WebSocket; see [phone protocol](phone-protocol.md) and [running](running.md).
- The service keeps the latest reading of each sensor with its age, counted from capture on the phone (or arrival, if that is older). Before the phone's clock is synced nothing counts as fresh. Anything that drives must check freshness first; stale data means stop.
- Not yet: authentication for the phone connection. The tailnet is the only boundary, so a pairing token is needed before a driving loop trusts the data ([open questions](open-questions.md)).
- Not yet: precise motion tracking (needs a native ARKit app or offline visual odometry), which fine-tuning will need for trajectory labels.

### Driving service (computer)
- Runs the model and the control loop.
- Owns all decisions. The phone, chair, and iPad are thin clients.
- One process on one machine for now. Moves onto the chair later.

### Chair interface
- An Arduino UNO Q pretends to be the WOLF joystick's MLX90393 magnet sensor, so the chair's own controller stays stock. See [hardware/README.md](../hardware/README.md).
- Any Tailscale device calls `Wheelchair().set(x, y)` ([`hardware/wheelchair.py`](../hardware/wheelchair.py)), stick percent, sent as UDP to a daemon on the board. The model's live view does this through [`ml/fsd_ml/chair_out.py`](../ml/fsd_ml/chair_out.py).

### iPad web app
- Tesla-FSD-style view: live rendering of the chair, path ahead, obstacles, speed, map with route.
- Inputs: set destination, engage, disengage.

## Safety

These live on the chair, not on the remote computer:

- **Watchdogs**: `Wheelchair` sends zeros 0.3 s after the last `set`; the board daemon holds through Wi-Fi gaps up to 0.6 s, eases to a stop, and after 2 s latches self-driving off until re-armed; the MCU hands control to the real stick if the daemon stops feeding it for ~0.25 s.
- **E-stop**: the chair's own power button.
- **Speed cap**: the chair's own speed setting, plus a stick cap (30%) on model commands in `chair_out.py`.
- **Takeover**: pushing the real joystick in self-driving hands control back (`stats()["takeover"]`).

A network drop must always result in a stopped chair.
