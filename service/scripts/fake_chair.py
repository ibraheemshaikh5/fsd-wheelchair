"""Pretend to be the chair's UNO Q board: say hello, follow drive commands, report status.

Usage (from service/, with the service running and DRIVE_CHAIR_TOKEN set in both shells):

    uv run python -m scripts.fake_chair --url ws://127.0.0.1:8000/ws/chair \
        --token-env DRIVE_CHAIR_TOKEN

The token is read from the named environment variable, never from the command
line (other users can read process arguments). The board rules match the sketch
(docs/chair-link.md): output is zero unless a command newer than its ttl_ms is
held; speed and turn are clamped to the board caps (reported as "limit"); the
e-stop and joystick inputs stop the output. ``--estop`` and ``--joystick``
simulate those inputs. Exits when the service closes the connection, or after
``--seconds``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass, field, replace

from pydantic import ValidationError

from drive_service.chair.protocol import DriveCommand

log = logging.getLogger("fake_chair")

DEFAULT_URL = "ws://127.0.0.1:8000/ws/chair"
DEFAULT_TOKEN_ENV = "DRIVE_CHAIR_TOKEN"
FIRMWARE = "fake-chair-1"
# Same dev defaults as the sketch.
DEFAULT_MAX_SPEED_MPS = 0.5
DEFAULT_MAX_TURN_RADPS = 0.5
STATUS_HZ = 20.0
MS_PER_S = 1000.0
EXIT_OK = 0
EXIT_REFUSED = 3
EXIT_UNREACHABLE = 4


@dataclass(frozen=True, slots=True)
class BoardInputs:
    """The two wired inputs. Pressed (or a broken e-stop wire) reads as True."""

    estop: bool = False
    joystick_active: bool = False


@dataclass(frozen=True, slots=True)
class FakeBoard:
    max_speed_mps: float
    max_turn_radps: float
    applied_seq: int | None = None
    command_v_mps: float = 0.0
    command_w_radps: float = 0.0
    valid_until_ms: float = 0.0


@dataclass(frozen=True, slots=True)
class BoardOutput:
    v_mps: float
    w_radps: float
    stopped_reason: str | None


@dataclass(frozen=True, slots=True)
class FakeChairConfig:
    url: str
    token: str = field(repr=False)
    seconds: float | None = None
    inputs: BoardInputs = BoardInputs()
    max_speed_mps: float = DEFAULT_MAX_SPEED_MPS
    max_turn_radps: float = DEFAULT_MAX_TURN_RADPS


def apply_drive(board: FakeBoard, text: str, now_ms: float) -> FakeBoard:
    """Hold a valid, newer drive command. Anything else leaves the board as it was."""
    try:
        command = DriveCommand.model_validate_json(text)
    except ValidationError as error:
        log.warning("ignored a bad drive command: %d errors", error.error_count())
        return board
    if board.applied_seq is not None and command.seq <= board.applied_seq:
        return board
    return replace(
        board,
        applied_seq=command.seq,
        command_v_mps=command.v_mps,
        command_w_radps=command.w_radps,
        valid_until_ms=now_ms + command.ttl_ms,
    )


def _stopped(reason: str) -> BoardOutput:
    return BoardOutput(0.0, 0.0, reason)


def board_output(board: FakeBoard, inputs: BoardInputs, now_ms: float) -> BoardOutput:
    """What the board drives right now, in the sketch's order of precedence."""
    if inputs.estop:
        return _stopped("estop")
    if inputs.joystick_active:
        return _stopped("joystick")
    if board.applied_seq is None:
        return _stopped("no_link")
    if now_ms >= board.valid_until_ms:
        return _stopped("watchdog")
    v = min(max(board.command_v_mps, 0.0), board.max_speed_mps)
    w = min(max(board.command_w_radps, -board.max_turn_radps), board.max_turn_radps)
    clamped = (v, w) != (board.command_v_mps, board.command_w_radps)
    return BoardOutput(v, w, "limit" if clamped else None)


def hello_message(board: FakeBoard) -> str:
    return json.dumps(
        {
            "type": "hello",
            "role": "chair",
            "firmware": FIRMWARE,
            "max_speed_mps": board.max_speed_mps,
            "max_turn_radps": board.max_turn_radps,
        }
    )


