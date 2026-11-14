# Docs

Start with [architecture](architecture.md).

## Design

| Doc | Covers |
|-----|--------|
| [architecture.md](architecture.md) | The whole system: pieces, how the models fit together, safety |
| [model.md](model.md) | Which models, why, and the post-training plan |
| [driving-pipeline.md](driving-pipeline.md) | How the service turns sensor data into a decision, stage by stage |

## Interfaces

| Doc | Covers |
|-----|--------|
| [phone-protocol.md](phone-protocol.md) | Phone → service: camera frames and sensors |
| [outputs.md](outputs.md) | Service → chair, iPad and disk |

## Working on it

| Doc | Covers |
|-----|--------|
| [running.md](running.md) | Running the phone stream and the service |
| [ml/README.md](../ml/README.md) | Running the bake-off and downloading model checkpoints |
| [model-data-guide.md](model-data-guide.md) | Taking 20 fps frames plus matching motion, GPS and heading out of a recording |
| [open-questions.md](open-questions.md) | What is still undecided |
