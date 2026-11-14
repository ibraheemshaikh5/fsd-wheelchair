# Chair link (Mac side)

How the driving service on the Mac talks to the Arduino UNO Q on the chair. This page covers the service half: the protocol, auth, what the service enforces, and how to test without a board. The board half (sketch and Linux bridge) has its own notes.

The message shapes follow [outputs.md](outputs.md) section 1, with the transport now decided: a WebSocket over Tailscale.

Code: `service/drive_service/chair/` (a self-contained package). Fake board: `service/scripts/fake_chair.py`.

## Connection

The board is the WebSocket **client**. It dials the Mac:

| Where | URL |
|-------|-----|
| On the tailnet | `wss://wheelchair-mac.taile75b1b.ts.net/ws/chair` |
| On the Mac (dev) | `ws://127.0.0.1:8000/ws/chair` |

It must send the header `Authorization: Bearer <token>` and **no** `Origin` header.

The service refuses the handshake (close code 1008 before accept, so the client sees HTTP 403) when:

- the service has no token configured (`DRIVE_CHAIR_TOKEN` unset): every chair is refused, fail closed;
- the token is missing or wrong (compared with `hmac.compare_digest`);
- the request carries any `Origin` header. Only browsers send one, and no web page may drive the motors, not even the service's own pages.

One chair at a time. A newer authenticated connection takes over; the older one is closed with **4410**. Ten invalid messages in a row close it with **1008**.

| Code | Why the service closed the link |
|------|------|
| 1008 | Refused handshake, or ten invalid messages in a row |
| 4408 | The board sent nothing for `idle_timeout_ms` (2 s), or a drive send was blocked for over `ttl_ms` (the board stopped reading) |
| 4409 | The board's `applied_seq` trails the newest command by more than `max_command_lag_ms` (500 ms): it is driving old commands |
| 4410 | Replaced by a newer chair connection |

The board reconnects (with backoff) after any of these.

## Messages

### Board to service: `hello` (first message)

```json
{"type": "hello", "role": "chair", "firmware": "uno-q-0.1", "max_speed_mps": 0.5, "max_turn_radps": 0.5}
```

`max_speed_mps` and `max_turn_radps` are the caps compiled into the sketch. The service never asks for more than the lower of these and its own caps. A status sent before hello, or a second hello, counts as an invalid message.

### Service to board: `drive` (20 Hz, always)

```json
{"type": "drive", "seq": 48213, "t": 1794650000123, "v_mps": 0.0, "w_radps": 0.0, "ttl_ms": 200}
```

| Field | Meaning |
|-------|---------|
| `seq` | Up by one each command. Keeps counting across reconnects while the service runs |
| `t` | Service wall clock, ms |
| `v_mps` | Forward speed, never negative |
| `w_radps` | Turn rate, positive turns left |
| `ttl_ms` | How long the board may hold this command (default 200, never more than 500) |

Stopping is a command: `0, 0` is sent on every tick while stopped. The cadence is paced by the service's event loop, not by anything the board sends.

### Board to service: `chair` status (10 to 20 Hz)

```json
{
  "type": "chair", "t": 1794650000140, "mode": "auto",
  "joystick_active": false, "estop": false, "battery_pct": null,
  "applied_seq": 48213, "output": {"v_mps": 0.0, "w_radps": 0.0},
  "stopped_reason": "watchdog"
}
```

`mode` is `auto` or `manual`. `stopped_reason` is one of `watchdog`, `estop`, `joystick`, `no_link`, `limit` or `null`. `battery_pct` and `applied_seq` may be `null`.

Every message is validated: unknown fields, missing fields, wrong types (`"false"` for a boolean), NaN, infinity, out-of-range numbers and messages over 2 KB are refused.

## Safety

