#!/usr/bin/python3
"""Narrow command-line client for the GEMM autoresearch controller."""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
from pathlib import Path
import re
import socket
import stat
import struct
import sys
import tempfile
from typing import Any


PROTOCOL_VERSION = 1
SOCKET_PATH = "/run/gemm-autoresearch/controller.sock"
LOCAL_CANDIDATE_PATH = Path(
    "/var/lib/gemm-agent/workspace/gemm-autoresearch/candidate/candidate_gemm.cu"
)
MAX_CANDIDATE_BYTES = 1_048_576
MAX_RESPONSE_BYTES = 1_500_000
DEFAULT_WAIT_SECONDS = 2_700
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


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


def _restored_candidate(result: dict[str, Any]) -> tuple[bytes | None, dict[str, Any]]:
    available = result.get("available")
    if available is False:
        if "candidate_base64" in result:
            raise ClientError("controller returned candidate bytes when no best was available")
        return None, result
    if available is not True:
        raise ClientError("controller returned invalid best-candidate availability")
    encoded = result.get("candidate_base64")
    reported_size = result.get("candidate_bytes")
    candidate_sha = result.get("candidate_sha")
    source = result.get("source")
    iteration = result.get("iteration")
    if (
        not isinstance(encoded, str)
        or not isinstance(candidate_sha, str)
        or not SHA_RE.fullmatch(candidate_sha)
        or source not in {"best", "main"}
        or isinstance(reported_size, bool)
        or not isinstance(reported_size, int)
        or reported_size < 1
        or reported_size > MAX_CANDIDATE_BYTES
        or (
            source == "best"
            and (
                isinstance(iteration, bool)
                or not isinstance(iteration, int)
                or iteration < 1
            )
        )
        or (source == "main" and iteration is not None)
    ):
        raise ClientError("controller returned invalid best-candidate metadata")
    try:
        candidate = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ClientError("controller returned invalid best-candidate base64") from exc
    if len(candidate) != reported_size:
        raise ClientError("restored candidate size does not match controller metadata")
    if b"\0" in candidate:
        raise ClientError("restored candidate contains a NUL byte")
    try:
        candidate.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ClientError("restored candidate is not valid UTF-8") from exc
    public_result = {key: value for key, value in result.items() if key != "candidate_base64"}
    return candidate, public_result


def _write_candidate(path: Path, candidate: bytes) -> None:
    if not candidate or len(candidate) > MAX_CANDIDATE_BYTES or b"\0" in candidate:
        raise ClientError("restored candidate bytes are invalid")
    try:
        parent = path.parent.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise ClientError("candidate parent directory is missing or unsafe") from exc
    if parent != path.parent:
        raise ClientError("candidate parent directory must not contain symlinks")
    try:
        existing = path.lstat()
    except FileNotFoundError as exc:
        raise ClientError("candidate destination does not exist") from exc
    if not stat.S_ISREG(existing.st_mode):
        raise ClientError("candidate destination must be a regular, non-symlink file")

    directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary_path: str | None = None
    try:
        descriptor, temporary_path = tempfile.mkstemp(prefix=".best-candidate.", dir=parent)
        with os.fdopen(descriptor, "wb") as output:
            os.fchmod(output.fileno(), 0o644)
            output.write(candidate)
            output.flush()
            os.fsync(output.fileno())
        os.replace(
            Path(temporary_path).name,
            path.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        temporary_path = None
        os.fsync(directory_fd)
    except OSError as exc:
        raise ClientError("could not atomically restore the best candidate") from exc
    finally:
        os.close(directory_fd)
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gemmctl",
        description="Use the narrow trusted GitHub/DGX autoresearch controller.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("status", help="show the current research run and best result")

    subparsers.add_parser(
        "restore-best",
        help="atomically restore the controller's best accepted candidate",
    )

    start = subparsers.add_parser("start", help="start a new run after the previous PR is closed")
    start.add_argument("--title", default="GEMM autoresearch run")

    resume = subparsers.add_parser(
        "resume", help="resume result collection for the exact pending candidate SHA"
    )
    resume.add_argument(
        "--hypothesis",
        help="required only to recover a pending submission from controller v1",
    )
    resume.add_argument(
        "--timeout-seconds",
        type=int,
        default=DEFAULT_WAIT_SECONDS,
        help="DGX wait limit (60–2700 seconds; default: 2700)",
    )

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
        elif arguments.command == "resume":
            request["timeout_seconds"] = arguments.timeout_seconds
            if arguments.hypothesis is not None:
                request["hypothesis"] = arguments.hypothesis
            timeout = arguments.timeout_seconds
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
        if arguments.command == "restore-best":
            candidate, result = _restored_candidate(result)
            if candidate is not None:
                _write_candidate(LOCAL_CANDIDATE_PATH, candidate)
                result["restored"] = True
            else:
                result["restored"] = False
    except ClientError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
