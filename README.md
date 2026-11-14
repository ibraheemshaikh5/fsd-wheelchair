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

## Status

Design only. No code yet.
