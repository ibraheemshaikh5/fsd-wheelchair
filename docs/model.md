# Model

## Goal

Start from an open-weight model that already navigates well from a single camera, then post-train it on wheelchair data. The story: a strong general base, made specifically good at this chair.

## Base model candidates

Unverified shortlist. Each needs to be checked for license, current weights, and whether it runs fast enough on our computer.

| Model | Why consider it |
|-------|-----------------|
| NoMaD / ViNT (Berkeley) | Navigation foundation models for small ground robots. Single camera in, waypoints out. Closest match to sidewalk-speed driving. |
| NaVILA | Vision-language navigation. Takes instructions like "go to the door". |
| OpenVLA / SmolVLA | General robot vision-language-action models. Bigger community, less navigation-specific. |
| NVIDIA Alpamayo | Open driving model with reasoning. Built for cars on roads, so a weaker fit for sidewalks and indoors. |

**Starting recommendation:** NoMaD/ViNT. They are built for exactly this input and output, are small enough to run in real time, and were trained across many robot bodies, so adapting to a new one is the intended use. They also navigate to a goal image rather than a GPS point, which covers indoors.

## Post-training

1. **Collect**: a person drives the chair by joystick on sidewalks and indoors while we log camera, GPS, and joystick commands.
2. **Fine-tune**: train the base model to predict the human's trajectory from the camera frames.
3. **Evaluate**: replay held-out drives offline first, then closed-loop on a fixed test route.
4. **Repeat**: log every disengagement, add those clips to the training set.

## What to measure

- Distance per disengagement on the test route.
- Base model vs. post-trained model on the same route. This is the demo number.
