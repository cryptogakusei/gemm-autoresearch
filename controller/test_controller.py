#!/usr/bin/python3

from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import urllib.error
import zipfile

from controller import gemm_controller as controller
from controller import gemmctl as client


def artifact_bytes(
    candidate_sha: str,
    *,
    score: float = 0.25,
    correctness_status: str = "PASS",
    include_gpu_attestation: bool = True,
    gpu_lock_wait_seconds: str = "0",
) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "identity.txt",
            "candidate_repository=cryptogakusei/gemm-autoresearch\n"
            f"candidate_sha={candidate_sha}\n"
            f"verifier_sha={'a' * 40}\n",
        )
        sandbox = (
            "image=example@sha256:deadbeef\n"
            "rootless=true\n"
            "network=none\n"
            "read_only_root=true\n"
            "capabilities=none\n"
        )
        if include_gpu_attestation:
            sandbox += (
                "gpu_lease=exclusive\n"
                "gpu_idle_checks=passed\n"
                f"gpu_lock_wait_seconds={gpu_lock_wait_seconds}\n"
            )
        sandbox += (
            "candidate_bytes=100\n"
            "artifact_bytes=200\n"
            f"container_exit_code={0 if correctness_status == 'PASS' else 1}\n"
        )
        archive.writestr("sandbox.txt", sandbox)
        archive.writestr(
            "correctness.csv",
            ",".join(controller.CORRECTNESS_HEADER)
            + "\n"
            + f"tiny,{correctness_status},cpu,0,0,0,256,0,0,0\n",
        )
        if correctness_status == "PASS":
            archive.writestr(
                "performance.csv",
                ",".join(controller.PERFORMANCE_HEADER)
                + "\n"
                + f"square_16,16,16,16,20,1,1,1,1,{score}\n",
            )
            archive.writestr(
                "score.txt",
                f"score_geomean_vs_cublas={score:.6f}\n"
                f"score_percent_of_cublas={score * 100:.2f}\n"
                f"worst_ratio={score:.6f}\n"
                "worst_case=square_16\n"
                "performance_cases=1\n",
            )
    return output.getvalue()


class FakeGitHub:
    repository = "cryptogakusei/gemm-autoresearch"

    def __init__(self) -> None:
        self.counter = 1
        self.main_sha = "1" * 40
        self.head_sha: str | None = None
        self.branch: str | None = None
        self.pr_body = ""
        self.pr_state = "open"
        self.score = 0.25
        self.correctness_status = "PASS"
        self.candidates: dict[str, bytes] = {
            self.main_sha: b"// protected main baseline\n"
        }

    def main_ref(self) -> str:
        return self.main_sha

    def ref(self, branch: str) -> str:
        if branch != self.branch or self.head_sha is None:
            raise AssertionError("unexpected branch lookup")
        return self.head_sha

    def create_candidate_commit(self, parent: str, candidate: bytes, message: str) -> str:
        self.counter += 1
        self.pending_sha = f"{self.counter:040x}"
        self.last_candidate = candidate
        self.last_message = message
        self.candidates[self.pending_sha] = candidate
        return self.pending_sha

    def candidate_at_commit(self, commit_sha: str) -> bytes:
        try:
            return self.candidates[commit_sha]
        except KeyError as exc:
            raise AssertionError("unexpected candidate commit lookup") from exc

    def create_branch(self, branch: str, commit_sha: str) -> None:
        self.branch = branch
        self.head_sha = commit_sha

    def update_branch(self, branch: str, commit_sha: str) -> None:
        if branch != self.branch:
            raise AssertionError("unexpected branch update")
        self.head_sha = commit_sha

    def create_pull_request(self, branch: str, title: str, body: str) -> dict[str, object]:
        if branch != self.branch:
            raise AssertionError("PR did not use the controlled branch")
        self.pr_body = body
        return {"number": 7, "html_url": "https://github.com/example/pr/7"}

    def pull_request(self, number: int) -> dict[str, object]:
        return {"state": self.pr_state, "head": {"sha": self.head_sha}}

    def update_pull_request_body(self, number: int, body: str) -> None:
        self.pr_body = body

    def workflow_runs(self, candidate_sha: str) -> list[dict[str, object]]:
        return [
            {
                "id": 99,
                "event": "pull_request_target",
                "head_sha": candidate_sha,
                "path": controller.WORKFLOW_PATH,
                "status": "completed",
                "conclusion": "success",
                "html_url": "https://github.com/example/actions/99",
            }
        ]

    def run_artifacts(self, run_id: int) -> list[dict[str, object]]:
        return [
            {
                "id": 12,
                "name": f"gemm-results-{self.head_sha}",
                "expired": False,
            }
        ]

    def download_artifact(self, artifact_id: int) -> bytes:
        assert self.head_sha is not None
        return artifact_bytes(
            self.head_sha,
            score=self.score,
            correctness_status=self.correctness_status,
        )


