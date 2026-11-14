# Chair link (Arduino UNO Q)

The App Lab app that sits on the wheelchair. It connects to the driving service
on the Mac, receives drive commands (speed and turn rate) and turns them into
the chair's joystick input, while the board's microcontroller enforces every
safety rule on its own.

```
driving service (Mac)  --wss /ws/chair-->  Linux side (python/)  --Bridge RPC-->  STM32 sketch  -->  chair
                       <--chair status--                         <--status-----
```

> **The output to the chair is a placeholder.** How the WOLF wheelchair's
> joystick is wired, and what voltages it expects, is unknown until the chair
> is opened. Do not connect the PWM pins to the chair until that interface has
> been measured and `apply_output()` rewritten for it.

## Layout

| Path | What |
|------|------|
| `unoq-app/app.yaml` | App Lab app descriptor |
| `unoq-app/config.example.json` | Config template. Copy to `config.json` (gitignored) |
| `unoq-app/sketch/chair_control.h` | All safety logic, pure C++ (no Arduino calls) |
| `unoq-app/sketch/sketch.ino` | Reads pins, runs `chair_control.h`, drives outputs, serves Bridge RPCs |
| `unoq-app/sketch/hw_watchdog.h`, `watchdog_timing.h` | STM32 independent hardware watchdog (IWDG) |
| `unoq-app/python/main.py` | Thin App Lab entry point |
| `unoq-app/python/chair_link/` | Config, wire protocol, Bridge adapter, WebSocket client |
| `tests/` | pytest for `chair_link`, `tests/cpp/` host test for `chair_control.h` |

## Safety design

The rules live in the microcontroller (`chair_control.h`), not in Linux and not
on the Mac, so a crashed Python process, a dead Linux side or a dropped network
all end in a stopped chair.

- **Watchdog.** Output is zero unless the board holds a command younger than
  its `ttl_ms`. `ttl_ms` is capped at 500 ms whatever the service sends. An
  expired command is forgotten for good, so it cannot come back when
  `millis()` wraps after ~49.7 days. A newer command (higher `seq`) is obeyed
  again without re-arming.
