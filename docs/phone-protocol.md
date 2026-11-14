# Phone protocol (v1)

How the phone on the chair talks to the driving service. Implemented in
[`service/drive_service/protocol.py`](../service/drive_service/protocol.py) and
[`web/shared/protocol.ts`](../web/shared/protocol.ts); change all three together.

## Connection

- One WebSocket at `/ws/phone`. Reached over HTTPS through `tailscale serve`; the service itself listens on `127.0.0.1` only.
- **Newest phone wins.** A new `/ws/phone` connection takes over. The older socket is closed with `4410`, and anything it still sends is ignored.
- **Except a priority phone.** A phone signed in to Tailscale as one of `DRIVE_PRIORITY_USERS` (read from the `Tailscale-User-Login` header that `tailscale serve` adds) always takes over, and while it holds the slot every other phone is closed with `4409` as soon as it connects.
- A phone socket that sends nothing for 5 s (`DRIVE_PHONE_IDLE_TIMEOUT_S`) is closed with `4408`, so a dead phone never holds the slot. The phone pings every second, so a live one never trips this.
- The phone sends `hello`, then a `ping`, as the first two messages of every connect, and pings once a second after that.
- The phone reconnects with backoff (0.5 s doubling to 10 s). The backoff only resets once a connection has answered or stayed open for 2 s, so a socket that is closed straight after opening keeps backing off.
- If the service sends nothing back for 5 s, the phone drops the socket and reconnects.
- When the page is hidden (screen locked, Safari in the background) the phone closes its socket, stops the camera and stops sending. When the page is visible again it reconnects and resumes on its own, with no tap. A tap on Stop is respected: it stays stopped.

### Who may connect

- Every HTTP request and WebSocket must carry a `Host` in the allowlist (`DRIVE_ALLOWED_HOSTS`, default `["localhost", "127.0.0.1", "*.ts.net"]`; the port is ignored). Anything else gets HTTP `400` before it reaches the API, the sockets or the pages. This stops a DNS-rebinding page from reaching the service through the loopback port. `*.ts.net` keeps `tailscale serve` working with no setup; pinning the exact machine name is stricter.
- Browser connections must come from the service's own origin (or one in `DRIVE_ALLOWED_ORIGINS`). A socket from another origin is refused during the handshake with HTTP `403` (the service closes it before accepting), so it is never opened; a browser reports that as close code `1006`, the same as any failed connection, and the capture page shows "Connection lost, retrying". The service logs a warning naming the origin. Recording requests from another origin get `403`.
- There is no authentication beyond this. Anyone on the tailnet can open `/ws/phone` and take over the stream. See [open questions](open-questions.md).

### Close codes

| code | sent by | meaning | the capture page |
|---|---|---|---|
| `4410` | service | replaced by a newer phone connection | stops, does not reconnect: "Another phone or tab took over the stream. Tap Start to resume." |
| `4409` | service | another phone has priority | stops, does not reconnect: "Another phone has priority and is streaming. Tap Start to try again once it stops." |
| `4408` | service | no data for 5 s | reconnects with backoff |
| `1008` | service | too many invalid messages in a row | reconnects with backoff: "The service refused this page's messages" |
| `1006` | browser | the connection failed or dropped, including a foreign origin refused with HTTP `403` at the handshake | reconnects with backoff: "Connection lost" |
| `4429` | service | too many viewers (viewer sockets only) | the viewer page keeps backing off (0.5 s doubling to 5 s); its backoff only resets once the service has sent it something |
| `4000` | phone | no reply from the service for 5 s | reconnects with backoff |

## Clocks

Every message carries `t`: the phone's clock, milliseconds since the Unix epoch.

The phone sends `ping` right after `hello` and then once a second. The service
answers `pong` with its own time. The phone works out the round trip and offset
(`server = phone + offset_ms`, assuming equal delay each way) and reports them in
its next `ping`. The service uses the sample with the shortest round trip out of
the last 20. It is synced once it has a sample with a round trip of at most
500 ms (`DRIVE_MAX_CLOCK_RTT_MS`).