class FailingOnceGitHub(FakeGitHub):
    def __init__(self) -> None:
        super().__init__()
        self.workflow_attempts = 0

    def workflow_runs(self, candidate_sha: str) -> list[dict[str, object]]:
        self.workflow_attempts += 1
        if self.workflow_attempts == 1:
            raise controller.ControllerError("transient API failure")
        return super().workflow_runs(candidate_sha)


class ValidationTests(unittest.TestCase):
    def test_candidate_limits_and_nul(self) -> None:
        self.assertEqual(controller.validate_candidate("hello"), b"hello")
        with self.assertRaises(controller.ControllerError):
            controller.validate_candidate("")
        with self.assertRaises(controller.ControllerError):
            controller.validate_candidate("bad\0source")
        with self.assertRaises(controller.ControllerError):
            controller.validate_candidate("x" * (controller.MAX_CANDIDATE_BYTES + 1))

    def test_request_rejects_unknown_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            instance = controller.Controller(
                FakeGitHub(), controller.StateStore(Path(directory) / "state.json"), 3
            )
            with self.assertRaises(controller.ControllerError):
                instance.dispatch(
                    {"version": 1, "operation": "status", "arbitrary_api_path": "/user"}
                )
            with self.assertRaises(controller.ControllerError):
                instance.dispatch(
                    {
                        "version": 1,
                        "operation": "restore-best",
                        "candidate_sha": "a" * 40,
                    }
                )

    def test_state_file_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            store = controller.StateStore(path)
            value = {"schema": 1, "active_run": {"id": "example"}}
            store.save(value)
            self.assertEqual(store.load(), value)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_client_decodes_and_writes_restored_candidate_atomically(self) -> None:
        candidate = b"// trusted best\n"
        result = {
            "available": True,
            "source": "best",
            "iteration": 1,
            "candidate_sha": "a" * 40,
            "candidate_bytes": len(candidate),
            "candidate_base64": controller.base64.b64encode(candidate).decode("ascii"),
        }
        restored, public = client._restored_candidate(result)
        self.assertEqual(restored, candidate)
        self.assertNotIn("candidate_base64", public)
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory).resolve() / "candidate_gemm.cu"
            destination.write_text("// latest regression\n", encoding="utf-8")
            client._write_candidate(destination, candidate)
            self.assertEqual(destination.read_bytes(), candidate)
            self.assertEqual(destination.stat().st_mode & 0o777, 0o644)

    def test_client_refuses_symlink_candidate_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory).resolve()
            target = parent / "target.cu"
            target.write_text("// target\n", encoding="utf-8")
            destination = parent / "candidate_gemm.cu"
            destination.symlink_to(target)
            with self.assertRaisesRegex(client.ClientError, "regular, non-symlink"):
                client._write_candidate(destination, b"// trusted best\n")


class ConnectionRetryTests(unittest.TestCase):
    def test_candidate_fetch_is_fixed_to_exact_path_and_commit(self) -> None:
        authentication = mock.Mock()
        github = controller.GitHubClient(
            "cryptogakusei/gemm-autoresearch", authentication
        )
        candidate = b"// accepted source\n"
        response = {
            "type": "file",
            "path": controller.CANDIDATE_PATH,
            "encoding": "base64",
            "size": len(candidate),
            "content": controller.base64.b64encode(candidate).decode("ascii"),
        }
        with mock.patch.object(github, "api", return_value=response) as api:
            self.assertEqual(github.candidate_at_commit("a" * 40), candidate)
        requested = api.call_args.args[1]
        self.assertIn("/contents/candidate/candidate_gemm.cu?", requested)
        self.assertIn("ref=" + "a" * 40, requested)

    def test_safe_get_retries_a_fresh_verified_connection(self) -> None:
        authentication = mock.Mock()
        authentication.token.return_value = "temporary-token"
        github = controller.GitHubClient(
            "cryptogakusei/gemm-autoresearch", authentication
        )
        with mock.patch.object(controller.time, "sleep"), mock.patch.object(
            controller.urllib.request,
            "urlopen",
            side_effect=[urllib.error.URLError("certificate failure"), io.BytesIO(b"{}")],
        ) as urlopen:
            self.assertEqual(github.api("GET", "/meta"), {})
        self.assertEqual(urlopen.call_count, 2)

    def test_repository_mutation_is_not_retried(self) -> None:
        authentication = mock.Mock()
        authentication.token.return_value = "temporary-token"
        github = controller.GitHubClient(
            "cryptogakusei/gemm-autoresearch", authentication
        )
        with mock.patch.object(
            controller.urllib.request,
            "urlopen",
            side_effect=urllib.error.URLError("connection lost"),
        ) as urlopen, self.assertRaises(controller.ControllerError):
            github.api("POST", "/mutation", {"example": True})
        self.assertEqual(urlopen.call_count, 1)

    def test_token_mint_retries_without_weakening_verification(self) -> None:
        authentication = object.__new__(controller.AppAuthentication)
        authentication.app_id = 1
        authentication.installation_id = 2
        authentication.repository = "cryptogakusei/gemm-autoresearch"
        authentication.key_path = Path("/unused")
        authentication._token = None
        authentication._expires_at = 0.0
        response = io.BytesIO(b'{"token":"short-lived","expires_at":"soon"}')
        with mock.patch.object(authentication, "_jwt", return_value="signed-jwt"), mock.patch.object(
            controller.time, "sleep"
        ), mock.patch.object(
            controller.urllib.request,
            "urlopen",
            side_effect=[urllib.error.URLError("certificate failure"), response],
        ) as urlopen:
            self.assertEqual(authentication.token(), "short-lived")
        self.assertEqual(urlopen.call_count, 2)


