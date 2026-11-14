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

## Docs

- [Architecture](docs/architecture.md) — data flow, interfaces, safety
- [Model](docs/model.md) — base model candidates and post-training plan
- [Open questions](docs/open-questions.md) — what has to be decided before building

## iPad app

Lives in [`app/`](app). Vite + React + TypeScript, runs in Safari on the iPad (landscape).

```bash
cd app && npm install && npm run dev
```

- **First launch**: asks for location, then microphone, with one tap. If either is refused the app still works (default start point, typed search only).
- **Layout**: always a split view, chair on the left and map on the right. Before a destination is chosen the map carries the search bar and microphone button; afterwards it carries the next turn and trip card.
- **Map**: zoomed in on the chair, in Tesla's grey-and-white style (grey land, white roads, building footprints), with a Satellite toggle. A search bar returns places within 0.5 mi; the map widens to show them.
- **Voice**: mainly for indoor goals. Each spoken request is classified in [`app/src/services/goals.ts`](app/src/services/goals.ts): indoor ("go to the kitchen") becomes a language goal with no map route; outdoor ("the nearest coffee shop", an address) becomes a place search and the nearest match is chosen. If the wording doesn't settle it and the map finds nothing, the app asks. The classifier is a wording heuristic, meant to be replaced by the driving service or a language model.
- **Chair view**: the chair on open ground with range rings, people around it, and a swept footprint showing the space it is about to move into (blue when clear, amber when slowing, red when blocked). Above it: a mode chip (Parked, Self-driving, Manual), a speed arc, and one line saying what the chair is doing and why. A camera button opens a live inset of the forward camera.
- **Take-over alert**: when the service sets `takeOver`, or the connection drops, the pane gets a red border and a "Take over now" banner. In development, press `t` while driving to simulate one.
- **Indoor trips**: no map route exists, so the chair view fills the screen.
- **Google Maps**: copy `app/.env.example` to `app/.env` and set `VITE_GOOGLE_MAPS_API_KEY` (Maps JavaScript API, Places API (New), Routes API). Without a key the app uses OpenFreeMap tiles via MapLibre (Esri imagery for satellite), sample places and a straight-line stand-in route.
- **Backend**: the app talks to the driving service only through the `DrivingService` interface in [`app/src/services/drivingService.ts`](app/src/services/drivingService.ts), with shared shapes in [`app/src/types.ts`](app/src/types.ts). Today that is `MockDrivingService`, which simulates a drive in the browser. Swap it in `createDrivingService()`.
- Voice input uses the browser's speech recognition, which needs HTTPS (or localhost) and microphone permission.

Code layout under `app/src`:

| Folder | What lives there |
|--------|------------------|
| `types.ts` | Shapes shared with the driving service |
| `services/` | Driving service interface and simulator, place search, routing, spoken-goal classification |
| `lib/`, `hooks/` | Geometry, formatting, Google Maps loading, permissions, small React hooks |
| `components/drive/` | Chair view, speed arc, camera inset, turn and trip cards |
| `components/map/` | Google map and the keyless MapLibre map |
| `components/search/` | Search bar with suggestions, voice overlay |
| `components/` | Shared button, dialog and icons, permissions screen |
| `styles/` | One stylesheet per area |

## Status

iPad app scaffolded against a simulated driving service. No driving service, model, or chair interface yet.
