# Model data guide: 20 fps frames and motion from recordings

For whoever builds the navigation model (FlowPilot first, see [model.md](model.md)).
It explains how to take exactly **20 frames per second** out of a recording, with
the speed, turn rate, GPS and heading that go with each frame. Everything here
comes from the code: [`service/drive_service/`](../service/drive_service),
[`web/capture/`](../web/capture) and [`ml/fsd_ml/recording.py`](../ml/fsd_ml/recording.py).
The wire format is in [phone-protocol.md](phone-protocol.md).

## In short

- The phone sends **20 fps at 480 px by default**, sized for driving. For
  training data set it to **60 fps and 640 px** in the capture page's settings
  (60 is the top rate of the iPhone's normal video formats; a setting allows up
  to 240) and take 20 fps out of that: at 60 fps the newest frame at a tick is
  within ~17 ms. Earlier recordings are mostly 60 fps at 640 px.
  Check the rate a recording actually has from `frames.jsonl`.
- Pick frames **by capture time** on a 50 ms grid: at each tick, take the newest
  frame at or before the tick. Skip the tick if that frame is more than 25 ms
  old. Never take a frame from after the tick, and never take "every 3rd frame".
- For each picked frame, take every other reading **at or before the frame's
  `t`**. Recordings from the current service include `motion` lines with speed,
  turn rate (positive = left) and acceleration.