class ArtifactTests(unittest.TestCase):
    def test_accepts_exact_passing_artifact(self) -> None:
        sha = "b" * 40
        result = controller.parse_artifact(
            artifact_bytes(sha),
            "cryptogakusei/gemm-autoresearch",
            sha,
            "https://github.com/example/actions/1",
            "success",
        )
        self.assertTrue(result["correctness"])
        self.assertEqual(result["score_geomean_vs_cublas"], 0.25)

    def test_rejects_candidate_identity_mismatch(self) -> None:
        with self.assertRaisesRegex(controller.ControllerError, "candidate SHA does not match"):
            controller.parse_artifact(
                artifact_bytes("b" * 40),
                "cryptogakusei/gemm-autoresearch",
                "c" * 40,
                "https://github.com/example/actions/1",
                "success",
            )

    def test_rejects_artifact_without_exclusive_gpu_attestation(self) -> None:
        sha = "b" * 40
        with self.assertRaisesRegex(controller.ControllerError, "sandbox properties"):
            controller.parse_artifact(
                artifact_bytes(sha, include_gpu_attestation=False),
                "cryptogakusei/gemm-autoresearch",
                sha,
                "https://github.com/example/actions/1",
                "success",
            )

    def test_rejects_invalid_gpu_lock_wait(self) -> None:
        sha = "b" * 40
        with self.assertRaisesRegex(controller.ControllerError, "GPU lock wait"):
            controller.parse_artifact(
                artifact_bytes(sha, gpu_lock_wait_seconds="301"),
                "cryptogakusei/gemm-autoresearch",
                sha,
                "https://github.com/example/actions/1",
                "success",
            )

    def test_correctness_failure_never_has_a_score(self) -> None:
        sha = "b" * 40
        result = controller.parse_artifact(
            artifact_bytes(sha, correctness_status="FAIL"),
            "cryptogakusei/gemm-autoresearch",
            sha,
            "https://github.com/example/actions/1",
            "failure",
        )
        self.assertFalse(result["correctness"])
        self.assertIsNone(result["score_geomean_vs_cublas"])
        self.assertEqual(result["failed_cases"], ["tiny"])

    def test_rejects_unsafe_zip_path(self) -> None:
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("../identity.txt", "bad")
        with self.assertRaisesRegex(controller.ControllerError, "unsafe"):
            controller.parse_artifact(
                output.getvalue(),
                "cryptogakusei/gemm-autoresearch",
                "b" * 40,
                "https://github.com/example/actions/1",
                "success",
            )


