# Phone protocol (v1)

How the phone on the chair talks to the service. The code is the source of truth:
[`service/drive_service/protocol.py`](../service/drive_service/protocol.py) and
[`web/shared/protocol.ts`](../web/shared/protocol.ts). Change both together. Limits and timeouts
are in [`config.py`](../service/drive_service/config.py) (`DRIVE_*` environment variables).

## Connection

- One WebSocket at `/ws/phone`, reached over HTTPS through `tailscale serve`.
- The phone sends `hello`, then `ping`, on every connect, then a `ping` every second.
- Newest phone wins the slot, except a phone in `DRIVE_PRIORITY_USERS`, which always wins.
- The phone reconnects with backoff (0.5 s doubling to 10 s). It closes its socket while the page is hidden.
- Only the tailnet protects this: there is no other authentication.

### Close codes

| code | sent by | meaning | the phone |
|---|---|---|---|
| `4410` | service | replaced by a newer phone | stops, does not reconnect |
| `4409` | service | another phone has priority | stops, does not reconnect |
| `4408` | service | no data for 5 s | reconnects |
| `1008` | service | too many invalid messages in a row | reconnects |
| `1006` | browser | connection failed or dropped (also a refused origin) | reconnects |
| `4000` | phone | no reply from the service for 5 s | reconnects |

## Camera frames (binary)

```
[uint32 little-endian: header length][header JSON][JPEG bytes]
```

Header: `{"seq": int, "t": ms, "w": px, "h": px, "rot": 0|90|180|270}` (`rot` optional).

- `seq` counts every frame the camera produced, including skipped ones.
- `t` is the capture time on the phone's wall clock, ms since the Unix epoch, like every other `t`.
- `rot` is how far the page turned the image clockwise to make it upright. `w`, `h` are after turning.

### Flow control

The service answers every frame with an `ack` carrying its `seq`.

- A frame is in flight from send until an ack with its `seq` or a higher one arrives, or for 1 s at most.
- The phone sends a new frame only if fewer than 2 are in flight. Otherwise it skips it: frames are never queued, so a slow link gets fewer frames, not older ones.
- Before the first ack on a connection only the socket's send budget (about 100 ms of queued data) applies.
- JPEG quality steps down when acks are slow and back up when they are fast. Resolution never changes.

## Text messages: phone → service

All carry `v: 1`, `type`, `t`. Unknown fields are rejected.

| type | fields | rate |
|------|--------|------|
| `hello` | `user_agent`, `camera {label, width, height, fps}`, `mount {height_m, pitch_deg}` or null, `imu_axes`? (`"device_xyz"`) | first, on every connect |
| `gps` | `lat`, `lon`, `accuracy_m`, `altitude_m`?, `speed_mps`?, `course_deg`? (true north, clockwise), `fix_t` | about 1 Hz |
| `heading` | `magnetic_deg` (clockwise from magnetic north), `accuracy_deg`? | up to 10 Hz |
| `imu` | `samples`: up to 500 of `[ms since t, ax, ay, az, rx, ry, rz]` | every 50 ms |
| `ping` | `rtt_ms`?, `offset_ms`? from the previous exchange | after `hello`, then 1 Hz |
| `link` | `captured`, `sent`, `acked`, `skipped_flow`, `skipped_encoder`, `skipped_socket`, `in_flight`, `jpeg_quality`, `ack_rtt_ms`? | 1 Hz, diagnostic only |

`?` = may be null.

## Text messages: service → phone

| type | fields |
|------|--------|
| `pong` | `t` (the ping's, echoed), `server_t` |
| `ack` | `seq` (the frame's), `t` (service clock) |
| `error` | `message`: why a message was rejected |

## Clocks and freshness

The phone works out its offset to the service's clock from `ping`/`pong` and reports it in the next `ping`.
The service ages each reading from the older of "when it arrived" and "when the phone captured it".
Before the clock is synced, nothing is fresh.

| reading | stale after |
|---|---|
| camera frame | 500 ms |
| GPS fix | 10 s |
| compass | 1 s |
| motion | 250 ms |

Status `sensors_ok` means: phone connected, clock synced, frame and GPS fresh. Anything that drives must check it.

## IMU axes and signs on iOS

iOS Safari differs from the W3C spec in two ways.

- **Device axes:** x to the right of the screen, y toward its top, z out toward the viewer. The back camera looks along −z.
- **Gyro:** `rx, ry, rz` are rotation about device x, y, z in deg/s when `hello` has `imu_axes: "device_xyz"`.
  Older pages without it sent `[about y, about z, about x]`; the service remaps those.
- **Accelerometer:** m/s² including gravity, and gravity points toward the ground: a phone lying flat, face up,
  reads `az ≈ −9.8`. True linear acceleration is `G − reading`, with `G` the low-passed reading.
- **Derived motion** (service): `yaw_rate_dps` positive = turning left; `accel_fwd_mps2` positive = speeding up along the
  camera's forward; `accel_lat_mps2` positive = toward the chair's left.
  The status also carries `yaw_deg`: the yaw rate summed over the phone's own sample times since it connected
  (positive left, not wrapped, the gyro's resting rate taken off). It is not in the recorded `motion` line.

## Recordings

`POST /api/recording/start` and `/stop`, or the buttons in the viewer. Each recording is a folder under `recordings/`
(git-ignored; it holds location and video of people nearby, so treat it as private):

```
20261114T150000Z/
  meta.json        protocol, start/end time, camera, mount, user agent, imu_axes, clock, counts, error, end_reason
  frames/000001.jpg …
  frames.jsonl     {n, file, seq, t, w, h, recv_ms, rot?} per frame
  sensors.jsonl    every gps/heading/imu/link message as received, plus recv_ms,
                   and a service-derived motion line after each imu line
```

The motion line:

```
{"recv_ms": <server wall ms>, "v": 1, "type": "motion", "t": <the imu line's t>,
 "speed_source": "gps" | "imu" | "still" | null,
 "samples": [[dt_ms, speed_mps, yaw_rate_dps, accel_fwd_mps2, accel_lat_mps2], ...]}
```

One row per IMU sample of the imu line with the same `t`. Any value may be null.