The rules that keep the chair safe live **on the microcontroller**, independent of Linux and the network (see [architecture.md](architecture.md#safety)):

- Output is zero unless the board holds a command newer than its `ttl_ms` (default 200 ms, never more than 500).
- `v` is never negative; `|v|` and `|w|` are clamped to caps compiled into the sketch (dev defaults 0.5 m/s, 0.5 rad/s).
- E-stop input is normally closed with `INPUT_PULLUP`: LOW means OK, HIGH means pressed **or wire broken**, so the chair stops. An unwired dev board stays stopped until the e-stop pin is jumpered to GND.
- Joystick-active input (`INPUT_PULLUP`, LOW = rider touching the stick) stops the output and reports `manual`.
- Linux losing the service connection tells the microcontroller to stop at once.

What the service adds on top, as defence in depth:

- It sends stop unless a bench test is running and unexpired.
- Bench tests are checked against the service's caps and the board's reported caps. An out-of-range request is refused with 422, never trimmed.
- A bench test starts only once the chair has reported a status no older than `status_stale_ms` (500 ms) showing no e-stop and no joystick. Before the first status, with a stale one, or while it reports e-stop or manual, the request gets 409.
- A running bench test is cancelled as soon as the chair reports e-stop, joystick or manual, so it can never resume when the rider lets go, and as soon as its status goes stale.
- A new connection or a disconnect clears any bench test.
- Commands that reach the board late end the link (next section).

### Late commands

If the Mac-to-board direction stalls while TCP stays open (a Tailscale path hiccup, congestion), drive commands queue up in the service's write buffer and the kernel. When the path clears the board would get a burst of old commands, each with a fresh `ttl_ms` by its own clock, and could drive a bench command the operator already stopped. Both ends guard against this.

Service (done):

- Every drive send is bounded by `ttl_ms`. A send blocked longer means the board stopped reading: the link closes with 4408 and the bench test is cleared.
- Every status is checked: `(last sent seq - applied_seq) * interval` over `max_command_lag_ms` closes the link with 4409 and clears the bench test. `applied_seq: null` (the board is stopped) and a seq higher than the last one sent (left over from before a service restart) are not lag.

Board (its half of the contract; the board lane implements it):

- Drop a `drive` whose `t` is older than `ttl_ms` against the board's NTP-synced wall clock (`now_ms - t > ttl_ms`), and one more than `ttl_ms` in the future. Count from receipt only after that check. This is the guard that matters: closing the socket does not take back bytes already queued, so only the board can refuse them.
- Keep reporting `applied_seq` as the seq it is driving on, and `null` while stopped, so the service can measure the lag.
- Until the board clock is synced, it should treat every command as too old and stay stopped.

The output to the wheelchair's joystick input is a placeholder until the WOLF chair is opened and its wiring is known.

## HTTP API

All responses use the service envelope `{success, data, error}`.

| Method | Path | Does |
|--------|------|------|
| GET | `/api/chair` | Link status: `connected`, `hello`, `status`, `status_age_ms`, `status_fresh` (age within 500 ms), `last_command`, `test` (`active`, `v_mps`, `w_radps`, `remaining_ms`), `rejected` |
| POST | `/api/chair/test` | `{"v_mps": 0.2, "w_radps": 0.0, "duration_ms": 1000}`: drive for that long. Caps 0.5 m/s, 0.5 rad/s, 3000 ms |
| DELETE | `/api/chair/test` | Stop now |

POST and DELETE accept requests from the service's own pages, configured `DRIVE_ALLOWED_ORIGINS`, or clients that send no `Origin` (curl). Other sites get 403.

| Status | When |
|--------|------|
| 400 | Body is not JSON, or `content-length` is not a number |
| 403 | Request came from another site |
| 409 | Chair not connected, no hello yet, no status yet, status older than 500 ms, or it reports e-stop or manual |
| 413 | Body over 1 KB (refused from `content-length` before reading; a chunked body is cut off once it passes 1 KB) |
| 422 | Wrong fields or types, or a value over the caps |

## From the model to the chair

The model runs in the [viewer](../ml/README.md#watch-a-model-on-the-chairs-live-camera), not in the service. Only its decision crosses over: a forward speed and a turn rate. Paths, scores and frames stay in the viewer.

```
model viewer (ml/)  --POST /api/chair/drive-->  driving service  --drive, 20 Hz, /ws/chair-->  Arduino
```

| Method | Path | Does |
|--------|------|------|
| POST | `/api/chair/drive` | `{"v_mps": 0.3, "w_radps": 0.1, "hold_ms": 600}`: the model's latest decision |
| DELETE | `/api/chair/drive` | Stop now |

- **Holds, then stops.** The service passes the decision on, on every tick, until `hold_ms` runs out (at most `DRIVE_CHAIR_MAX_DRIVE_HOLD_MS`, 600 ms) or a newer one replaces it. A model that stalls or crashes stops the chair within that time; the board's own `ttl_ms` follows.
- **Only the model.** The request must carry `Authorization: Bearer <DRIVE_CHAIR_DRIVER_TOKEN>`, a different token from the board's. Any request with an `Origin` header is refused (403), so no web page can drive, not even the service's own. With no driver token set, every request is refused.
- **Same readiness as a bench test.** 409 until the chair is connected and has reported a fresh status with no e-stop and no joystick. An e-stop or joystick report cancels the held decision.
- **Lowered, not refused.** Unlike a bench test, a speed or turn rate over the caps is lowered to them, since the model plans its own speed. The response's `limited` says so. Values no chair could mean (negative speed, over 5 m/s or 5 rad/s, NaN, extra fields) are refused with 422.
- **Its own rate budget** (`DRIVE_MAX_DRIVE_RATE_HZ`, 30 a second), so model commands cannot use up the budget a Stop needs. The sender in `ml/` sends at most 20 a second, the rate the board is sent commands.
- `GET /api/chair` shows the held decision under `test`, with `source: "model"`.

The wire format to the board is the `drive` message above, built in one place (`protocol.DriveCommand`). If the board team settles on a different format, that class is the only thing to change; the model side is unaffected.

The model side is `ml/fsd_ml/chair_out.py`. In the viewer's live view, tick **Send to chair**. The viewer needs the same driver token as the service, in `DRIVE_CHAIR_DRIVER_TOKEN`. Leaving the live view, reconnecting or unticking sends a stop.

## Configuration

`ChairSettings` reads `DRIVE_CHAIR_*` environment variables, and also `service/.env` (gitignored). Copy [`chair.env.example`](../service/drive_service/chair/chair.env.example). Never commit a real token.

| Variable | Default | Notes |
|----------|---------|-------|
| `DRIVE_CHAIR_TOKEN` | unset | At least 16 characters. Unset or blank refuses every chair. A value containing `replace-with` or `change-me` (any case), such as the example file's, stops the service at startup |
| `DRIVE_CHAIR_DRIVER_TOKEN` | unset | The model's token for `/api/chair/drive`. At least 16 characters, different from the chair token. Unset refuses every model command |
| `DRIVE_CHAIR_MAX_DRIVE_HOLD_MS` | 600 | Longest a model decision is held. 50 to 1000 |
| `DRIVE_CHAIR_RATE_HZ` | 20 | Drive commands per second |
| `DRIVE_CHAIR_TTL_MS` | 200 | 50 to 500, and at least two command intervals |
| `DRIVE_CHAIR_MAX_TEST_SPEED_MPS` | 0.5 | Hard ceiling 1.0 |
| `DRIVE_CHAIR_MAX_TEST_TURN_RADPS` | 0.5 | Hard ceiling 1.0 |
| `DRIVE_CHAIR_MAX_TEST_DURATION_MS` | 3000 | Hard ceiling 10000 |
| `DRIVE_CHAIR_STATUS_STALE_MS` | 500 | Older status shows `status_fresh: false` |
| `DRIVE_CHAIR_IDLE_TIMEOUT_MS` | 2000 | Silent board is closed with 4408 |
| `DRIVE_CHAIR_MAX_COMMAND_LAG_MS` | 500 | 50 to 2000 and at least `ttl_ms`. A board applying commands later than this is closed with 4409 |

Make a token with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`.

## Wiring it into the app

`drive_service/chair/__init__.py` exposes `build_chair(settings, allowed_origins, time_source)`, returning the API router, the socket router and their shared state. In `service/drive_service/app.py`:

```python
from drive_service.chair import ChairSettings, build_chair

CONTROL_PREFIXES = ("/api/recording/", "/api/destination", "/api/chair/")

# in create_app, next to the other include_router calls:
chair = build_chair(ChairSettings(), settings.allowed_origins, time_source)
app.include_router(chair.api_router)
app.include_router(chair.ws_router)
```

`/api/chair/` in `CONTROL_PREFIXES` puts bench start and stop on the control rate budget and its origin check; `GET /api/chair` stays on the read budget.

## Testing without a board

With the service running and the same token in both shells:

```bash
cd service
export DRIVE_CHAIR_TOKEN=...            # same value the service has
uv run python -m scripts.fake_chair --url ws://127.0.0.1:8000/ws/chair --token-env DRIVE_CHAIR_TOKEN
```

In another shell:

```bash
curl -s localhost:8000/api/chair
curl -s -X POST localhost:8000/api/chair/test -H 'content-type: application/json' \
  -d '{"v_mps": 0.2, "w_radps": 0.0, "duration_ms": 1000}'
curl -s -X DELETE localhost:8000/api/chair/test
```

The fake board applies the same rules as the sketch: ttl watchdog, caps (reported as `limit`), e-stop and joystick. `--estop` and `--joystick` simulate those inputs; `--seconds N` stops after N seconds; `--max-speed` and `--max-turn` set its caps. It exits 0 when the service closes the connection, 3 when the token is refused, 4 when the service cannot be reached.

Tests build a small app holding only the chair routers, including an end-to-end run of the fake board against a real local server. The project-wide coverage gate (80% of all of `drive_service`) fails on a subset, so run the chair tests either without coverage or with coverage scoped to the chair code:

```bash
cd service
uv run pytest -q tests/test_chair_*.py --no-cov
uv run pytest -q tests/test_chair_*.py --cov-reset --cov=drive_service.chair --cov=scripts.fake_chair --cov-fail-under=80
```

The whole suite (`uv run pytest -q`) runs them too, under the normal gate.
