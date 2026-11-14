# Phone protocol (v1)

How the phone on the chair talks to the driving service. Implemented in
[`service/drive_service/protocol.py`](../service/drive_service/protocol.py) and
[`web/shared/protocol.ts`](../web/shared/protocol.ts); change all three together.

## Connection

- One WebSocket at `/ws/phone`. Reached over HTTPS through `tailscale serve`; the service itself listens on `127.0.0.1` only.
- **Newest phone wins.** A new `/ws/phone` connection always takes over. The older socket is closed with `4410`, and anything it still sends is ignored.
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

IMU is recorded but not kept as a live reading.

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

Header: `{"seq": int, "t": ms, "w": px, "h": px}`.

- `seq` counts every frame the camera produced, including ones the phone skipped. Gaps show up as `missed` in the stats.
- `t` is the capture time when the browser exposes it (`requestVideoFrameCallback`), otherwise the time the frame was grabbed. It is on the same wall clock (`Date.now()`) as every other `t`: the phone takes only the short delay since capture from the monotonic clock (`performance.now()`) and subtracts it from `Date.now()`. It never uses `performance.timeOrigin + captureTime`, because the monotonic clock stops while an iPhone sleeps and would stamp every frame after a screen lock in the past. Without `requestVideoFrameCallback` the phone skips a grab while the video is paused, has no current frame, or the camera track is muted or ended.
- Default 640 px on the long side, JPEG quality 0.7, 10 fps.
- **Latest wins.** The phone skips a frame if the last one is still encoding or more than ~150 KB is queued on the socket. Old frames are never queued up behind a slow link.

## Text messages: phone → service

All have `v: 1`, `type`, `t`. Unknown fields are rejected.

| type | fields | rate |
|------|--------|------|
| `hello` | `user_agent`, `camera {label, width, height, fps}`, `mount {height_m, pitch_deg}` or null | first message on every connect |
| `gps` | `lat`, `lon`, `accuracy_m`, `altitude_m`?, `speed_mps`?, `course_deg`? (true north, clockwise), `fix_t` | whenever the browser reports (≈1 Hz) |
| `heading` | `magnetic_deg` (clockwise from magnetic north), `accuracy_deg`? | ≤10 Hz |
| `imu` | `samples`: up to 500 of `[ms since t, ax, ay, az, rx, ry, rz]`; acceleration in m/s² with gravity, rotation in deg/s about x, y, z | batched every 100 ms |
| `ping` | `rtt_ms`?, `offset_ms`? from the previous exchange | right after `hello`, then 1 Hz |

`?` = may be null. GPS `speed_mps`/`course_deg` are null when the phone is not moving.

`heading` is the only message the phone drops when more than ~150 KB is queued
on the socket; `hello`, `ping`, `gps` and `imu` are always sent.

After a reconnect the phone resends its last GPS fix if it is under 2 s old (with
a new `t`, same `fix_t`). Browsers often report the first fix before the socket
is open, and a phone standing still may not report again for a while. The
service ages it by `fix_t`, so a resent fix is never fresher than the fix itself.

`pose` is reserved for a future native client with motion tracking.

## Text messages: service → phone

| type | fields |
|------|--------|
| `pong` | `t` (the ping's, echoed), `server_t` |
| `error` | `message`, a human-readable reason a message was rejected |

## Limits

Set in [`config.py`](../service/drive_service/config.py), overridable with `DRIVE_*` environment variables.

| | default | over the limit |
|---|---|---|
| frame size | 1 MB | rejected |
| frame header | 1 KB | rejected |
| text message | 64 KB | rejected |
| frame rate | 60/s | not recorded, counted as `rate_limited` |
| text rate (pings not counted) | 200/s | not recorded, counted as `rate_limited` |
| invalid messages in a row | 20 | connection closed (`1008`) |
| phone idle | 5 s | connection closed (`4408`) |
| viewers | 4 | extra ones closed (`4429`) |
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
| frames | 100,000 (`DRIVE_RECORDER_MAX_FRAMES`) | `frame limit reached (N)` |
| frames plus logs | 20 GB (`DRIVE_RECORDER_MAX_BYTES`) | `size limit reached (N bytes)` |
| free disk | below 2 GB (`DRIVE_RECORDER_MIN_FREE_BYTES`) | `disk nearly full` |

`error` is only set for a failure, for example a write error. More than 64 MB
waiting to be written (`DRIVE_RECORDER_MAX_QUEUE_BYTES`) is dropped and counted
in `dropped`. Every item the service tried to record is either written or
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
  meta.json        protocol, start/end time, camera, mount, user agent, clock, counts, error, end_reason
  frames/000001.jpg …
  frames.jsonl     {n, file, seq, t, w, h, recv_ms} per frame
  sensors.jsonl    every gps/heading/imu message as received, plus recv_ms (hello only if the phone reconnects)
```

Camera, mount and user agent come from the phone's `hello`, which must arrive before
a recording can start. `recordings/` is git-ignored. Recordings contain location
and video of people nearby; treat them as private.