def status_message(board: FakeBoard, inputs: BoardInputs, now_ms: float, wall_ms: float) -> str:
    output = board_output(board, inputs, now_ms)
    return json.dumps(
        {
            "type": "chair",
            "t": int(wall_ms),
            "mode": "manual" if inputs.joystick_active else "auto",
            "joystick_active": inputs.joystick_active,
            "estop": inputs.estop,
            "battery_pct": None,
            "applied_seq": board.applied_seq,
            "output": {"v_mps": output.v_mps, "w_radps": output.w_radps},
            "stopped_reason": output.stopped_reason,
        }
    )


def _mono_ms() -> float:
    return time.monotonic() * MS_PER_S


async def _read_commands(ws, holder: dict[str, FakeBoard]) -> None:
    from websockets.exceptions import ConnectionClosed

    try:
        async for raw in ws:
            if isinstance(raw, bytes):
                log.warning("ignored a binary message from the service")
                continue
            holder["board"] = apply_drive(holder["board"], raw, _mono_ms())
    except ConnectionClosed as error:
        log.info("connection lost: %s", error)
    log.info("the service closed the connection (code %s)", ws.close_code)


async def _session(ws, config: FakeChairConfig) -> None:
    from websockets.exceptions import ConnectionClosed

    # Swapped, never mutated: the reader and the status loop share this one slot.
    holder = {"board": FakeBoard(config.max_speed_mps, config.max_turn_radps)}
    await ws.send(hello_message(holder["board"]))
    reader = asyncio.create_task(_read_commands(ws, holder))
    start = time.monotonic()
    try:
        while not reader.done():
            if config.seconds is not None and time.monotonic() - start >= config.seconds:
                log.info("ran for %.1f s; disconnecting", config.seconds)
                break
            message = status_message(holder["board"], config.inputs, _mono_ms(), time.time() * 1e3)
            await ws.send(message)
            await asyncio.sleep(1.0 / STATUS_HZ)
    except ConnectionClosed as error:
        log.info("could not send status, connection closed: %s", error)
    finally:
        reader.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reader


async def run(config: FakeChairConfig) -> int:
    """Run one session. Returns the process exit code."""
    import websockets
    from websockets.exceptions import InvalidHandshake, InvalidStatus, InvalidURI

    headers = {"Authorization": f"Bearer {config.token}"}
    try:
        async with websockets.connect(config.url, additional_headers=headers) as ws:
            log.info("connected to %s", config.url)
            await _session(ws, config)
    except InvalidStatus as error:
        log.error(
            "the service refused the chair (HTTP %d): check the token and that the "
            "service has DRIVE_CHAIR_TOKEN set",
            error.response.status_code,
        )
        return EXIT_REFUSED
    except (OSError, InvalidURI, InvalidHandshake) as error:
        log.error("could not reach the service at %s: %s", config.url, error)
        return EXIT_UNREACHABLE
    return EXIT_OK


def _parse_args(argv: Sequence[str] | None) -> tuple[argparse.Namespace, str]:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--token-env", default=DEFAULT_TOKEN_ENV)
    parser.add_argument("--seconds", type=float, default=None, help="stop after this long")
    parser.add_argument("--estop", action="store_true", help="simulate the e-stop pressed")
    parser.add_argument("--joystick", action="store_true", help="simulate the rider on the stick")
    parser.add_argument("--max-speed", type=float, default=DEFAULT_MAX_SPEED_MPS)
    parser.add_argument("--max-turn", type=float, default=DEFAULT_MAX_TURN_RADPS)
    args = parser.parse_args(argv)
    token = os.environ.get(args.token_env, "").strip()
    if not token:
        parser.error(f"set the chair token in the {args.token_env} environment variable")
    if args.seconds is not None and args.seconds <= 0:
        parser.error("--seconds must be positive")
    if args.max_speed < 0 or args.max_turn < 0:
        parser.error("--max-speed and --max-turn must not be negative")
    return args, token


def main(argv: Sequence[str] | None = None) -> None:
    args, token = _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    config = FakeChairConfig(
        url=args.url,
        token=token,
        seconds=args.seconds,
        inputs=BoardInputs(estop=args.estop, joystick_active=args.joystick),
        max_speed_mps=args.max_speed,
        max_turn_radps=args.max_turn,
    )
    raise SystemExit(asyncio.run(run(config)))


if __name__ == "__main__":
    main()
