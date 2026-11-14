"""Read a small request body without ever holding more than the limit in memory."""

from __future__ import annotations

from fastapi import Request

CONTENT_LENGTH = "content-length"


class BodyTooLarge(ValueError):
    """The body is (or says it is) over the limit."""


class BadContentLength(ValueError):
    """The content-length header is not a whole number."""


def _declared_length(request: Request) -> int | None:
    raw = request.headers.get(CONTENT_LENGTH)
    if raw is None:
        return None
    text = raw.strip()
    if not text.isdigit():
        raise BadContentLength(f"content-length {raw!r} is not a number")
    return int(text)


async def read_limited(request: Request, max_bytes: int) -> bytes:
    """The body, refused up front when content-length is over ``max_bytes``.

    A body with no content-length (chunked) is read piece by piece and refused as
    soon as it passes the limit, so a huge upload is never read in full.
    """
    declared = _declared_length(request)
    if declared is not None and declared > max_bytes:
        raise BodyTooLarge(f"body is {declared} bytes; the limit is {max_bytes}")
    received = bytearray()
    async for chunk in request.stream():
        received.extend(chunk)
        if len(received) > max_bytes:
            raise BodyTooLarge(f"body is over {max_bytes} bytes")
    return bytes(received)
