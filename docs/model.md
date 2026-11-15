# Model

## Goal

Start from open-weight models that already navigate well from a single camera, then post-train on wheelchair data. The story: a strong general base, made specifically good at this chair.

## Decision: OmniVLA for everything

Decided 2026-11-14: use OmniVLA both outdoors and indoors, hosted on a Modal GPU, with latency as the constraint to watch. One model takes a GPS-style point outdoors and an instruction or photo indoors, so there is no hand-off between models, one thing to host and one model to post-train.

No other model is kept in the repo: the FlowPilot and S2E wrappers that ran on a laptop were removed. The two-layer plan below is what this replaced; its reasoning and the benchmark still explain the trade-off: OmniVLA's sidewalk driving is unmeasured and its nearest relative did markedly worse than FlowPilot there.

The driver and Modal app for OmniVLA are written but not yet run ([`ml/README.md`](../ml/README.md#omnivla-on-modal)).

## Earlier plan: two layers

| Layer | Job | Model | Runs at |
|-------|-----|-------|---------|
| Driver | Stay on the path, avoid obstacles and people, follow the route | **FlowPilot** | ~20 Hz |
| Goal layer | Indoors and language goals ("go to the kitchen"), image goals | **OmniVLA** | slower |

Outdoors, GPS feeds the route straight to the driver. Indoors, OmniVLA drives from an image or language goal. Later, OmniVLA can hand short-range targets to the driver so one model does all the steering.

## Why these

**FlowPilot** (UCLA, CoRL 2026). 230M parameters, trained on 300 hours of sidewalk driving, single camera plus coarse GPS, outputs speed and turn rate. Among the best public models on SidewalkBench. It also ships a method for fine-tuning from human interventions, which is exactly our post-training loop.

**OmniVLA** (Berkeley, ICRA 2026). From the NoMaD/ViNT authors. One model that takes a GPS point, a goal image, a language instruction, or any mix. This is the "NoMaD plus language" combination, already built. 8B parameters on an OpenVLA base, MIT-licensed code, fine-tuning code released. A small `omnivla-edge` variant exists.

## What the benchmark says

SidewalkBench (2026), failures per 100 m on real-world scanned sidewalks. Lower is better.

| Model | Failures / 100 m | Trained on sidewalks |
|-------|------------------|----------------------|
| OpenPilot, retrained | 0.74 | yes, 1000 h |
| FlowPilot | 0.92 | yes, 300 h |
| S2E | 0.92 | yes, 100 h |
| MIMIC | 1.66 | yes, 50 h |
| MBRA | 5.15 | no |
| NoMaD | 5.89 | no |
| ViNT | 24.11 | no |

The paper's conclusion: sidewalk training data matters far more than architecture. OmniVLA was not tested; MBRA is its closest relative in the table.

## Ruled out

- **NoMaD / ViNT as the driver.** 6x to 25x more failures than sidewalk-trained models. NoMaD stays as a baseline and an indoor fallback.
- **NaVILA.** 8B model built for legged robots, about 0.6 s per decision. OmniVLA covers language goals and also outputs velocities directly. We keep NaVILA's idea of a slow language model steering a fast driver.
- **NVIDIA Alpamayo.** 10B parameters, needs a 24 GB GPU, trained on cars with multi-camera rigs on roads. Wrong domain, and the benchmark shows domain is what counts.

## Risks

- **Indoors is unproven.** FlowPilot was trained on sidewalks. OmniVLA's indoor quality on our chair is unknown.
- **Chair size.** These models were trained on small delivery-style robots. A wheelchair is wider and carries a person, so learned clearances will be too tight until post-trained.
- **Camera match.** FlowPilot expects a specific camera view. The phone mount has to approximate it, or we fine-tune it away.
- **Not yet verified:** FlowPilot weight license, and whether its fine-tuning code is public.

## What the phone stream changes

The phone capture is built ([phone protocol](phone-protocol.md)). Checked against what FlowPilot expects:

| Topic | Stream today | FlowPilot expects | Action |
|-------|--------------|-------------------|--------|
| Orientation | Portrait, 360×640 | Landscape 16:9, 384×216 | Mount and capture in landscape |
| Field of view | Ultra-wide lens | About 100° horizontal (from its published camera settings) | Landscape ultra-wide should be close; portrait is much narrower. Measure it |
| Frame rate | 10 fps | One frame per step at 20 Hz | Raise capture to 20 fps |
| Speed and turn rate | Gyro in the stream; GPS speed at ~1 Hz, none at rest or indoors | Measured speed and yaw rate every step | Needs a real speed source, or use the FlowPilot variant that takes neither |
| Position and heading | GPS; compass not yet checked when mounted | Coarse GPS plus heading to place the chair on the route | Measure GPS and compass on a moving sidewalk run |
| Latency | 38–47 ms median to the service | n/a | Fine. Model time will dominate |

Two consequences:

- **Motion tracking moves up.** A native ARKit app (or offline visual odometry) gives the chair's actual path. That one source provides trajectory labels for post-training, a ground truth to score the bake-off, and the live speed FlowPilot wants. It is [open question 7](open-questions.md).
- **Joystick logging is not required to start.** Labels come from the path the chair took, not the stick. The bake-off can begin with landscape recordings alone, judged by eye with predicted paths drawn on the video.

The mount height and pitch the phone already reports feed the footprint check. The chair's camera will likely sit higher than the robots these models trained on; post-training covers that.

## First step: bake-off

Record chair footage on sidewalks and indoors, then run FlowPilot, OmniVLA, OmniVLA-edge, S2E, and NoMaD on it offline. UCLA's Navigation Model Zoo puts most of these behind one interface, so this is cheap. Pick the driver from the result, not from this doc.

The harness is in [`ml/`](../ml): it reads a recording, runs a driver over it and saves what the driver proposed. FlowPilot (small variant) and S2E were wrapped and run for this, at about 400 ms and 240 ms per call on a Mac CPU, and have since been removed; only OmniVLA is wrapped now. A [viewer](../ml/README.md#watch-a-model-on-a-video) shows a model's proposals on any video.

## What wrapping FlowPilot showed

From FlowPilot's own documentation, read while wrapping it:

- **It was trained on one camera**: a fisheye about 137° wide, 0.456 m off the ground. The table above assumed about 100°; that figure belongs to an older variant. A phone ultra-wide is narrower and a chair mount is higher, so expect shifted plans until it is post-trained on chair footage.
- **Three public variants.** We use the small one (21M parameters). A larger one (208M) and a streaming one that needs no speed input also exist.
- **Weights and training code are public**, so post-training is possible. The intervention-based fine-tuning stage is announced but not released yet.
- **No licence file in its repository.** The model zoo it also ships in is Apache-2.0, but the terms for these weights are not stated. Fine for a hackathon; check before anything else.
- **Goals closer than 20 m tell it to slow down and stop.**

## Where models run

On a Modal GPU, called over HTTP by the pipeline. A model on another machine sits behind the same interface as a local one, so nothing else changes. Latency is the cost: the round trip has to stay small next to the 50 ms loop, and the pipeline stops the chair if a result is more than a second old. `ml/fsd_ml/latency.py` measures the round trip. The Modal app is written but not yet run.

## Post-training

1. **Collect**: a person drives the chair by joystick on sidewalks and indoors while we record the phone stream and the chair's path from motion tracking.
2. **Fine-tune**: train the driver to predict the human's trajectory from the camera frames.
3. **Evaluate**: replay held-out drives offline first, then closed-loop on a fixed test route.
4. **Repeat**: log every disengagement and fine-tune on those corrections.

## What to measure

- Distance per disengagement on the test route.
- Base model vs. post-trained model on the same route. This is the demo number.

## Sources

- FlowPilot: https://github.com/VAIL-UCLA/FlowPilot
- OmniVLA: https://github.com/NHirose/OmniVLA
- SidewalkBench: https://arxiv.org/abs/2606.16953
- Navigation Model Zoo: https://huggingface.co/UCLA-VAIL/Navigation-Model-Zoo-Public
- NoMaD / ViNT: https://github.com/robodhruv/visualnav-transformer
- NaVILA: https://github.com/AnjieCheng/NaVILA
- Alpamayo: https://github.com/NVlabs/alpamayo