A new connection may be a different phone, so the service forgets `hello` and
the clock estimate on every connect. Within one capture session the phone's
first `ping` carries its last measured sample, so the clock is back within one
round trip. A fresh page, or a resume after being hidden, takes about 1–2 s.

## Staleness

A reading's age runs from the **older** of two moments: when the service
received it, and when the phone captured it, converted to the service's clock
with the offset. Half the round trip is added to the transit time, since that is
how wrong a min-RTT offset can be. Taking the older of the two means a slow or
backlogged link, or a resent fix, makes a reading look staler, and a wrong
offset or a jump in the phone's clock can never make it look fresher than its
arrival.

Which phone timestamp each reading is aged by:

| reading | timestamp | stale after |
|---|---|---|
| camera frame | header `t` | 500 ms |
| GPS fix | the earlier of `fix_t` and `t` | 10 s (an iPhone at rest reports about every 5 s) |
| compass | `t` | 1 s |
| motion (speed, turn rate) | the newest IMU sample used (`t` + its `dt_ms`) | 250 ms (`DRIVE_MOTION_STALE_MS`) |

**Fail closed.** Before the clock is synced no reading is fresh, whatever its
receive time. A reading stamped more than 250 ms in the future
(`DRIVE_MAX_CLOCK_SKEW_MS`) proves the offset is wrong: the service drops the
clock estimate, logs a warning, and nothing new is fresh until the next
measured `ping`.

In the status (`GET /api/status` and every `/ws/viewer` message):