- **Hardware watchdog.** The STM32's independent watchdog (IWDG, own 32 kHz
  clock) resets the microcontroller if `refresh()` has not run for 200 ms, for
  example because a Bridge handler, the router transport or a Zephyr fatal
  error stopped `loop()`. Without it the PWM timer would keep the last duty. It
  is started at the end of `setup()`; until it runs, drive commands are refused.
  It cannot be stopped once started. The counter is frozen while a debugger
  halts the core so flashing over SWD is not cut short. After the reset every
  pin is **high-impedance** until `setup()` centres the axes again (the
  sketch's re-arm rule then keeps the chair stopped until a zero command). The
  real chair interface must therefore treat a floating or absent signal as stop
  (for example a pull to the joystick's centre voltage, or a relay that drops
  out without a heartbeat). Not yet exercised on hardware: confirm with a
  deliberate hang (e.g. a `while (true) {}` in a test build) that the board
  resets and that App Lab can still flash it.
- **Speed caps.** `0 <= v <= 0.5 m/s` and `|w| <= 0.5 rad/s` (dev defaults,
  compiled into the sketch as `MAX_SPEED_MPS` / `MAX_TURN_RADPS`). Linux cannot
  raise them. A clamped command is reported as `stopped_reason: "limit"`.
- **No reverse.** Negative `v` is never output. The Linux side also rejects it.
- **E-stop.** Normally-closed switch from `D2` to GND, `INPUT_PULLUP`. LOW = OK.
  HIGH means pressed *or wire broken*, and stops the chair. An unwired dev
  board therefore stays stopped until `D2` is jumpered to GND.
- **Joystick take-over.** `D4`, `INPUT_PULLUP`, LOW = rider touching the
  joystick. Stops the chair and reports `mode: "manual"`.
- **Re-arming.** After boot, a link loss (Linux calls `stop`), the e-stop or a
  joystick take-over, the chair stays stopped until it receives a **zero
  command** (`v_mps` 0, `w_radps` 0) while both inputs are clear. Only then is a
  moving command obeyed. Releasing the e-stop or the joystick never makes the
  chair move by itself. The service already sends zero commands every tick
  while stopped, so this only requires it to stop (disengage) when it sees
  `manual`, `estop` or a reconnect, which it must do anyway.
- **Ordering.** Commands with a `seq` not newer than the last one are ignored.
  `stop` resets this, so a restarted service counting from 0 is accepted.
- **No replay.** The Linux side keeps only the newest command in a single slot
  and forwards that one, never one older than the last it sent. Commands that
  queued up during a Wi-Fi or Tailscale stall, or behind a slow Bridge call,
  are dropped instead of being executed one after another (each would get a
  fresh `ttl_ms` on the microcontroller).
- **Link loss.** On any disconnect, error, bad message or failed Bridge call the
  Linux side calls the sketch's `stop` immediately; the watchdog covers the
  case where Linux itself dies.

Reason codes (status array and `stopped_reason`): 0 none, 1 `watchdog`,
2 `estop`, 3 `joystick`, 4 `no_link`, 5 `limit`.

## Pins and LEDs

From the zephyr core's UNO Q variant (`arduino_uno_q_stm32u585xx`, core 0.52.0).
The STM32U585's GPIOs are **3.3 V logic**; outputs swing 0 to 3.3 V only.

| Pin | MCU | Use |
|-----|-----|-----|
| `D9` | PB8, TIM4_CH3 | Speed axis, PWM 500 Hz |
| `D10` | PB9, TIM4_CH4 | Turn axis, PWM 500 Hz |
| `D2` | PB3 | E-stop input (NC to GND, `INPUT_PULLUP`) |
| `D4` | PA12 | Joystick-active input (LOW = touched, `INPUT_PULLUP`) |
| `LED4_R/G/B` | PH13-15 | Status LED (active LOW) |

Placeholder axis mapping (`axis_duty` in `chair_control.h`): both axes rest at
50 % duty. Full scale is ±25 % (`kAxisSpan`). Speed goes 50 % to 75 % (forward
only). Positive `w` (turn left) lowers the turn-axis duty. A real joystick
usually needs an analogue voltage around a centre, so expect an RC filter or a
DAC and level shifting once the WOLF interface is known.

Status LED: green = following commands; red = e-stop; red + green = joystick
take-over (manual); blue = waiting (no link, not re-armed, or watchdog).

## Wire protocol

Implemented exactly as specified for `/ws/chair` (see `docs/outputs.md`):

- Connect as a client with `Authorization: Bearer <token>`.
- First message: `{"type":"hello","role":"chair","firmware","max_speed_mps","max_turn_radps"}`
  (the caps are read from the sketch's `limits` RPC, so they always match).
- Receives `{"type":"drive","seq","t","v_mps","w_radps","ttl_ms"}` at 20 Hz.
- Sends `{"type":"chair","t","mode","joystick_active","estop","battery_pct","applied_seq","output":{"v_mps","w_radps"},"stopped_reason"}`
  at `status_hz` (default 10). `battery_pct` is `null` until a battery reading
  is wired. `applied_seq` is `null` whenever the output is not following a
  command.
- Reconnects with exponential backoff and jitter (0.25 s doubling, capped at 5 s).

Bridge RPCs served by the sketch (`provide_safe`, so they run inside `loop()`
with no data races): `drive(seq, v, w, ttl_ms) -> bool`, `stop()`,
`status() -> [mode_auto, joystick_active, estop, applied_seq, v_mm_s, w_mrad_s, reason]`,
`limits() -> [max_v_mm_s, max_w_mrad_s, max_ttl_ms]`.

## Tests

From `chair/`:

```sh
make test-cpp      # clang++ -std=c++17 host tests of chair_control.h and watchdog_timing.h
make test-py       # uv run pytest (fake service on localhost, fake Bridge)
make lint          # ruff
```

Compile check of the sketch on the board (compile only, never upload):

```sh
adb shell mkdir -p /tmp/chair-sketch-check/sketch
adb push unoq-app/sketch/. /tmp/chair-sketch-check/sketch/
adb shell 'cd /tmp/chair-sketch-check && TMPDIR=/tmp arduino-cli compile --profile default sketch'
adb shell rm -rf /tmp/chair-sketch-check
```

`TMPDIR=/tmp` is needed because `adb shell` sets an Android-style `TMPDIR`.

## Configure the token

The service refuses every chair connection unless `DRIVE_CHAIR_TOKEN` is set on
the Mac, and the board must send the same value.

1. Generate a token once: `openssl rand -hex 32`.
2. On the Mac, set `DRIVE_CHAIR_TOKEN` for the driving service.
3. On the board, copy `config.example.json` to `config.json` in the app folder
   and set `token`. `config.json` is gitignored; never commit it.

`config.json` fields: `service_url`, `token` (16+ characters), `firmware`
(label sent in `hello`, 1 to 64 characters, the service's limit), `status_hz`
(1 to 20, default 10). The app refuses to start on a missing or invalid config
and tells the sketch to stop.

`service_url`:

- On the chair: `wss://wheelchair-mac.taile75b1b.ts.net/ws/chair`.
- Dev board on the Mac's USB: `127.0.0.1` on the board is the board itself, not
  the Mac. Forward the port first with `adb reverse tcp:8000 tcp:8000`, then use
  `ws://127.0.0.1:8000/ws/chair`. (If the app's container does not share the
  board's network, use the Tailscale URL instead.)
- Plain `ws://` is accepted only for loopback (`127.0.0.0/8`, `::1`,
  `localhost`) and tailnet hosts (`*.ts.net`, `100.64.0.0/10`,
  `fd7a:115c:a1e0::/48`), where the token cannot be sniffed. Any other host
  must use `wss://`, because the token travels in the `Authorization` header.

## Deploy

1. Copy the app to the board:
   ```sh
   adb push unoq-app/. /home/arduino/ArduinoApps/chair-link/
   adb shell cp /home/arduino/ArduinoApps/chair-link/config.example.json \
                /home/arduino/ArduinoApps/chair-link/config.json
   adb shell nano /home/arduino/ArduinoApps/chair-link/config.json   # set the token
   ```
2. Start it from App Lab, or `arduino-app-cli app start ~/ArduinoApps/chair-link`.
   App Lab compiles and flashes the sketch and runs `python/main.py` in its
   container; `python/requirements.txt` pins `websockets==15.0.1`.
3. Until the e-stop is wired, jumper `D2` to GND or the chair stays stopped.

## Tailscale on the board

The board reaches the Mac over the tailnet. On the board (the `sudo` step asks
for the board password, which you type yourself):

```sh
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Open the login link it prints, then check `tailscale status` shows the Mac.

Still to verify on hardware: the app runs in a Docker container, so confirm
the container can resolve and reach `wheelchair-mac.taile75b1b.ts.net` (look for
"connected to" in the app log). If MagicDNS does not resolve inside the
container, that needs fixing at the board level, not by switching to plain
`ws://` over an IP.
