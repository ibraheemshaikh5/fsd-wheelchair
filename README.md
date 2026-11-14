# FSD Wheelchair

Full self-driving for an electric wheelchair. Pick a destination on an iPad, the chair drives there. Sidewalks and indoors.

## Components

| # | Part | What it is |
|---|------|------------|
| 1 | Chair | Culver Mobility WOLF folding electric wheelchair |
| 2 | Sensors | Phone mounted on the chair: camera + GPS |
| 3 | Model | Open-weight navigation model, post-trained on our own driving data |
| 4 | Compute | One computer running the driving service (off-chair for now) |
| 5 | Display | iPad web app, styled like the Tesla FSD screen |

## How it fits together

```
Phone (camera + GPS) ──video, location──▶ Driving service (computer)
                                              │  model → steering + speed
Chair controller ◀──────drive commands────────┤
                                              └──state, video, path──▶ iPad web app
iPad web app ──destination, start/stop──▶ Driving service
```

## Code

| Folder | What |
|--------|------|
| [`service/`](service) | Driving service (Python, FastAPI). Today: receives the phone stream, keeps the latest sensor readings, records drives. |
| [`web/`](web) | Browser pages (TypeScript, Vite): the phone's capture page, a sensor viewer, and a device check. |
| [`app/`](app) | iPad app (React, TypeScript, Vite). See below. |
| [`ml/`](ml) | Model side (Python): the driving pipeline, a simulated chair to run it against, the models behind one interface, and tools to compare them. |

## Docs

Index: [docs/README.md](docs/README.md).

- [Architecture](docs/architecture.md): data flow, interfaces, safety
- [Model](docs/model.md): base model candidates and post-training plan
- [Driving pipeline](docs/driving-pipeline.md): how sensor data becomes a decision
- [Outputs](docs/outputs.md): what the service sends to the chair, the iPad and disk
- [Phone protocol](docs/phone-protocol.md): how the phone streams to the service
- [Running](docs/running.md): setup, each session, tests
- [Open questions](docs/open-questions.md): what has to be decided before building

## iPad app

Lives in [`app/`](app). Vite + React + TypeScript, runs in Safari on the iPad (landscape).

```bash
cd app && npm install && npm run dev
```

It opens on http://localhost:5174 and connects to the driving service on port 8000 (start it first, see [running](docs/running.md)). Add `?sim` to the address (or set `VITE_DRIVING_SERVICE=sim`) to use the in-browser simulator instead, with no service or phone.

Tests: `cd app && npm test` (unit tests with coverage) and `npm run typecheck`.

On the iPad, publish the app on the tailnet over HTTPS (needed for location and voice), in a second terminal next to the service's `tailscale serve`:

```bash
tailscale serve --bg --https=8443 http://127.0.0.1:5174
```

Then open `https://your-mac.your-tailnet.ts.net:8443` in Safari. The app's server listens on loopback only and passes `/api` and `/ws` through to the service, so the service's host and origin checks apply unchanged.

- **Live mode** (default): the app shows the phone on the chair through the service's `/ws/viewer`. The chair's position, heading and speed come from the phone's GPS and compass, and the camera button shows the phone's camera. A chip next to the mode says whether the iPad hears the service and the phone (green "Phone" with GPS accuracy, amber while the phone's clock or sensors are not ready, red "No phone" or "Service offline"). Picking a destination sends it to the service (`POST /api/destination`) and resends it if the service restarts; ending the trip clears it. The service does not drive yet, so Start Self-Driving is shown disabled with the reason, and the screen follows along while the rider drives with the joystick (mode "Manual" while moving, "Arrived" within 8 m of the destination). Indoor goals stay on the iPad for now.

- **First launch**: asks for location, then microphone, with one tap. If either is refused the app still works (default start point, typed search only).
- **Layout**: always a split view, chair on the left and map on the right. Before a destination is chosen the map carries the search bar and microphone button; afterwards it carries the next turn and trip card.
- **Map**: zoomed in on the chair, in Tesla's grey-and-white style (grey land, white roads, building footprints), with a Satellite toggle. A search bar returns places within 0.5 mi; the map widens to show them.
- **Voice**: mainly for indoor goals. Each spoken request is classified in [`app/src/services/goals.ts`](app/src/services/goals.ts): indoor ("go to the kitchen") becomes a language goal with no map route; outdoor ("the nearest coffee shop", an address) becomes a place search and the nearest match is chosen. If the wording doesn't settle it and the map finds nothing, the app asks. The classifier is a wording heuristic, meant to be replaced by the driving service or a language model.
- **Chair view**: the chair on open ground with range rings, people around it, and a swept footprint showing the space it is about to move into (blue when clear, amber when slowing, red when blocked). Above it: a mode chip (Parked, Self-driving, Manual), a speed arc, and one line saying what the chair is doing and why. A camera button opens a live inset of the forward camera.
- **Take-over alert**: when the service sets `takeOver`, or the connection drops, the pane gets a red border and a "Take over now" banner. In development, press `t` while driving to simulate one.
- **Indoor trips**: no map route exists, so the chair view fills the screen.
- **Google Maps**: copy `app/.env.example` to `app/.env` and set `VITE_GOOGLE_MAPS_API_KEY` (Maps JavaScript API, Places API (New), Routes API). Without a key the app uses OpenFreeMap tiles via MapLibre (Esri imagery for satellite), sample places and a one-corner stand-in route.
- **Backend**: the app talks to the driving service only through the `DrivingService` interface in [`app/src/services/drivingService.ts`](app/src/services/drivingService.ts), with shared shapes in [`app/src/types.ts`](app/src/types.ts). `LiveDrivingService` ([`app/src/services/live/`](app/src/services/live)) talks to the real service; `MockDrivingService` simulates a whole drive in the browser (`?sim`). `createDrivingService()` picks one.
- Voice input uses the browser's speech recognition, which needs HTTPS (or localhost) and microphone permission.

Code layout under `app/src`:

| Folder | What lives there |
|--------|------------------|
| `types.ts` | Shapes shared with the driving service |
| `services/` | Driving service interface and simulator, place search, routing, spoken-goal classification |
| `services/live/` | Connection to the real driving service: status parsing, the reconnecting viewer socket, status → DriveState |
| `lib/`, `hooks/` | Geometry, formatting, Google Maps loading, permissions, small React hooks |
| `components/drive/` | Chair view, speed arc, camera inset, turn and trip cards |
| `components/map/` | Google map and the keyless MapLibre map |
| `components/search/` | Search bar with suggestions, voice overlay |
| `components/` | Shared button, dialog and icons, permissions screen |
| `styles/` | One stylesheet per area |

## Status

- **Sensors (2):** phone camera, GPS, compass and IMU stream to the service, with a live viewer and recording. Tested with a fake phone, in Chromium, and once by hand on the iPhone over Tailscale; not yet mounted on the chair.
- **Display (5):** iPad app connected to the service: shows the phone's position, heading, speed and camera live, and sends the destination. Self-driving is not wired yet. Tested with the fake phone; not yet on the iPad.
- **Chair interface:** an Arduino UNO Q fakes the joystick's magnet sensor, driven from code with `Wheelchair().set(x, y)` over Tailscale. See [hardware/README.md](hardware/README.md). The model's live view can drive it (**Send to chair** in the viewer).
