"""The driving pipeline: sensor data in, a drive command out (docs/driving-pipeline.md).

It lives here, apart from the driving service, so it can be developed against
the simulator and recordings. It imports nothing from the service.
"""

from fsd_ml.pipeline.loop import ModelRunner, Pipeline, PipelineConfig
from fsd_ml.pipeline.route import Route
from fsd_ml.pipeline.types import ChairStatus, Decision, DriveCommand, Obstacle, Sensors

__all__ = [
    "ChairStatus",
    "Decision",
    "DriveCommand",
    "ModelRunner",
    "Obstacle",
    "Pipeline",
    "PipelineConfig",
    "Route",
    "Sensors",
]
