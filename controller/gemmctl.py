#!/usr/bin/python3
"""Narrow command-line client for the GEMM autoresearch controller."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import socket
import struct
import sys
from typing import Any


PROTOCOL_VERSION = 1
SOCKET_PATH = "/run/gemm-autoresearch/controller.sock"
MAX_CANDIDATE_BYTES = 1_048_576
MAX_RESPONSE_BYTES = 1_000_000
DEFAULT_WAIT_SECONDS = 2_700


class ClientError(RuntimeError):
    pass


def _recv_exact(connection: socket.socket, length: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < length:
        chunk = connection.recv(length - len(chunks))
        if not chunk:
            raise ClientError("controller disconnected before completing its response")
        chunks.extend(chunk)
    return bytes(chunks)


def _exchange(request: dict[str, Any], timeout: int) -> dict[str, Any]:
    payload = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(timeout + 180)
    try:
        connection.connect(SOCKET_PATH)
        connection.sendall(struct.pack("!I", len(payload)) + payload)
        header = _recv_exact(connection, 4)
        (length,) = struct.unpack("!I", header)
        if length < 2 or length > MAX_RESPONSE_BYTES:
            raise ClientError("controller returned an invalid or oversized response")
        raw = _recv_exact(connection, length)
    except (FileNotFoundError, ConnectionRefusedError, PermissionError) as exc:
        raise ClientError(f"cannot connect to trusted controller at {SOCKET_PATH}: {exc}") from exc
    except socket.timeout as exc:
        raise ClientError("timed out communicating with the trusted controller") from exc
    finally:
        connection.close()
    try:
        response = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ClientError("controller returned invalid JSON") from exc
    if not isinstance(response, dict) or not isinstance(response.get("ok"), bool):
        raise ClientError("controller returned an invalid response envelope")
    if response["ok"] is not True:
        error = response.get("error")
        raise ClientError(error if isinstance(error, str) else "controller rejected the request")
    result = response.get("result")
    if not isinstance(result, dict):
        raise ClientError("controller returned an invalid result")
    return result


def _candidate(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ClientError("candidate must be a regular, non-symlink file")
    raw = path.read_bytes()
    if not raw or len(raw) > MAX_CANDIDATE_BYTES:
        raise ClientError(
            f"candidate must be between 1 and {MAX_CANDIDATE_BYTES} bytes"
        )
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ClientError("candidate must contain valid UTF-8") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gemmctl",
        description="Submit only GEMM candidate source to the trusted GitHub/DGX controller.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("status", help="show the current research run and best result")

    start = subparsers.add_parser("start", help="start a new run after the previous PR is closed")
    start.add_argument("--title", default="GEMM autoresearch run")

    submit = subparsers.add_parser(
        "submit", help="submit one candidate and wait for the trusted DGX result"
    )
    submit.add_argument("candidate", type=Path)
    submit.add_argument("--hypothesis", required=True)
    submit.add_argument(
        "--timeout-seconds",
        type=int,
        default=DEFAULT_WAIT_SECONDS,
        help="DGX wait limit (60–2700 seconds; default: 2700)",
    )
    return parser


def main() -> int:
    arguments = build_parser().parse_args()
    request: dict[str, Any] = {"version": PROTOCOL_VERSION, "operation": arguments.command}
    timeout = 30
    try:
        if arguments.command == "start":
            request["title"] = arguments.title
        elif arguments.command == "submit":
            request.update(
                {
                    "candidate": _candidate(arguments.candidate),
                    "hypothesis": arguments.hypothesis,
                    "timeout_seconds": arguments.timeout_seconds,
                }
            )
            timeout = arguments.timeout_seconds
        result = _exchange(request, timeout)
    except ClientError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