class ControllerFlowTests(unittest.TestCase):
    def test_status_reports_remaining_submission_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            github = FakeGitHub()
            store = controller.StateStore(Path(directory) / "state.json")
            instance = controller.Controller(github, store, 3)
            initial = instance.status()
            self.assertEqual(initial["max_iterations_per_run"], 3)
            self.assertEqual(initial["remaining_iterations"], 3)

            instance.submit(b"// candidate\n", "first optimization", 60)
            current = instance.status()
            self.assertEqual(current["active_run"]["iteration"], 1)
            self.assertEqual(current["remaining_iterations"], 2)

    def test_restore_best_returns_best_source_after_a_regression(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            github = FakeGitHub()
            store = controller.StateStore(Path(directory) / "state.json")
            instance = controller.Controller(github, store, 3)

            github.score = 0.5
            first = instance.submit(b"// accepted best\n", "first optimization", 60)
            self.assertEqual(first["decision"], "new best")
            best_sha = first["candidate_sha"]

            github.score = 0.2
            second = instance.submit(b"// slower attempt\n", "risky optimization", 60)
            self.assertEqual(second["decision"], "not improved")
            self.assertNotEqual(second["candidate_sha"], best_sha)

            restored = instance.dispatch({"version": 1, "operation": "restore-best"})
            self.assertTrue(restored["available"])
            self.assertEqual(restored["source"], "best")
            self.assertEqual(restored["candidate_sha"], best_sha)
            self.assertEqual(
                controller.base64.b64decode(restored["candidate_base64"]),
                b"// accepted best\n",
            )

    def test_restore_best_uses_main_before_an_accepted_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            github = FakeGitHub()
            instance = controller.Controller(
                github, controller.StateStore(Path(directory) / "state.json"), 3
            )
            restored = instance.dispatch({"version": 1, "operation": "restore-best"})
            self.assertTrue(restored["available"])
            self.assertEqual(restored["source"], "main")
            self.assertEqual(restored["candidate_sha"], github.main_sha)
            self.assertEqual(
                controller.base64.b64decode(restored["candidate_base64"]),
                b"// protected main baseline\n",
            )

    def test_restore_best_uses_main_after_rejected_first_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            github = FakeGitHub()
            store = controller.StateStore(Path(directory) / "state.json")
            instance = controller.Controller(github, store, 3)
            github.correctness_status = "FAIL"
            result = instance.submit(b"// incorrect attempt\n", "unsafe change", 60)
            self.assertEqual(result["decision"], "rejected")

            restored = instance.dispatch({"version": 1, "operation": "restore-best"})
            self.assertEqual(restored["source"], "main")
            self.assertEqual(
                controller.base64.b64decode(restored["candidate_base64"]),
                b"// protected main baseline\n",
            )

    def test_submit_creates_only_candidate_pr_and_tracks_best(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            github = FakeGitHub()
            store = controller.StateStore(Path(directory) / "state.json")
            instance = controller.Controller(github, store, 3)
            result = instance.dispatch(
                {
                    "version": 1,
                    "operation": "submit",
                    "candidate": "// candidate\n",
                    "hypothesis": "try a tiled kernel",
                    "timeout_seconds": 60,
                }
            )
            self.assertEqual(github.last_candidate, b"// candidate\n")
            self.assertEqual(result["decision"], "new best")
            self.assertEqual(result["pr_number"], 7)
            self.assertIn("try a tiled kernel", github.pr_body)
            self.assertEqual(store.load()["active_run"]["best"]["iteration"], 1)

    def test_open_pr_blocks_new_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            github = FakeGitHub()
            store = controller.StateStore(Path(directory) / "state.json")
            instance = controller.Controller(github, store, 3)
            instance.start("first")
            state = store.load()
            state["active_run"]["pr_number"] = 7
            store.save(state)
            with self.assertRaisesRegex(controller.ControllerError, "still has open PR"):
                instance.start("second")

    def test_failed_result_collection_is_resumable_without_a_new_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            github = FailingOnceGitHub()
            store = controller.StateStore(Path(directory) / "state.json")
            instance = controller.Controller(github, store, 3)
            with self.assertRaisesRegex(controller.ControllerError, "transient"):
                instance.submit(b"// candidate\n", "test recovery", 60)
            submitted_sha = store.load()["active_run"]["head_sha"]
            self.assertEqual(github.counter, 2)
            result = instance.dispatch(
                {"version": 1, "operation": "resume", "timeout_seconds": 60}
            )
            self.assertEqual(result["candidate_sha"], submitted_sha)
            self.assertEqual(github.counter, 2)
            self.assertNotIn("pending", store.load()["active_run"])

    def test_legacy_pending_sha_can_be_recovered_with_hypothesis(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            github = FakeGitHub()
            store = controller.StateStore(Path(directory) / "state.json")
            instance = controller.Controller(github, store, 3)
            instance.start("legacy")
            state = store.load()
            run = state["active_run"]
            legacy_sha = "9" * 40
            run.update(
                {
                    "pr_number": 7,
                    "pr_url": "https://github.com/example/pr/7",
                    "head_sha": legacy_sha,
                    "iteration": 1,
                }
            )
            store.save(state)
            github.branch = run["branch"]
            github.head_sha = legacy_sha
            with self.assertRaisesRegex(controller.ControllerError, "requires --hypothesis"):
                instance.resume(60)
            result = instance.resume(60, "recover the original submission")
            self.assertEqual(result["candidate_sha"], legacy_sha)
            self.assertEqual(result["decision"], "new best")


if __name__ == "__main__":
    unittest.main()
