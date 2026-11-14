# Merging the working chair control into the model link

This page is for whoever (person or coding agent) merges the Arduino code that already drives the real wheelchair into branch `feat/chair-model-link`. That branch already gets the driving model's decisions all the way to the Arduino UNO Q. The only thing missing is the last step, where the board turns a command into real motor movement. Right now that step is a placeholder.

Read this page first, then [chair-link.md](chair-link.md) (Mac side, protocol) and [../chair/README.md](../chair/README.md) (board side, safety, pins) for the details.

## How a model decision reaches the wheels

```
camera ─► model viewer (ml/)                     decides a forward speed v and a turn rate w
            │  POST /api/chair/drive  {v_mps, w_radps, hold_ms}
            ▼
          driving service (service/, on the Mac)   holds the decision, caps it, checks e-stop/joystick
            │  WebSocket /ws/chair, "drive" message at 20 Hz, over Tailscale
            ▼
          UNO Q Linux side (chair/unoq-app/python/) keeps only the newest command
            │  Bridge RPC drive(seq, v, w, ttl_ms)
            ▼
          UNO Q STM32 sketch (chair/unoq-app/sketch/)
            chair_control.h  ─ safety rules, decides out_v / out_w
            apply_output(v, w)  ◄── THE MERGE POINT: today a PWM placeholder
            ▼
          wheelchair
```

Everything above `apply_output()` is done and tested. It doesn't need changes for the merge.

## What the model sends

Only two numbers ever reach the sketch:

| Value | Unit | Range the sketch sees | Meaning |
|-------|------|------------------------|---------|
| `v` | m/s | `0` to `MAX_SPEED_MPS` (0.5) | Forward speed. **Never negative**: the model never reverses |
| `w` | rad/s | `-MAX_TURN_RADPS` to `+MAX_TURN_RADPS` (±0.5) | Turn rate. **Positive = turn left**, negative = turn right |

`v = 0, w = 0` means stop. The service sends a stop command on every tick while stopped, so stop is a normal command, not a missing one.

Turning on the spot is `v = 0, w ≠ 0`. A gentle curve is `v > 0, w ≠ 0`.

## The merge point: `apply_output()`

In [`chair/unoq-app/sketch/sketch.ino`](../chair/unoq-app/sketch/sketch.ino):

```cpp
// The ONE place the output leaves the board.
void apply_output(float v, float w) {
  write_axis(PIN_SPEED_AXIS, chair::axis_duty(v, MAX_SPEED_MPS));
  write_axis(PIN_TURN_AXIS, chair::axis_duty(-w, MAX_TURN_RADPS));
}
```

`refresh()` calls it on every `loop()` with the output the safety logic allows (`state.out_v`, `state.out_w`). It is the only place the board touches the chair.

