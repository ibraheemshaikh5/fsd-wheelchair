# Architecture

## Loop

1. Phone streams camera frames and GPS fixes to the driving service.
2. Service plans a route to the destination (GPS waypoints).
3. Model takes the latest frames plus the next waypoint and outputs a short trajectory.
4. Service converts the trajectory to linear and angular velocity and sends it to the chair.
5. Service pushes state to the iPad: video, planned path, detected obstacles, speed, route progress.

## Where it drives

Sidewalks and indoors.

- **Sidewalks**: GPS gives position and the route. The loop above applies as written.
- **Indoors**: no GPS. The destination has to be given another way, which is still [undecided](open-questions.md). The model and chair control stay the same; only steps 2 and 3 change.

## Pieces

### Phone (sensors)
- Sends: camera video, GPS, heading (compass/IMU).
- Mounted forward-facing at a fixed, known height and angle.

### Driving service (computer)
- Runs the model and the control loop.
- Owns all decisions. The phone, chair, and iPad are thin clients.
- One process on one machine for now. Moves onto the chair later.

### Chair interface
- A small microcontroller on the chair that takes velocity commands and drives the chair.
- How it connects to the WOLF's controller is not yet known. See [open questions](open-questions.md).

### iPad web app
- Tesla-FSD-style view: live rendering of the chair, path ahead, obstacles, speed, map with route.
- Inputs: set destination, engage, disengage.

## Safety

These live on the chair, not on the remote computer:

- **Watchdog**: no fresh command within a short timeout → stop.
- **Physical e-stop**: cuts drive power, reachable by the rider.
- **Speed cap**: hard limit enforced by the chair interface.
- **Manual override**: touching the joystick takes control immediately.

A network drop must always result in a stopped chair.
