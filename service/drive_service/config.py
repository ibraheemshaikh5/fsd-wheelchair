"""Service settings. Override any field with a ``DRIVE_``-prefixed environment variable."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
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


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DRIVE_", frozen=True)

    # Bound to loopback on purpose: `tailscale serve` is the only way in.
    host: str = "127.0.0.1"
    port: int = 8000
    record_dir: Path = REPO_ROOT / "recordings"
    web_dist: Path = REPO_ROOT / "web" / "dist"
    # Extra browser origins to accept, e.g. "https://my-mac.tailnet.ts.net".
    allowed_origins: tuple[str, ...] = ()
    # Host header values to accept (port ignored), e.g. ["my-mac.tailnet.ts.net"].
    allowed_hosts: tuple[str, ...] = DEFAULT_ALLOWED_HOSTS
    # Tailscale logins (e.g. ["you@gmail.com"]) whose phone always gets the phone
    # slot. While one streams, any other phone is turned away (4409). The login comes
    # from the Tailscale-User-Login header that `tailscale serve` sets. Empty: the
    # newest connection always wins.
    priority_users: tuple[str, ...] = ()

    # Size limits
    max_frame_bytes: int = 1_000_000
    max_frame_header_bytes: int = 1024
    max_text_bytes: int = 64_000

    # Flood limits
    # Recording writes only; the snapshot always keeps the newest frame. About 2x the
    # phone's top frame rate (60 fps) so jitter never drops frames.
    max_frame_rate_hz: int = 120
    max_text_rate_hz: int = 200
    max_api_rate_hz: int = 20  # status reads
    max_control_rate_hz: int = 5  # recording start/stop, separate so reads cannot starve it
    max_drive_rate_hz: int = 30  # the model's chair commands, separate so they cannot starve a stop
    max_consecutive_errors: int = 20
    max_viewers: int = 4
    # A phone socket that sends nothing for this long is closed (4408) so the slot
    # frees up. The phone pings every second, so this is about five missed pings.
    phone_idle_timeout_s: float = 5.0

    # A reading older than this must not be used to drive.
    frame_stale_ms: float = 500.0
    # An iPhone at rest reports a fix only about every 5 s (measured), so a tighter
    # limit would mark a stopped chair's GPS stale. GPS only tracks route progress;
    # the camera's limit above is what keeps driving safe.
    gps_stale_ms: float = 10_000.0
    heading_stale_ms: float = 1000.0
    # Speed and turn rate come from IMU samples at about 60 Hz; older than this
    # means the IMU stream has stalled.
    motion_stale_ms: float = 250.0
    # The phone reports link stats once a second; older than this means they stopped.
    link_stale_ms: float = 3000.0

    # Above this speed GPS course is trusted over the compass.
    moving_speed_mps: float = 0.5
    # Local magnetic declination in degrees, east positive (College Park is about -11).
    # Unset: computed from the latest fresh GPS fix with the World Magnetic Model.
    # Until it is known the compass heading is reported as magnetic, never as true.
    magnetic_declination_deg: float | None = Field(default=None, ge=-180.0, le=180.0)

    # Status plus the newest frame, this many times a second, to every viewer.
    viewer_fps: float = 30.0
    stats_window: int = 100
    clock_window: int = 20
    # Readings are aged from capture only with a clock estimate whose round trip is at
    # most this long; otherwise nothing is fresh.
    max_clock_rtt_ms: float = 500.0
    # A reading stamped further in the future than this proves the offset is wrong.
    max_clock_skew_ms: float = 250.0
    # Frame rate is measured over frames received this recently.
    fps_window_s: float = 2.0

    # Recording limits. Reaching max frames, max bytes or the free-disk floor ends the
    # recording cleanly (status recording.end_reason); starting needs min free bytes.
    # Items waiting to be written: 60 frames, 20 IMU and 20 motion lines and about
    # 10 headings a second, so this rides out about 9 s of disk stall.
    recorder_queue_size: int = 1024
    recorder_max_frames: int = 432_000  # 2 h at 60 fps
    recorder_max_bytes: int = 20_000_000_000  # per recording, frames plus logs
    recorder_max_queue_bytes: int = 64_000_000  # waiting to be written; more is dropped
    recorder_min_free_bytes: int = 2_000_000_000

    @field_validator("allowed_hosts")
    @classmethod
    def _validate_allowed_hosts(cls, hosts: tuple[str, ...]) -> tuple[str, ...]:
        if not hosts:
            raise ValueError("allowed_hosts must list at least one host")
        return tuple(_check_host_pattern(host) for host in hosts)

    @field_validator("priority_users")
    @classmethod
    def _normalize_priority_users(cls, users: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(user.strip().lower() for user in users)
        if any(not user for user in normalized):
            raise ValueError("priority_users entries must be non-empty Tailscale logins")
        return normalized