- `clock_synced` is true while a phone is connected and its clock is synced.
- `sensors_ok` means the phone is connected, `clock_synced`, and both the frame and GPS fix are fresh. Anything that drives must check this (or `clock_synced` plus each reading's freshness), not just the age of one reading.
- `stats.fps` counts frames received in the last 2 s, so it drops to 0 when frames stop.

## Heading

Heading uses GPS course while moving faster than 0.5 m/s and the compass otherwise.

The phone sends the compass as magnetic. The service converts it to true north
with the local magnetic declination, so both sources share one frame:

- `DRIVE_MAGNETIC_DECLINATION_DEG` sets it (east positive; about −11 around College Park).
- If that is unset, it comes from the World Magnetic Model (WMM2025, valid to 2029) at the first fresh GPS fix, and is recomputed after moving more than 5 km.

Status `heading`:

| field | |
|---|---|
| `deg` | best heading, clockwise, in `reference` north |
| `source` | `gps_course` or `compass` |
| `reference` | `true`, or `magnetic` while declination is unknown (null with no heading) |
| `declination_deg` | the declination applied, east positive, or null |
| `compass_deg` | the raw magnetic compass reading, never corrected |

Only a `true` heading can be combined with GPS course or a map. Recordings keep
the raw `magnetic_deg`.

## Binary messages: camera frames

```
[uint32 little-endian: header length][header JSON][JPEG bytes]
```

Header: `{"seq": int, "t": ms, "w": px, "h": px, "rot": 0|90|180|270}` (`rot` is optional; older pages leave it out).

- `rot` is how many degrees clockwise the page turned the camera image before encoding, so the picture is upright and landscape when the phone is mounted sideways. Safari cannot lock a page to landscape: with Portrait Orientation Lock on (or the page in portrait) a sideways phone still delivers portrait video lying on its side. The page reads gravity from `devicemotion` (smoothed over ~0.5 s, and it only switches once the phone is turned more than 60° from the current way, so walking jolts do not flip it) and turns the frame by the matching quarter turn. Video Safari already turned to landscape is left alone (`rot` 0). An upright phone stays portrait: a portrait picture cannot become landscape without cropping. The phone's settings have "Rotate picture" to force a turn if the automatic choice is ever wrong. `w` and `h` are the size after turning. Recordings keep `rot` per frame in `frames.jsonl`.

- `seq` counts every frame the camera produced, including ones the phone skipped. Gaps show up as `missed` in the stats.
- `t` is the capture time when the browser exposes it (`requestVideoFrameCallback`), otherwise the time the frame was grabbed. It is on the same wall clock (`Date.now()`) as every other `t`: the phone takes only the short delay since capture from the monotonic clock (`performance.now()`) and subtracts it from `Date.now()`. It never uses `performance.timeOrigin + captureTime`, because the monotonic clock stops while an iPhone sleeps and would stamp every frame after a screen lock in the past. Without `requestVideoFrameCallback` the phone skips a grab while the video is paused, has no current frame, or the camera track is muted or ended.
- Default 640 px on the long side, JPEG quality 0.7, 60 fps (the top rate of the iPhone's normal video formats; a setting allows up to 240). The driving model picks 20 fps by capture time; see [model data guide](model-data-guide.md).
- **Latest wins.** The phone skips a frame if two are already encoding, if sending it would leave more than about 100 ms of data (at most 100 KB, at the measured uplink rate) queued on the socket, or, once the service acks frames, if 3 frames are already in flight (see [Flow control](#flow-control-acks)). Old frames are never queued up behind a slow link.

## Flow control: acks

The service answers **every** binary frame with an `ack` carrying its `seq`,
right after handling it: whether it was stored, only left out of the recording
by the frame rate limit, or refused for a reason other than an unreadable
header (too large, not a JPEG, or a header that is a JSON object with a whole
`seq` ≥ 0 but fails another check, such as `w: 0` or an added field; the
`error` reply comes first, then the `ack`). A frame whose header is not such an
object has no usable `seq` and gets only the `error`. Acks go out one per
frame, in order.

The phone's rule:

- A frame is **in flight** from the moment it is sent until the ack with its `seq`, or any higher `seq`, arrives.
- While acks flow, a new frame is sent only if fewer than `MAX_FRAMES_IN_FLIGHT = 3` are in flight. Otherwise it is skipped (counted as `skipped_flow`), never queued: the next frame is newer anyway.
- An in-flight frame with no ack after `ACK_TIMEOUT_MS = 1000` stops counting, so a lost ack can never stall the stream.
- Until the **first** ack on a connection, only the `bufferedAmount` send budget applies, so a page still works against a service without acks. After it, both checks apply.
- All of this, and the link counters, reset on every reconnect.
- The phone measures the ack round trip (send to ack, monotonic clock) and keeps a smoothed value (`ack_rtt_ms`).
- **Adaptive quality.** Once a second, while the service acks: JPEG quality drops by 0.1 (not below 0.4) when the smoothed ack round trip is over 150 ms or fewer than 20 frames a second got through (when the camera delivered at least 20); it rises by 0.05, up to the configured quality, when acks take under 100 ms and 20+ frames a second got through; otherwise it holds. The share of frames skipped is not the signal: at 60 fps with 3 frames in flight, any round trip above ~50 ms skips most frames while 20+ fresh ones still arrive. Without acks (an older service) it falls back to the share skipped by the send budget: over 30 % steps down, under 5 % steps up. Resolution never changes.

With 3 frames in flight a frame can wait behind at most two others, so on a
slow link the phone sends fewer frames, at lower quality, instead of older
ones. The driving model only needs 20 fps, and needs them at most about
100–150 ms old.

**Why acks.** On 2026-11-14 (recording `20261114T175513Z`) the iPhone captured
about 58 fps, but walking around on weak home Wi-Fi (a direct Tailscale link,
not relayed) only 4–15 fps reached the Mac, each frame 0.4–1.0 s old on arrival
(median per 10 s window), and GPS and motion, which share the socket, arrived
late too (status `motion` went stale). Resting on the table at the end: 60 fps
at 36 ms. The `bufferedAmount` budget skipped 5251 frames and still let latency
reach about 1 s, because `bufferedAmount` only sees the browser's own queue,
not data already handed to the OS, TCP and Tailscale, which is where the
backlog sat. An ack is the only signal that a frame has actually arrived.

The phone reports how this is going in a `link` message once a second; the
service records it and shows the latest as status `link`:

| field | |
|---|---|
| `captured`, `sent`, `acked` | frames since this connection started |
| `skipped_flow`, `skipped_encoder`, `skipped_socket` | frames skipped for flow control, because two were already encoding, and by the `bufferedAmount` budget |
| `in_flight` | frames sent and not yet acked |
| `jpeg_quality` | the quality in use now (0–1] |
| `ack_rtt_ms` | smoothed send → ack time, or null before the first ack |
| `age_ms` | since the service received this report (service clock, not the phone's) |
| `fresh` | `age_ms` ≤ 3000 (`DRIVE_LINK_STALE_MS`) |

Status `link` is null until the first `link` message, and again after a new
phone connection. It is diagnostic only; nothing drives from it.

## Text messages: phone → service

All have `v: 1`, `type`, `t`. Unknown fields are rejected.

| type | fields | rate |
|------|--------|------|
| `hello` | `user_agent`, `camera {label, width, height, fps}`, `mount {height_m, pitch_deg}` or null, `imu_axes`? (`"device_xyz"`) | first message on every connect |
| `gps` | `lat`, `lon`, `accuracy_m`, `altitude_m`?, `speed_mps`?, `course_deg`? (true north, clockwise), `fix_t` | whenever the browser reports (≈1 Hz) |
| `heading` | `magnetic_deg` (clockwise from magnetic north), `accuracy_deg`? | ≤10 Hz |
| `imu` | `samples`: up to 500 of `[ms since t, ax, ay, az, rx, ry, rz]`; acceleration in m/s² with gravity, rotation in deg/s about device x, y, z (see [IMU axes and signs](#imu-axes-and-signs-on-ios)) | batched every 50 ms |
| `ping` | `rtt_ms`?, `offset_ms`? from the previous exchange | right after `hello`, then 1 Hz |
| `link` | `captured`, `sent`, `acked`, `skipped_flow`, `skipped_encoder`, `skipped_socket` (counts since this connection started, each a JSON integer 0 to 10⁹; `true` or `"5"` is refused), `in_flight` (integer, 0 to 1000), `jpeg_quality` (0–1]), `ack_rtt_ms`? (0 to 600,000) | 1 Hz; see [Flow control](#flow-control-acks) |

`?` = may be null. GPS `speed_mps`/`course_deg` are null when the phone is not moving.

`heading` is the only JSON message the phone drops under the same send budget
as frames (about 100 ms of queued data); `hello`, `ping`, `gps`, `imu` and
`link` are always sent.

After a reconnect the phone resends its last GPS fix if it is under 2 s old (with
a new `t`, same `fix_t`). Browsers often report the first fix before the socket
is open, and a phone standing still may not report again for a while. The
service ages it by `fix_t`, so a resent fix is never fresher than the fix itself.

`pose` is reserved for a future native client with motion tracking.

### IMU axes and signs on iOS

Verified against WebKit (`WebCoreMotionManager.mm`, `DeviceMotionClientIOS.mm`)
and Apple's CoreMotion docs. iOS Safari differs from the W3C spec in two ways.

- **Device axes:** x to the right of the screen, y toward the top of the screen,
  z out of the screen toward the viewer. The back camera looks along −z.
- **Gyro axes.** On iOS `rotationRate.alpha` is rotation about device **x**,
  `beta` about **y**, `gamma` about **z** (deg/s, right-handed). The spec says
  otherwise. A page that sends `imu_axes: "device_xyz"` in its `hello` writes
  `rx, ry, rz` as rotation about x, y, z. Without that field the page is older:
  it wrote `[beta, gamma, alpha]`, so those columns hold `[about y, about z,
  about x]`, and the true values are `x = rz`, `y = rx`, `z = ry`. The service
  remaps old-order batches before using them. Recordings keep the columns as
  sent, and `meta.json` stores `imu_axes` (null for old pages) so a reader can
  remap the same way. Replaying the first iPhone recording confirmed this: with
  the remap, gyro-propagated gravity tracked the accelerometer best.
- **Accelerometer sign.** On iOS `accelerationIncludingGravity` is CoreMotion's
  (userAcceleration + gravity) × 9.80665, and CoreMotion's gravity points
  **toward the ground**: a phone lying flat, face up, reads `az ≈ −9.8` (the
  spec says +9.8). The reading is the negative of specific force. With `G` the
  low-passed reading (the gravity vector, pointing down): up is `−G/|G|`, and
  true linear acceleration is `G − reading` (`IOS_ACCEL_SIGN = −1` in
  [`motion.py`](../service/drive_service/motion.py)). Device check: lay the
  phone flat, face up; `az` must read about −9.8.

## Motion: speed, turn rate, acceleration

The service estimates the chair's motion from every IMU batch and GPS fix
([`motion.py`](../service/drive_service/motion.py)). It works for any mount
because everything is measured against gravity and the camera axis:

- **Gravity** `G`: each sample the estimate is turned by the gyro, then pulled
  toward the accelerometer reading with a 10 s time constant. The gyro step
  handles tilting, so the pull only has to remove gyro drift; a faster pull
  would absorb a steady push as tilt and make a moving chair look still.
- **Turn rate** `yaw_rate_dps` = rotation-rate vector · unit up. Positive =
  turning **left** (counter-clockwise seen from above).
- **Forward** = the back camera's axis (−z) flattened onto the horizontal plane;
  **left** = up × forward. Undefined (null) while the camera points nearly
  straight up or down.
- **Acceleration** = `G − reading`, projected: `accel_fwd_mps2` positive =
  speeding up along the camera's forward direction; `accel_lat_mps2` positive =
  toward the chair's left.
- **Speed** `speed_mps` ≥ 0: forward acceleration integrated between GPS fixes
  (`speed_source: "imu"`); each GPS fix with a speed moves it 70 % of the way to
  the GPS speed (`"gps"`); once the phone has been still for 0.5 s (rotation
  < 2 deg/s, linear acceleration < 0.25 m/s², readings within 0.4 m/s²) it is 0
  (`"still"`), unless a GPS fix in the last 2 s said faster than 0.3 m/s. While
  the speed estimate itself is above 0.3 m/s the phone must be still for 1.5 s
  instead, because a steady roll on a smooth floor (indoors, where GPS speed
  is null) looks still to an IMU.
- **Unknown** (null) until a still period or a GPS speed gives a starting point;
  again after IMU samples stop for more than 0.5 s, after 3 s of integrating
  with neither a GPS speed nor a still period, and on every phone reconnect.
  Samples with an accelerometer reading under 1 or over 160 m/s², or rotation
  over 2000 deg/s, are ignored.

Limits: Safari delivers IMU at about 60 Hz and GPS speed at about 1 Hz (often
null indoors or when stopped). Between fixes the IMU speed drifts, and a long
steady push slowly leaks into the gravity estimate, so IMU-only speed under-reads
it (about 12 % after a 2.5 s push) until the next fix. Treat speed as a display and a sanity check, not as
odometry.

Status `motion` (null until the first IMU batch):

| field | |
|---|---|
| `speed_mps` | ≥ 0, or null when unknown |
| `speed_source` | `gps`, `imu`, `still`, or null |
| `yaw_rate_dps` | positive = turning left |
| `accel_fwd_mps2` | positive = speeding up along the camera's forward |
| `accel_lat_mps2` | positive = toward the chair's left |
| `age_ms` | age of the newest IMU sample used, aged like every other reading |
| `fresh` | `age_ms` ≤ 250 with a known capture time; never before clock sync |

A GPS fix updates `speed_mps` without changing `age_ms`.

## Text messages: service → phone

| type | fields |
|------|--------|
| `pong` | `t` (the ping's, echoed), `server_t` |
| `ack` | `seq` (the frame's), `t` (service wall clock, ms since the epoch, when the frame was handled); one per binary frame, see [Flow control](#flow-control-acks) |
| `error` | `message`, a human-readable reason a message was rejected |

## Limits

Set in [`config.py`](../service/drive_service/config.py), overridable with `DRIVE_*` environment variables.

| | default | over the limit |
|---|---|---|
| frame size | 1 MB | rejected |
| frame header | 1 KB | rejected |
| text message | 64 KB | rejected |
| frame rate | 120/s (about 2× the phone's top rate) | not recorded, counted as `rate_limited` |
| text rate (pings not counted) | 200/s | not recorded, counted as `rate_limited` |
| invalid messages in a row | 20 | connection closed (`1008`) |
| phone idle | 5 s | connection closed (`4408`) |
| viewers | 4 | extra ones closed (`4429`) |
| viewer updates | 30/s (`DRIVE_VIEWER_FPS`): the status, then the newest frame if it is new | paced by the service; a viewer cannot speed it up |
| `/api/*` reads (status) | 20/s | HTTP `429` |
| `/api/recording/*` | 5/s, counted separately | HTTP `429` |

The live snapshot is always latest-wins: every valid frame and message replaces
the previous reading, even over the rate limits. The rate limits only decide
what is written to a recording, so a burst can never leave an older reading in
place of a newer one. Pings are always answered.

## Recordings

`POST /api/recording/start` and `/stop` (also buttons in the viewer). Both need the
service's own origin. Every `/api` response is `{success, data, error}`.

`start` answers:

| status | when |
|---|---|
| `200` | started; `data` is the recording status |
| `409` | the phone is not connected or has not sent `hello` yet; a recording is already running; or the previous one is still finishing |
| `507` | less than 2 GB free on the recordings disk (`DRIVE_RECORDER_MIN_FREE_BYTES`) |
| `403` | request from another origin |

`stop` returns once the files are written. A recording is written by a
background thread; if it takes more than 10 s, `stop` returns anyway
and the folder is finalised when the writer exits. Until then `finishing` is true
and a new `start` gets `409`.

A recording ends on its own, cleanly, at the first of these. `end_reason` says
which, and `error` stays null:

| limit | default | `end_reason` |
|---|---|---|
| frames | 432,000, 2 h at 60 fps (`DRIVE_RECORDER_MAX_FRAMES`) | `frame limit reached (N)` |
| frames plus logs | 20 GB (`DRIVE_RECORDER_MAX_BYTES`) | `size limit reached (N bytes)` |
| free disk | below 2 GB (`DRIVE_RECORDER_MIN_FREE_BYTES`) | `disk nearly full` |

`error` is only set for a failure, for example a write error. More than 1024
items (`DRIVE_RECORDER_QUEUE_SIZE`, about 9 s at 60 fps plus the IMU, motion
and heading lines) or 64 MB (`DRIVE_RECORDER_MAX_QUEUE_BYTES`) waiting to be
written is dropped and counted in `dropped`. Every item the service tried to record is either written or
counted in `dropped`, including items still queued when the disk fills up or
the writer fails. A manual stop leaves both `end_reason` and `error` null.

A recording that ends at a limit gets the same 10 s wait as a manual stop: if
its writer has not exited by then, `error` becomes
`recording writer did not finish in time` and `meta.json` is finalised
provisionally (rewritten with the real counts once the writer does exit). On
shutdown the service waits, bounded the same way, for every writer still
finishing.

Status `recording`: `active`, `finishing`, `session`, `frames`, `sensors`, `dropped`, `error`, `end_reason`.

Each recording is a folder under `recordings/`:

```
20261114T150000Z/
  meta.json        protocol, start/end time, camera, mount, user agent, imu_axes, clock, counts, error, end_reason
  frames/000001.jpg …
  frames.jsonl     {n, file, seq, t, w, h, recv_ms, rot?} per frame
  sensors.jsonl    every gps/heading/imu/link message as received, plus recv_ms (hello only if the phone reconnects),
                   and a service-derived motion line after each recorded imu line
```

The motion line is written by the service, not the phone:

```
{"recv_ms": <server wall ms>, "v": 1, "type": "motion", "t": <the imu line's t>,
 "speed_source": "gps" | "imu" | "still" | null,
 "samples": [[dt_ms, speed_mps, yaw_rate_dps, accel_fwd_mps2, accel_lat_mps2], ...]}
```

One row per IMU sample with the same `dt_ms`, so motion rows line up 1:1 with
the imu rows of the line with the same `t`. Any value may be null when unknown;
an ignored sample has nulls in every column but `dt_ms`. Units, axes and signs
as in [Motion](#motion-speed-turn-rate-acceleration); `speed_source` is the
source after the batch. A motion line is only written when its imu line was
(the text flood limit applies to both), but a full recorder queue can still
drop one without the other, so match them by `t`. Motion lines count in the
recording's `sensors` total. Readers that only know the phone's message types
skip them, and likewise `link` lines from newer pages.

Camera, mount and user agent come from the phone's `hello`, which must arrive before
a recording can start. `recordings/` is git-ignored. Recordings contain location
and video of people nearby; treat them as private.