**What to do:** replace the body of `apply_output()` (and `write_axis()` / `chair::axis_duty()` if they don't fit) with the code that already moves the chair. Map `v` and `w` onto whatever that code takes: joystick voltages, motor-driver PWM, relays, and so on. For example, if the working code has a function like `drive_chair(forward, turn)` that takes -1..1:

```cpp
void apply_output(float v, float w) {
  const float forward = v / MAX_SPEED_MPS;   // 0..1
  const float turn = w / MAX_TURN_RADPS;      // -1..1, positive = left
  drive_chair(forward, turn);                 // the friend's existing code
}
```

Also update:

- `PIN_SPEED_AXIS` / `PIN_TURN_AXIS` and their `pinMode` / `analogWriteResolution` setup in `setup()` to the pins the real wiring uses. Keep `D2` (e-stop) and `D4` (joystick active) free, or move them and update the pin table in `chair/README.md`.
- `MAX_SPEED_MPS` / `MAX_TURN_RADPS` (top of `sketch.ino`) if the chair's real top speed at full output is known. These are hard caps the Mac cannot raise. The board reports them to the service in `hello`, so nothing else needs to change. Keep them low for first tests.
- The "PLACEHOLDER OUTPUT" comments in `sketch.ino` and the warning box in `chair/README.md` once the real output is in.

## Rules the merged code must keep

These are what make it safe to let a model drive. Don't remove them while merging:

1. **Don't move the chair anywhere except `apply_output()`.** Don't drive the motors from `setup()`, from a Bridge handler, from a timer or from a second loop. `chair_control.h` decides when output is allowed. Anything that bypasses it bypasses the watchdog and the e-stop.
2. **`apply_output(0, 0)` must mean fully stopped.** After a reset, every pin is high-impedance until `setup()` runs. The real interface has to treat a floating or missing signal as stop (for example pulled to joystick centre, or a relay that drops without a signal).
3. **Keep `loop()` fast and non-blocking.** No `delay()`, no long `while` loops, no blocking serial reads. The hardware watchdog resets the board if `refresh()` doesn't run for 200 ms, and the command watchdog stops the chair if no fresh command arrives within `ttl_ms` (200 ms). Blocking code will either stop the chair or reset the board.
4. **No reverse from the model.** If the working code can reverse, keep that out of the model path: `v` is always ≥ 0 here. Reverse for manual or testing use should stay a separate, explicit path, not a negative `v`.
5. **Keep the e-stop and joystick inputs.** `D2` is a normally-closed e-stop to GND (open = stop, so a broken wire also stops). `D4` LOW = rider is on the joystick, which hands control back to the rider. On a bench without these wired, jumper `D2` to GND or the chair stays stopped (that is intended).
6. **Keep the re-arm rule.** After boot, e-stop, joystick take-over or a link drop, the chair waits for a zero command before it will move. Don't "fix" this. It stops the chair from jumping when the rider lets go.
7. **Keep `chair_control.h` free of Arduino calls.** It's plain C++ so it can be unit-tested on the Mac. Hardware code belongs in `sketch.ino`.

## Merge checklist

- [ ] Check out `feat/chair-model-link`, then bring in the working chair-control code (merge his branch, or copy his motor code into the sketch).
- [ ] Replace the body of `apply_output()` with the real output. Map `v` (0..max, forward) and `w` (±max, + = left).
- [ ] Update output pins and their `setup()` code. Leave `D2` and `D4` as safety inputs.
- [ ] Remove any other code path that writes to the motors (his old `loop()` driving logic, serial or joystick test modes) so `apply_output()` is the only one.
- [ ] Make sure nothing in `loop()` blocks.
- [ ] Set `MAX_SPEED_MPS` / `MAX_TURN_RADPS` to safe values for the first real run.
- [ ] Run the tests (below). If `axis_duty` changes or is removed, update `chair/tests/cpp/test_chair_control.cpp` to match.
- [ ] Compile the sketch on the board (below).
- [ ] Bench test with the **wheels off the ground** before letting the model drive.

## Tests

```bash
cd chair && make test        # C++ host tests of chair_control.h + Python link tests
cd chair && make lint
cd service && uv run pytest -q
```

To compile the sketch on the board without uploading:

```bash
adb shell mkdir -p /tmp/chair-sketch-check/sketch
adb push chair/unoq-app/sketch/. /tmp/chair-sketch-check/sketch/
adb shell 'cd /tmp/chair-sketch-check && TMPDIR=/tmp arduino-cli compile --profile default sketch'
adb shell rm -rf /tmp/chair-sketch-check
```

## Bringing it up end to end

Wheels off the ground for all of this.

1. **Tokens.** On the Mac, set `DRIVE_CHAIR_TOKEN` (the board's token) and `DRIVE_CHAIR_DRIVER_TOKEN` (the model's token, different) in `service/.env`. Generate each with `openssl rand -hex 32`. On the board, copy `chair/unoq-app/config.example.json` to `config.json` and set the same `DRIVE_CHAIR_TOKEN` value as `token`. Never commit either file.
2. **Start the driving service** on the Mac, as usual for this repo.
3. **Deploy the board app** (see [Deploy in chair/README.md](../chair/README.md#deploy)) and start it from App Lab. `GET /api/chair` on the Mac should show `connected: true` with a fresh status.
4. **Bench test without the model:**
   ```bash
   curl -s -X POST localhost:8000/api/chair/test -H 'content-type: application/json' \
     -d '{"v_mps": 0.2, "w_radps": 0.0, "duration_ms": 1000}'
   curl -s -X DELETE localhost:8000/api/chair/test
   ```
   Check that forward is forward, positive `w_radps` turns **left**, and that the chair stops by itself after `duration_ms`. Also try pulling the network or killing the app: the chair must stop within about 200 ms.
5. **Let the model drive.** Run the viewer (`cd ml && uv run --group onnx python -m fsd_ml.viewer`, with `DRIVE_CHAIR_DRIVER_TOKEN` set) and tick **Send to chair** in the live view. Unticking or leaving the view sends a stop.

## Where things live

| Path | What |
|------|------|
| `chair/unoq-app/sketch/sketch.ino` | Pins, `apply_output()`, Bridge RPCs. **Merge here** |
| `chair/unoq-app/sketch/chair_control.h` | Safety logic (watchdog, caps, e-stop, re-arm). Leave alone |
| `chair/unoq-app/sketch/hw_watchdog.h` | STM32 hardware watchdog |
| `chair/unoq-app/python/chair_link/` | Board Linux side: WebSocket to the Mac, Bridge to the sketch |
| `service/drive_service/chair/` | Mac side: `/ws/chair`, `/api/chair/*`, caps and checks |
| `service/scripts/fake_chair.py` | Fake board for testing the Mac side without hardware |
| `ml/fsd_ml/chair_out.py` | Sends the model's decision to the service |