- The IMU gyro columns are in the **wrong order in older recordings** (no
  `imu_axes` in `meta.json`). Remap them before you use them (see
  [the remap](#gyro-column-order)).
- iOS reports acceleration **with the opposite sign to the W3C spec**: a phone
  lying face up reads `az ≈ −9.8`.

## What a recording contains

```
recordings/20261114T163514Z/      folder name = UTC start time
  meta.json
  frames/000001.jpg …            JPEG, numbered in the order written
  frames.jsonl                   one line per frame
  sensors.jsonl                  one line per gps / heading / imu / motion / hello message
```

Recordings contain video of people and the rider's location. Keep them private
(`recordings/` is git-ignored).

### `meta.json`

| key | meaning |
|---|---|
| `protocol` | `1` |
| `started_utc`, `ended_utc` | ISO times on the service's clock |
| `camera` | from the phone's `hello`: `label` (e.g. `"Back Ultra Wide Camera"`), `width`, `height` (the camera's own size, **not** the JPEG's), `fps` (the rate the camera reported, or 30 if it reported none; this is not the rate actually recorded) |
| `mount` | `{height_m, pitch_deg}` as typed into the phone's settings, or null. `pitch_deg` < 0 means pointing down. These are hand-entered values, and the defaults are 1.1 m and −10° |
| `user_agent` | the phone's Safari user agent |
| `imu_axes` | `"device_xyz"` for current recordings. Null or **missing** means old gyro column order |
| `clock` | `{offset_ms, rtt_ms, samples}` at the start, or null if the clock was not synced yet. `server_ms = phone_ms + offset_ms` |
| `frames`, `sensors` | lines written (`sensors` includes motion lines) |
| `dropped` | items the recorder could not write (queue full). If this is not 0, there are holes |
| `error`, `end_reason` | why it ended, if not by a manual stop |

### `frames.jsonl`

```json
{"n": 1, "file": "frames/000001.jpg", "seq": 62, "t": 1794674114720.0, "w": 360, "h": 640, "recv_ms": 1794674114719.49}
```

| field | meaning |
|---|---|
| `n` | file number, 1, 2, 3 … with no gaps, even where frames are missing |
| `file` | path relative to the folder |
| `seq` | the phone's frame counter. Gaps usually mean the phone skipped frames, but not always: older pages thinned frames without a gap in `seq`. **Do not use `seq` or `n` to measure time** |
| `t` | **capture time**, phone clock, ms since the Unix epoch. Use this |
| `w`, `h` | JPEG size in pixels, after any turn. It can change during a recording (see [frame geometry](#frame-geometry)) |
| `rot` | optional: degrees clockwise the phone turned the image so it is upright (0, 90, 180, 270). Missing in recordings made before 2026-11-14 afternoon |
| `recv_ms` | when the service received it, **service** clock |

### `sensors.jsonl`

Every line is the phone's message as received, plus `recv_ms` (service clock).
All `t` values are on the phone clock. Lines are in arrival order; sort by time
yourself.

| `type` | fields | rate | notes |
|---|---|---|---|
| `imu` | `t`, `samples: [[dt_ms, ax, ay, az, rx, ry, rz], …]` | ~60 Hz samples, a batch every 50 ms (100 ms on older pages) | sample time = `t + dt_ms`. Accel in m/s² **including gravity**, rotation in deg/s. Always exactly 7 numbers. A phone with no gyro sends 0, not null |
| `motion` | `t` (= the matching imu line's `t`), `speed_source`, `samples: [[dt_ms, speed_mps, yaw_rate_dps, accel_fwd_mps2, accel_lat_mps2], …]` | one per recorded imu line | **Written by the service**, not the phone. Rows match that imu line's rows 1:1 with the same `dt_ms`. Any value may be null. Not present in recordings made before this was added |
| `gps` | `lat`, `lon`, `accuracy_m`, `altitude_m`?, `speed_mps`?, `course_deg`?, `fix_t` | ~1 Hz while moving, ~every 5 s at rest | the fix time is `min(fix_t, t)`. `speed_mps` and `course_deg` (true north, clockwise) are **null when the phone is not moving** and often null indoors. After a reconnect the last fix is sent again with a new `t` and the same `fix_t`, so de-duplicate by `fix_t` |
| `heading` | `magnetic_deg`, `accuracy_deg`? | up to ~30 Hz (10 Hz on older pages) | clockwise from **magnetic** north (iOS `webkitCompassHeading`). `t` is when the event was handled. `accuracy_deg` is null when iOS says the compass needs calibrating |
| `hello` | same as in `meta.json`: `camera`, `mount`, `user_agent`, `imu_axes` | only if the phone reconnected mid-recording | the camera rate, size or `imu_axes` may differ after it |

`?` = may be null. Recordings made before the motion work have no `motion` lines,
no `imu_axes` and 10 fps frames.

## Clocks

- Every `t`, `fix_t` and `dt_ms` is on the **phone's wall clock** (ms since the
  Unix epoch). Frames, IMU, GPS and heading are all on that clock, so you can
  compare them directly. You do not need the offset for that.
- `recv_ms` is the **service's** clock. To compare: `phone_ms + meta.clock.offset_ms`.
  `recv_ms − that` is the transit delay, about 40–100 ms indoors. Do not pick
  frames by `recv_ms`: the network adds jitter and batching.
- The offset in `meta.json` was measured at the start. A reconnect can change
  it. That only matters for `recv_ms` comparisons.

## What frame rate you actually have

Use the frame times, not `meta.camera.fps`.

```python
t = np.array([f.t_ms for f in rec.frames])
dt = np.diff(t)
print(f"median {1000 / np.median(dt):.1f} fps, worst gap {dt.max():.0f} ms")
```

The recorded rate can be lower than the camera rate, and it varies, because:

- the phone drops a frame while two are already being encoded,
- the phone drops frames to keep at most ~100 ms queued on a slow uplink (IMU
  and GPS are never dropped),
- the service records at most 120 frames/s, and its recorder drops items when
  its queue is full (`meta.dropped`),
- the page stops sending while the screen is locked or Safari is in the
  background.

At 60 fps, consecutive frames are about 16.7 ms apart with ±2 ms of jitter.
Expect holes of hundreds of milliseconds where the link stalled.
`Recording.fps` is an average over the whole drive and hides all of this.

The first iPhone recording (`20261114T163514Z`) is **10 fps, portrait, old gyro
order, no motion lines**. You cannot get 20 fps out of it: half the ticks come
out empty.

## Resampling to 20 Hz

```
tick k at T0 + 50·k ms
  frame = newest frame with t ≤ tick
  use it if tick − frame.t ≤ 25 ms, otherwise record a gap at this tick
```

- **At or before, never after.** At run time the model only has frames up to
  now. Picking the nearest frame (which can be in the future) leaks the future
  into training.
- **Tolerance below one tick.** With 25 ms no frame can be used for two ticks.
  At 60 fps the newest frame is always within ~17 ms unless frames are missing.
  The picked frames are then 33–67 ms apart, not exactly 50 ms. Give the model
  the real `t` if it cares about timing.
- **Report gaps.** Count the missing ticks and list each run of them (start,
  length). Split the drive into **contiguous segments**, and only build
  context windows inside a segment, since a window across a gap is not 20 Hz.
- **Why not every Nth frame:** `frames[::3]` is 20 fps only if the camera ran
  at exactly 60 fps with no drops. One dropped frame shifts every later pick by
  16.7 ms. A 30 fps stretch (low light) turns it into 10 fps. A one-second stall
  turns three frames into one second of time. The model would see time warp
  without anything to show for it.
- **`Recording.windows(size)` is the wrong tool at 60 fps.** It gives
  consecutive frames at the camera rate, so a 4-frame window covers 50 ms
  instead of 150 ms. Build windows from the resampled ticks instead.

## Motion, GPS and heading for each frame

For each picked frame, take the **newest reading with time ≤ `frame.t`**
(`Recording.gps_at` / `heading_at` already do this). Also check its age, and
treat it as missing when it is too old.

| value | source | null / missing when | suggested max age |
|---|---|---|---|
| speed (m/s, ≥ 0) | `motion` row `speed_mps` | no `motion` lines (old recording); no starting point yet (no still period and no GPS speed); >3 s of integrating with no GPS speed or still period; IMU gap >0.5 s; reconnect; a sample the service ignored (all nulls but `dt_ms`) | 250 ms (the service's rule) |
| turn rate (deg/s, + = left) | `motion` `yaw_rate_dps` | an ignored sample; gravity estimate under 5 m/s² (free fall) | 250 ms |
| accel fwd / lateral (m/s², + = speeding up / toward the left) | `motion` `accel_fwd_mps2`, `accel_lat_mps2` | as above; also when the camera points nearly straight up or down | 250 ms |
| position | `gps` `lat`, `lon`, `accuracy_m` | no fix yet, or indoors | a few s (the service uses 10 s) |
| GPS speed / course | `gps` | not moving, indoors, often at walking speed | the fix's age |
| compass | `heading` `magnetic_deg` | no compass permission | 1 s |

`Driver.propose()` takes `Motion(speed_mps, yaw_rate_radps)`: convert with
`math.radians(yaw_rate_dps)`.

### How good these are

- **Turn rate is good.** It is the gyro projected onto "up", measured at ~60 Hz,
  and does not depend on how the phone is mounted.
- **Speed is approximate.** The service integrates forward acceleration between
  GPS fixes, moves 70 % of the way to each GPS speed, and sets it to 0 after
  0.5 s still (1.5 s if it thought it was moving above 0.3 m/s). GPS speed comes
  at ~1 Hz, and not at all indoors or when stopped. Between fixes the IMU speed
  drifts, and it under-reads a long steady push (about 12 % after 2.5 s). Use
  it as an input, **not as a training label or odometry** (trajectory labels are
  [open question 7](open-questions.md)).
- **The `motion` lines are what the live service saw.** The estimate only uses
  data up to each sample, so it matches what the model would get while driving.
  A GPS fix updates speed between IMU batches, so it first shows up in the next
  batch's rows. A line's `speed_source` is the source **after** its batch, not
  per row.
- **GPS position** is ±3–20 m (see `accuracy_m`). Do not derive speed from
  position differences at walking pace.
- **Compass is magnetic.** True = magnetic + declination (east positive, about
  −11° around College Park). The recording does not store the
  declination. Compute it from the GPS fix with the World Magnetic Model
  (`pygeomag`, as `service/drive_service/declination.py` does), or use a
  constant for one area. Whether `webkitCompassHeading` points where the
  camera looks with the phone upright in the mount is **not verified yet**.
  `gps.course_deg` is already true north, but only while moving.

## Axes and signs

### Device axes (fixed to the phone's body)

x to the right of the screen, y toward the top of the screen, z out of the
screen toward the viewer. **The back camera looks along −z.** These axes do not
turn when the page or the image rotates.

### Gyro column order

On iOS, `rotationRate.alpha` is the rate about **x**, `beta` about **y** and
`gamma` about **z** (deg/s, right-handed). The W3C spec says something else.

- `meta.imu_axes == "device_xyz"`: columns `rx, ry, rz` are about x, y, z. Use
  them as they are.
- No `imu_axes` (null or missing): the page wrote `[beta, gamma, alpha]`, so the
  columns hold **[about y, about z, about x]**. Remap: **x = rz, y = rx, z = ry**.
  Replaying the first recording confirmed this order.

```python
def imu_device_xyz(imu: np.ndarray, imu_axes: str | None) -> np.ndarray:
    """Recording.imu rows [t, ax, ay, az, rx, ry, rz] with rotation about device x, y, z."""
    if imu_axes == "device_xyz":
        return imu
    fixed = imu.copy()
    fixed[:, 4] = imu[:, 6]  # about x was stored in rz
    fixed[:, 5] = imu[:, 4]  # about y was stored in rx
    fixed[:, 6] = imu[:, 5]  # about z was stored in ry
    return fixed
```

If `sensors.jsonl` has a `hello` line, the IMU lines after it follow that
hello's `imu_axes`. Check it agrees with `meta.json`.

### Accelerometer sign (iOS)

`ax, ay, az` are iOS `accelerationIncludingGravity`: CoreMotion's (user
acceleration + gravity) × 9.80665, where gravity points **toward the ground**.

- At rest the reading **is** the gravity vector and points down: face up flat
  gives `az ≈ −9.8`; upright in portrait gives `ay ≈ −9.8`. The W3C spec gives
  the opposite sign.
- With `G` = low-passed reading: **up = −G/|G|**, and **true linear
  acceleration = G − reading** (`IOS_ACCEL_SIGN = −1` in
  [`motion.py`](../service/drive_service/motion.py)).
- Camera pitch from gravity (0 = level, negative = down): `asin(G_z / |G|)`.
  Use this to check the hand-entered `mount.pitch_deg`.

### Derived motion conventions (`motion` lines and live status)

- Turn rate = rotation vector · up. **Positive = turning left** (counter-clockwise
  seen from above).
- Forward = the camera axis (−z) flattened onto the horizontal plane. Left =
  up × forward.
- `accel_fwd_mps2` > 0 = speeding up along forward; `accel_lat_mps2` > 0 =
  toward the chair's left.

These are measured against gravity and the camera, so they work in portrait,
landscape or a tilted mount.

For old recordings without `motion` lines, you can still get the turn rate,
with a causal low-pass so it matches what the model would see live:

```python
imu = imu_device_xyz(rec.imu, rec.meta.get("imu_axes"))
alpha = 0.02                     # ~0.8 s at 60 Hz
g = imu[0, 1:4].copy()
yaw_dps = np.empty(len(imu))
for i, row in enumerate(imu):
    g += alpha * (row[1:4] - g)  # gravity, pointing down
    yaw_dps[i] = row[4:7] @ (-g / np.linalg.norm(g))   # + = left
```

## Frame geometry

- **Size.** The camera opens at about 1280×720. Each frame is scaled so its long
  side is `maxWidth` (default **640**), JPEG quality 0.7, so 640×360 or 360×640.
  Both can be changed on the phone; read `w`, `h` per frame.
- **Orientation: upright, and landscape when the phone is mounted sideways.**
  A web page cannot lock orientation on iPhone, so the phone page turns each
  frame itself from gravity (`rot` in `frames.jsonl` says by how much). Mount
  the phone sideways and frames come out landscape (`w > h`, e.g. 640×360) and
  upright, whether or not iOS rotation lock is on. An upright phone gives
  portrait frames: FlowPilot expects landscape 16:9 (384×216), so skip or crop
  those. Recordings before `rot` existed (e.g. the first one, 360×640) are
  portrait. Always check `w > h` and look at a frame before training.
- **Orientation can change mid-recording** if the phone rotates: `w`/`h` swap
  between lines. Drop or split those stretches.
- **Lens.** The page picks "Back Ultra Wide Camera" when Safari offers it
  (`meta.camera.label`). The **field of view is unknown until calibrated**.
  Apple quotes 120° for the ultra-wide, but the 16:9 video Safari gets is a
  crop, and how much narrower it is has not been measured. FlowPilot expects
  about 100° horizontal. Calibrate once (for
  example a checkerboard with OpenCV) on the same phone, lens, size and
  orientation, and keep the intrinsics with the recordings.
- **Mount.** `meta.mount.height_m` (lens above ground) and `pitch_deg` are typed
  in by hand. If they are exactly 1.1 and −10, they are probably the defaults.
  Cross-check pitch with gravity (above).

## Example

Uses `Recording` for frames, GPS, heading and raw IMU, and reads `motion` lines
alongside it (the loader does not read them yet). Tested against the first
recording and a synthetic 60 fps one with drops.

```python
import bisect
import json
from pathlib import Path

import numpy as np

from fsd_ml.recording import FrameRef, Recording

TICK_MS = 50.0            # 20 Hz
FRAME_TOLERANCE_MS = 25.0
MOTION_STALE_MS = 250.0


def load_motion(folder: Path) -> np.ndarray:
    """[t_ms, speed_mps, yaw_rate_dps, accel_fwd, accel_lat] per IMU sample; NaN = null."""
    rows = []
    with (folder / "sensors.jsonl").open() as lines:
        for line in lines:
            record = json.loads(line)
            if record.get("type") == "motion":
                for dt_ms, *values in record["samples"]:
                    rows.append([record["t"] + dt_ms, *(np.nan if v is None else v for v in values)])
    motion = np.array(rows, dtype=np.float64).reshape(-1, 5)
    return motion[np.argsort(motion[:, 0], kind="stable")]


def select_20hz(frames: list[FrameRef]) -> list[tuple[float, FrameRef | None]]:
    """Newest frame at or before each 50 ms tick, or None if none within the tolerance."""
    if not frames:
        return []
    times = [f.t_ms for f in frames]
    picked = []
    for tick in np.arange(times[0], times[-1] + TICK_MS / 2, TICK_MS):
        i = bisect.bisect_right(times, tick) - 1
        ok = i >= 0 and tick - times[i] <= FRAME_TOLERANCE_MS
        picked.append((float(tick), frames[i] if ok else None))
    return picked


def gaps(picked: list[tuple[float, FrameRef | None]]) -> list[tuple[float, float]]:
    """(first, last) tick of each run of empty ticks."""
    runs: list[tuple[float, float]] = []
    for tick, frame in picked:
        if frame is None:
            if runs and tick - runs[-1][1] == TICK_MS:
                runs[-1] = (runs[-1][0], tick)
            else:
                runs.append((tick, tick))
    return runs


def motion_at(motion: np.ndarray, t_ms: float) -> np.ndarray | None:
    """Newest motion row at or before t_ms, if fresh."""
    i = int(np.searchsorted(motion[:, 0], t_ms, side="right")) - 1
    return motion[i] if i >= 0 and t_ms - motion[i, 0] <= MOTION_STALE_MS else None


rec = Recording("../recordings/20261114T163514Z")
motion = load_motion(rec.folder)
picked = select_20hz(rec.frames)
missing = sum(frame is None for _, frame in picked)
print(f"{len(picked)} ticks, {missing} empty, gaps: {gaps(picked)[:5]}")

for tick, frame in picked:
    if frame is None:
        continue
    row = motion_at(motion, frame.t_ms)
    speed = None if row is None or np.isnan(row[1]) else float(row[1])
    yaw_dps = None if row is None or np.isnan(row[2]) else float(row[2])
    fix = rec.gps_at(frame.t_ms)        # check frame.t_ms - fix.t_ms yourself
    compass = rec.heading_at(frame.t_ms)
    image = rec.image(frame)            # RGB uint8, (h, w, 3)
```

Run it from `ml/` (`uv run python …`).

## Live feed for the model (`/ws/model`)

What `fsd_ml.live.LiveFeed` reads. The driving model's own feed, separate from
the iPad's viewer feed.

- The service pushes **every frame the moment it lands**, not on a timer: the
  JSON status, then the frame as `[uint32 LE header length][header JSON][JPEG]`,
  built together so the status describes that frame.
- Header: `seq`, `t` (phone clock), `w`, `h`, `rot` (if the phone sent it) and
  `age_ms`: how long ago the phone captured the frame, on the service's clock,
  when it was sent, or null before the phone's clock is synced. Capture time on
  the reader's clock = arrival − `age_ms`, so the two machines' clocks never
  need to agree. `LiveFeed.window()` spaces frames by this capture time, so a
  stall that delivers several frames at once still gives evenly spaced views.
  If any frame in its history has no capture time, it uses arrival times for
  all of them rather than mix the two clocks.
- With no new frame the status still comes every ~33 ms, so a reader sees the
  camera go stale at once.
- The status has `motion` (`speed_mps`, `speed_source`, `yaw_rate_dps`,
  `accel_fwd_mps2`, `accel_lat_mps2`, `age_ms`, `fresh`), `gps`, `heading`
  (`deg` true north when the declination is known), `clock_synced`,
  `sensors_ok`, `camera` and `mount`. Use a value only when its `fresh` is true.
- At most 2 model feeds, counted apart from viewers so open iPads never lock the
  model out; a third is closed with `4429`. Anything a reader sends is ignored.

The viewer feed (`/ws/viewer`, for the iPad and debug pages) wakes every ~33 ms
and sends the status and the newest JPEG, with no header, if it changed. It
suits a screen, not a model: a frame can wait up to a tick, and a 30 Hz
newest-only feed sampled at 20 Hz repeats or skips frames.

## Loader additions worth making in `ml/`

None of these exist yet; `recording.py` is unchanged.

1. `Recording.motion` (the array above) and `motion_at(t)`.
2. Apply the gyro remap in the loader using `meta.imu_axes` and any `hello`
   lines, so `rec.imu` is always about device x, y, z.
3. `frames_at(hz=20, tolerance_ms=25)` returning ticks, picks and gap runs, plus
   `windows` built on those ticks instead of consecutive frames.
4. De-duplicate GPS fixes by `fix_t`.
5. Expose `hello` lines (a mid-recording camera or orientation change).
6. True heading from the compass using WMM declination at the fix.
7. A check that fails on portrait or mixed-orientation frames for models that
   need landscape.

## Pitfalls

- [ ] Picked frames by `t`, not by index, `seq`, `n` or `recv_ms`.
- [ ] Never used a frame or reading from after the tick or frame time.
- [ ] Reported gap runs, and kept context windows inside one contiguous segment.
- [ ] Did not use `Recording.windows()` or `Recording.fps` at the camera rate.
- [ ] Remapped the gyro when `imu_axes` is missing (x = rz, y = rx, z = ry).
- [ ] Remembered the iOS accel sign: at rest the reading points **down**;
      linear acceleration = G − reading.
- [ ] Handled recordings with no `motion` lines (speed unknown, not 0).
- [ ] Treated null speed as unknown, not 0, and checked reading ages (motion
      250 ms).
- [ ] Converted turn rate to rad/s for `Driver.Motion`; positive = left.
- [ ] Did not use the motion speed as ground truth or odometry.
- [ ] Converted the compass from magnetic to true before comparing with GPS
      course or a map.
- [ ] De-duplicated GPS fixes resent after a reconnect (same `fix_t`).
- [ ] Checked `w > h` per frame, and that orientation does not change mid-drive.
- [ ] Did not assume a field of view; calibrated the lens.
- [ ] Checked `meta.dropped` and `meta.error`.
- [ ] Did not assume 60 fps: measured the rate from `frames.jsonl`.
