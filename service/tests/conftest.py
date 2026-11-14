"""Keep the developer's service/.env out of the tests.

Settings classes read service/.env, where a developer may choose the external
camera (DRIVE_CAMERA_SOURCE=external). Environment variables win over that
file, so the tests pin the defaults they assume here. Tests that want the
external camera pass CameraSettings explicitly.
"""

import os

os.environ["DRIVE_CAMERA_SOURCE"] = "phone"
os.environ["DRIVE_CAMERA_TOKEN"] = ""
