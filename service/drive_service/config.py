"""Service settings and tuning constants.

Only the ``Settings`` fields can be overridden, each with a ``DRIVE_``-prefixed
environment variable (e.g. ``DRIVE_PORT``). Everything else is a constant here.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]

# Host names the service answers to. Anything else is refused with HTTP 400, which
# stops a DNS-rebinding page from reaching the service through the loopback port.
# Nobody but Tailscale can point a *.ts.net name at this machine, so the wildcard
# keeps `tailscale serve` working with no setup; pin the exact name to be stricter.
DEFAULT_ALLOWED_HOSTS: tuple[str, ...] = ("localhost", "127.0.0.1", "*.ts.net")
WILDCARD_PREFIX = "*."


def _check_host_pattern(pattern: str) -> str:
    """Return the pattern lowercased, or raise ValueError if it is malformed."""
    if not pattern or pattern != pattern.strip():
        raise ValueError(f"allowed_hosts entry {pattern!r} must be a non-empty host name")
    if pattern == "*":
        raise ValueError("allowed_hosts must not contain '*'; it would accept any host")
    if "*" in pattern and (not pattern.startswith(WILDCARD_PREFIX) or "*" in pattern[1:]):
        raise ValueError(
            f"allowed_hosts entry {pattern!r}: a wildcard must look like '*.example.com'"
        )
    return pattern.lower()


# Bound to loopback on purpose: `tailscale serve` is the only way in.
HOST = "127.0.0.1"

# Size limits
MAX_FRAME_BYTES = 1_000_000
MAX_FRAME_HEADER_BYTES = 1024
MAX_TEXT_BYTES = 64_000

# A socket that sends this many bad messages in a row is closed.
MAX_CONSECUTIVE_ERRORS = 20
# A phone socket that sends nothing for this long is closed (4408) so the slot
# frees up. The phone pings every second, so this is about five missed pings.
PHONE_IDLE_TIMEOUT_S = 5.0

# A reading older than this must not be used to drive.
FRAME_STALE_MS = 500.0
# An iPhone at rest reports a fix only about every 5 s (measured), so a tighter
# limit would mark a stopped chair's GPS stale. GPS only tracks route progress;
# the camera's limit above is what keeps driving safe.
GPS_STALE_MS = 10_000.0
HEADING_STALE_MS = 1000.0
# Speed and turn rate come from IMU samples at about 60 Hz; older than this
# means the IMU stream has stalled.
MOTION_STALE_MS = 250.0
# The phone reports link stats once a second; older than this means they stopped.
LINK_STALE_MS = 3000.0

# Above this speed GPS course is trusted over the compass.
MOVING_SPEED_MPS = 0.5

# Status plus the newest frame, this many times a second, to every viewer.
VIEWER_FPS = 30.0
STATS_WINDOW = 100
CLOCK_WINDOW = 20
# Readings are aged from capture only with a clock estimate whose round trip is at
# most this long; otherwise nothing is fresh.
MAX_CLOCK_RTT_MS = 500.0
# A reading stamped further in the future than this proves the offset is wrong.
MAX_CLOCK_SKEW_MS = 250.0
# Frame rate is measured over frames received this recently.
FPS_WINDOW_S = 2.0

# Items waiting to be written to a recording; more are dropped and counted. With
# 60 frames, 20 IMU and 20 motion lines and about 10 headings a second, this rides
# out about 9 s of disk stall.
RECORDER_QUEUE_SIZE = 1024


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DRIVE_", frozen=True)

    port: int = 8000
    record_dir: Path = REPO_ROOT / "recordings"
    web_dist: Path = REPO_ROOT / "web" / "dist"
    # Extra browser origins to accept, e.g. "https://my-mac.tailnet.ts.net".
    allowed_origins: tuple[str, ...] = ()
    # Host header values to accept (port ignored), e.g. ["my-mac.tailnet.ts.net"].
    allowed_hosts: tuple[str, ...] = DEFAULT_ALLOWED_HOSTS

    @field_validator("allowed_hosts")
    @classmethod
    def _validate_allowed_hosts(cls, hosts: tuple[str, ...]) -> tuple[str, ...]:
        if not hosts:
            raise ValueError("allowed_hosts must list at least one host")
        return tuple(_check_host_pattern(host) for host in hosts)
