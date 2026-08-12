#!/usr/bin/python3
"""Run a bounded number of isolated GEMM autoresearch iterations sequentially."""

from __future__ import annotations

import fcntl
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
from typing import Any, Callable, Sequence


AGENT_UNIT = "gemm-autoresearch-agent.service"
GEMMCTL = "/usr/local/bin/gemmctl"
SYSTEMCTL = "/usr/bin/systemctl"
BATCH_LOCK = Path("/run/lock/gemm-autoresearch/batch.lock")
MAX_BATCH_ITERATIONS = 50
COUNT_RE = re.compile(r"(?:[1-9]|[1-4][0-9]|50)")

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
STOP_AFTER_CURRENT = False


class BatchError(RuntimeError):
    """A safe batch failure suitable for the systemd journal."""


def parse_count(raw: str) -> int:
    if not COUNT_RE.fullmatch(raw):
        raise BatchError(
            f"iteration count must be a canonical integer from 1 to {MAX_BATCH_ITERATIONS}"
        )
    return int(raw)


def _positive_int(value: Any, label: str, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise BatchError(f"controller returned invalid {label}")
    return value


def parse_status(raw: str) -> dict[str, Any]:
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BatchError("gemmctl status returned invalid JSON") from exc
    if not isinstance(envelope, dict) or envelope.get("ok") is not True:
        raise BatchError("gemmctl status returned an unsuccessful response")
    result = envelope.get("result")
    if not isinstance(result, dict):
        raise BatchError("gemmctl status returned invalid result metadata")
    maximum = _positive_int(result.get("max_iterations_per_run"), "iteration maximum")
    remaining = _positive_int(
        result.get("remaining_iterations"), "remaining iteration count", allow_zero=True
    )
    if remaining > maximum:
        raise BatchError("controller remaining iteration count exceeds its maximum")
    active = result.get("active_run")
    if active is not None and not isinstance(active, dict):
        raise BatchError("controller returned invalid active-run metadata")
    if isinstance(active, dict):
        iteration = _positive_int(
            active.get("iteration"), "active iteration", allow_zero=True
        )
        if maximum - iteration != remaining:
            raise BatchError("controller iteration budget metadata is inconsistent")
        pending = active.get("pending_candidate_sha")
        if pending is not None and (
            not isinstance(pending, str) or not re.fullmatch(r"[0-9a-f]{40}", pending)
        ):
            raise BatchError("controller returned invalid pending candidate SHA")
    return result


def result_signature(status: dict[str, Any]) -> tuple[Any, ...] | None:
    active = status.get("active_run")
    if not isinstance(active, dict):
        return None
    last_result = active.get("last_result")
    if not isinstance(last_result, dict):
        return None
    candidate_sha = last_result.get("candidate_sha")
    completed_at = last_result.get("completed_at")
    iteration = last_result.get("iteration")
    if (
        not isinstance(candidate_sha, str)
        or not re.fullmatch(r"[0-9a-f]{40}", candidate_sha)
        or isinstance(completed_at, bool)
        or not isinstance(completed_at, int)
        or completed_at < 1
        or isinstance(iteration, bool)
        or not isinstance(iteration, int)
        or iteration < 1
    ):
        raise BatchError("controller returned invalid last-result metadata")
    return (active.get("id"), iteration, candidate_sha, completed_at)


def controller_status(run_command: CommandRunner) -> dict[str, Any]:
    try:
        completed = run_command(
            [GEMMCTL, "status"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BatchError("could not execute bounded gemmctl status") from exc
    if completed.returncode != 0:
        detail = " ".join(completed.stderr.split())[:500]
        raise BatchError(f"gemmctl status failed: {detail or 'no diagnostic'}")
    return parse_status(completed.stdout)


def _ensure_agent_inactive(run_command: CommandRunner) -> None:
    try:
        completed = run_command(
            [SYSTEMCTL, "is-active", "--quiet", AGENT_UNIT],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BatchError("could not query the isolated agent service") from exc
    if completed.returncode == 0:
        raise BatchError("the one-iteration agent service is already active")
    if completed.returncode not in {3, 4}:
        detail = " ".join(completed.stderr.split())[:500]
        raise BatchError(f"could not determine agent service state: {detail or 'unknown error'}")


def _run_agent(run_command: CommandRunner) -> None:
    try:
        completed = run_command(
            [SYSTEMCTL, "start", AGENT_UNIT],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=14_520,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BatchError("isolated agent iteration exceeded its bounded wait") from exc
    if completed.returncode != 0:
        detail = " ".join(completed.stderr.split())[:500]
        raise BatchError(f"isolated agent iteration failed: {detail or 'inspect its journal'}")


def _progress_line(completed: int, requested: int, status: dict[str, Any]) -> str:
    active = status.get("active_run")
    last_result = active.get("last_result") if isinstance(active, dict) else None
    best = active.get("best") if isinstance(active, dict) else None
    if not isinstance(last_result, dict):
        raise BatchError("completed agent iteration has no controller result")
    decision = last_result.get("decision")
    score = last_result.get("score_geomean_vs_cublas")
    best_score = best.get("score_geomean_vs_cublas") if isinstance(best, dict) else None
    if decision not in {"new best", "not improved", "rejected"}:
        raise BatchError("controller returned invalid result decision")
    for value, label in ((score, "result score"), (best_score, "best score")):
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise BatchError(f"controller returned invalid {label}")
    return (
        f"Batch progress {completed}/{requested}: "
        f"controller_iteration={last_result.get('iteration')} "
        f"decision={decision} score={score} best={best_score}"
    )


def run_iterations(count: int, run_command: CommandRunner = subprocess.run) -> int:
    global STOP_AFTER_CURRENT
    status = controller_status(run_command)
    remaining = status["remaining_iterations"]
    if count > remaining:
        raise BatchError(
            f"requested {count} iterations but the active controller run has only "
            f"{remaining} remaining"
        )
    active = status.get("active_run")
    if isinstance(active, dict) and active.get("pending_candidate_sha") is not None:
        raise BatchError("a candidate result is pending; resume it before starting a batch")
    _ensure_agent_inactive(run_command)

    print(
        f"Starting bounded autoresearch batch: requested={count} "
        f"remaining_budget={remaining}",
        flush=True,
    )
    completed_count = 0
    while completed_count < count:
        if STOP_AFTER_CURRENT:
            print(
                f"Stop requested; batch ended after {completed_count}/{count} iterations",
                flush=True,
            )
            return completed_count
        if status["remaining_iterations"] < 1:
            raise BatchError("controller iteration budget was exhausted during the batch")
        active = status.get("active_run")
        if isinstance(active, dict) and active.get("pending_candidate_sha") is not None:
            raise BatchError("a candidate result became pending during the batch")
        before = result_signature(status)
        _ensure_agent_inactive(run_command)
        if STOP_AFTER_CURRENT:
            print(
                f"Stop requested; batch ended after {completed_count}/{count} iterations",
                flush=True,
            )
            return completed_count
        print(f"Starting batch iteration {completed_count + 1}/{count}", flush=True)
        _run_agent(run_command)
        status = controller_status(run_command)
        after = result_signature(status)
        if after is None or after == before:
            raise BatchError("agent service exited without a new trusted controller result")
        completed_count += 1
        print(_progress_line(completed_count, count, status), flush=True)

    print(f"Autoresearch batch completed: {completed_count}/{count}", flush=True)
    return completed_count


def acquire_lock() -> int:
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        descriptor = os.open(BATCH_LOCK, flags, 0o600)
    except OSError as exc:
        raise BatchError(f"could not open trusted batch lock {BATCH_LOCK}") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_mode & 0o077
        ):
            raise BatchError("trusted batch lock has unsafe ownership or permissions")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BatchError("another autoresearch batch is already active") from exc
        os.ftruncate(descriptor, 0)
        os.write(descriptor, f"pid={os.getpid()}\n".encode("ascii"))
        os.fsync(descriptor)
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _request_stop_after_current(signum: int, frame: Any) -> None:
    del signum, frame
    global STOP_AFTER_CURRENT
    STOP_AFTER_CURRENT = True
    print("Stop-after-current requested; no additional iteration will start", flush=True)


def main(arguments: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if arguments is None else arguments)
    if len(args) != 1:
        print("usage: run_autoresearch_batch.py ITERATIONS", file=sys.stderr)
        return 2
    try:
        if os.geteuid() != 0:
            raise BatchError("batch scheduler must run as root through systemd")
        count = parse_count(args[0])
        signal.signal(signal.SIGUSR1, _request_stop_after_current)
        lock_descriptor = acquire_lock()
        try:
            run_iterations(count)
        finally:
            os.close(lock_descriptor)
    except BatchError as exc:
        print(f"autoresearch batch failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
