"""Reject browser requests that come from another website.

There is no login: the Tailscale network is the access boundary. That leaves
one gap, a malicious web page open in a browser that *is* on the tailnet, which
could otherwise open a socket here and read the camera. Browsers always send
an Origin header on such requests, so checking it closes the gap. Clients that
send no Origin (scripts, the fake phone) are not browsers and are let through.

This check alone does not stop DNS rebinding, where the attacker's page and its
requests share one origin. The Host allowlist (``Settings.allowed_hosts``,
enforced by TrustedHostMiddleware in ``app.py``) covers that case.
"""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlsplit


def origin_allowed(headers: Mapping[str, str], allowed_origins: tuple[str, ...]) -> bool:
    origin = headers.get("origin")
    if origin is None:
        return True
    if origin in allowed_origins:
        return True
    origin_host = urlsplit(origin).netloc.lower()
    if not origin_host:
        return False
    # A reverse proxy such as `tailscale serve` may put the public name in
    # X-Forwarded-Host. A web page cannot set that header on a WebSocket or on
    # a simple cross-site request (it forces a CORS preflight that fails), and the
    # Host header has already passed the allowlist, so trusting it does not reopen
    # the gap.
    own_hosts = {
        headers.get("host", "").lower(),
        headers.get("x-forwarded-host", "").lower(),
    } - {""}
    return origin_host in own_hosts
