# Running the phone stream

## Once

```bash
brew install uv node
cd service && uv sync
cd ../web && npm install && npm run build
```

Install [Tailscale](https://tailscale.com/download) on the Mac and on the iPhone, sign in to the same account on both, and turn on HTTPS certificates for your tailnet in the Tailscale admin console (DNS → HTTPS Certificates).

## Each session

1. Start the service on the Mac:

   ```bash
   cd service && uv run python -m drive_service
   ```

2. Publish it on the tailnet over HTTPS (in a second terminal):

   ```bash
   tailscale serve --bg 8000
   ```

   It prints an address like `https://your-mac.your-tailnet.ts.net`. The service accepts any `*.ts.net` host by default; to accept only this Mac, start it with `DRIVE_ALLOWED_HOSTS='["your-mac.your-tailnet.ts.net","localhost","127.0.0.1"]'`.

3. On the iPhone, open that address in Safari and tap **Chair camera**. Tap **Start streaming** and allow camera, motion and location access. Use the page in Safari itself, not as a Home Screen app: a Home Screen app may report the screen lock as held when it is not.

4. On the Mac (or the iPad), open the same address and tap **Sensor viewer**. The phone row says "Connected, sensors OK" once the clocks are synced (about a second after connecting) and the camera and GPS are fresh. Use **Start recording** to save a drive under `recordings/`. Recording needs the phone connected; otherwise the viewer shows the service's reason.

Only one phone streams at a time. Opening the capture page on a second phone or tab takes over, and the first one shows "Another phone or tab took over the stream".

Before the first real drive, run **Device check** on the mounted phone and add its results to [open questions](open-questions.md).

### Keeping the phone streaming

Safari stops the camera when the page is hidden or the screen locks. The capture page then closes its connection, and starts again by itself when it is back in front.

- Keep Safari in front. Settings › Display & Brightness › Auto-Lock › **Never** while driving, if the page shows "Screen lock: Not held".
- Keep the phone on a charger and out of direct sun; streaming heats it.

## Without the phone

Stream synthetic frames and a GPS loop into a running service:

```bash
cd service && uv run python -m scripts.fake_phone --seconds 60
```

It behaves like the capture page (hello, then a ping every second), so the viewer shows "sensors OK" about a second in and recordings can be started while it runs.

For the page itself, `cd web && npm run dev` serves the pages with hot reload on port 5173 and proxies API and socket traffic to the service on 8000. Camera and location need HTTPS on a phone, so test the page on the device through `tailscale serve` with a fresh `npm run build`.

## Tests

```bash
cd service && uv run pytest
```

```bash
cd web && npm test
```

```bash
cd web && npx playwright test
```

The Playwright test builds the pages, starts the service on port 8766, and drives the capture page in Chromium with a fake camera and a mocked location.

## Settings

The service reads `DRIVE_*` environment variables; see [`config.py`](../service/drive_service/config.py) for all of them. Common ones:

| variable | default | |
|---|---|---|
| `DRIVE_PORT` | 8000 | |
| `DRIVE_RECORD_DIR` | `recordings/` | |
| `DRIVE_ALLOWED_HOSTS` | `["localhost","127.0.0.1","*.ts.net"]` | JSON list of `Host` names to answer (port ignored); others get HTTP 400. `"*"` is refused. Pin your Mac's tailnet name to be stricter |
| `DRIVE_ALLOWED_ORIGINS` | none | JSON list, e.g. `["https://my-mac.tailnet.ts.net"]`, only needed if the viewer is refused |
| `DRIVE_MAGNETIC_DECLINATION_DEG` | unset | east positive, about `-11` around College Park. Unset: computed from the first fresh GPS fix |
| `DRIVE_PHONE_IDLE_TIMEOUT_S` | 5 | a phone that sends nothing this long is disconnected |
| `DRIVE_RECORDER_MAX_BYTES` | 20 GB | a recording ends cleanly at this size |
| `DRIVE_RECORDER_MIN_FREE_BYTES` | 2 GB | a recording will not start, and ends, below this much free disk |

Staleness, clock and rate limits are listed in the [phone protocol](phone-protocol.md).
