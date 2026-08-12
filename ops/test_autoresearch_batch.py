#!/usr/bin/python3

from __future__ import annotations

import json
import subprocess
import unittest

from ops import run_autoresearch_batch as batch


def status_json(
    iteration: int,
    remaining: int,
    *,
    candidate_sha: str | None = None,
    completed_at: int | None = None,
    pending_sha: str | None = None,
    decision: str = "new best",
    score: float = 0.5,
) -> str:
    last_result = None
    best = None
    if candidate_sha is not None and completed_at is not None:
        last_result = {
            "iteration": iteration,
            "candidate_sha": candidate_sha,
            "completed_at": completed_at,
            "decision": decision,
            "score_geomean_vs_cublas": score,
        }
        best = {
            "iteration": iteration,
            "candidate_sha": candidate_sha,
            "score_geomean_vs_cublas": score,
        }
    return json.dumps(
        {
            "ok": True,
            "result": {
                "max_iterations_per_run": 50,
                "remaining_iterations": remaining,
                "active_run": {
                    "id": "run-1",
                    "iteration": iteration,
                    "pending_candidate_sha": pending_sha,
                    "last_result": last_result,
                    "best": best,
                },
            },
        }
    )


class FakeRunner:
    def __init__(self, statuses: list[str], *, start_returncode: int = 0) -> None:
        self.statuses = list(statuses)
        self.start_returncode = start_returncode
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        self.calls.append(command)
        if command == [batch.GEMMCTL, "status"]:
            if not self.statuses:
                raise AssertionError("unexpected extra controller status call")
            return subprocess.CompletedProcess(command, 0, self.statuses.pop(0), "")
        if command == [batch.SYSTEMCTL, "is-active", "--quiet", batch.AGENT_UNIT]:
            return subprocess.CompletedProcess(command, 3, "", "")
        if command == [batch.SYSTEMCTL, "start", batch.AGENT_UNIT]:
            return subprocess.CompletedProcess(
                command,
                self.start_returncode,
                "",
                "agent failed" if self.start_returncode else "",
            )
        raise AssertionError(f"unexpected command: {command}")


class BatchTests(unittest.TestCase):
    def setUp(self) -> None:
        batch.STOP_AFTER_CURRENT = False

    def test_count_must_be_canonical_and_bounded(self) -> None:
        self.assertEqual(batch.parse_count("1"), 1)
        self.assertEqual(batch.parse_count("50"), 50)
        for invalid in ("0", "00", "01", "51", "-1", "1.0", "ten", ""):
            with self.subTest(invalid=invalid), self.assertRaises(batch.BatchError):
                batch.parse_count(invalid)

    def test_two_iterations_run_sequentially_and_require_new_results(self) -> None:
        runner = FakeRunner(
            [
                status_json(2, 48, candidate_sha="a" * 40, completed_at=100),
                status_json(3, 47, candidate_sha="b" * 40, completed_at=200),
                status_json(4, 46, candidate_sha="c" * 40, completed_at=300),
            ]
        )
        self.assertEqual(batch.run_iterations(2, runner), 2)
        starts = [
            call
            for call in runner.calls
            if call == [batch.SYSTEMCTL, "start", batch.AGENT_UNIT]
        ]
        self.assertEqual(len(starts), 2)
        self.assertFalse(runner.statuses)

    def test_request_larger_than_remaining_budget_fails_before_start(self) -> None:
        runner = FakeRunner(
            [status_json(49, 1, candidate_sha="a" * 40, completed_at=100)]
        )
        with self.assertRaisesRegex(batch.BatchError, "only 1 remaining"):
            batch.run_iterations(2, runner)
        self.assertNotIn([batch.SYSTEMCTL, "start", batch.AGENT_UNIT], runner.calls)

    def test_pending_submission_blocks_batch(self) -> None:
        runner = FakeRunner(
            [
                status_json(
                    3,
                    47,
                    candidate_sha="a" * 40,
                    completed_at=100,
                    pending_sha="b" * 40,
                )
            ]
        )
        with self.assertRaisesRegex(batch.BatchError, "pending"):
            batch.run_iterations(1, runner)

    def test_agent_failure_stops_batch(self) -> None:
        runner = FakeRunner(
            [status_json(2, 48, candidate_sha="a" * 40, completed_at=100)],
            start_returncode=1,
        )
        with self.assertRaisesRegex(batch.BatchError, "agent iteration failed"):
            batch.run_iterations(2, runner)
        starts = [
            call
            for call in runner.calls
            if call == [batch.SYSTEMCTL, "start", batch.AGENT_UNIT]
        ]
        self.assertEqual(len(starts), 1)

    def test_unchanged_controller_result_fails_closed(self) -> None:
        unchanged = status_json(2, 48, candidate_sha="a" * 40, completed_at=100)
        runner = FakeRunner([unchanged, unchanged])
        with self.assertRaisesRegex(batch.BatchError, "without a new trusted"):
            batch.run_iterations(1, runner)

    def test_stop_after_current_prevents_another_iteration(self) -> None:
        class StopRunner(FakeRunner):
            def __call__(
                self, command: list[str], **kwargs: object
            ) -> subprocess.CompletedProcess[str]:
                completed = super().__call__(command, **kwargs)
                if command == [batch.SYSTEMCTL, "start", batch.AGENT_UNIT]:
                    batch.STOP_AFTER_CURRENT = True
                return completed

        runner = StopRunner(
            [
                status_json(2, 48, candidate_sha="a" * 40, completed_at=100),
                status_json(3, 47, candidate_sha="b" * 40, completed_at=200),
            ]
        )
        self.assertEqual(batch.run_iterations(3, runner), 1)


if __name__ == "__main__":
    unittest.main()
