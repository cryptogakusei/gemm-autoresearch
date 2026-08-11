#!/usr/bin/python3
"""Trusted broker between an untrusted research agent and GitHub/DGX CI.

The broker deliberately exposes only four operations over a Unix socket:
start a research run, submit candidate source, resume result collection for an
already-submitted SHA, and inspect status. GitHub App credentials never cross
that socket. There is intentionally no merge, close, arbitrary-ref,
arbitrary-path, or arbitrary-API operation.
"""

from __future__ import annotations

import base64
import calendar
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request
import zipfile


API_ROOT = "https://api.github.com"
API_VERSION = "2026-03-10"
USER_AGENT = "gemm-autoresearch-controller/1"
WORKFLOW_FILE = "gemm-autoresearch.yml"
WORKFLOW_PATH = ".github/workflows/gemm-autoresearch.yml"
BASE_BRANCH = "main"
CANDIDATE_PATH = "candidate/candidate_gemm.cu"
SOCKET_PATH = Path("/run/gemm-autoresearch/controller.sock")
STATE_PATH = Path("/var/lib/gemm-autoresearch/controller-state.json")
PRIVATE_KEY_PATH = Path("/etc/gemm-autoresearch/github-app.pem")

PROTOCOL_VERSION = 1
# A one-MiB UTF-8 candidate can expand to roughly six MiB when JSON escapes
# every ASCII control byte.  Keep that valid case bounded without silently
# lowering the documented source limit.
MAX_REQUEST_BYTES = 6_400_000
MAX_RESPONSE_BYTES = 1_000_000
MAX_CANDIDATE_BYTES = 1_048_576
MAX_HYPOTHESIS_CHARS = 500
MAX_TITLE_CHARS = 100
MAX_ARTIFACT_BYTES = 134_217_728
MIN_WAIT_SECONDS = 60
MAX_WAIT_SECONDS = 2_700
DEFAULT_WAIT_SECONDS = 2_700
POLL_SECONDS = 5

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
BRANCH_RE = re.compile(r"^autoresearch/run-[0-9]{8}-[0-9]{6}-[0-9a-f]{8}$")


class ControllerError(RuntimeError):
    """A safe, expected controller failure that may be returned to the client."""


class GitHubError(ControllerError):
    def __init__(self, status: int, method: str, path: str, detail: str):
        super().__init__(f"GitHub API {method} {path} failed with HTTP {status}: {detail}")
        self.status = status


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _read_limited(stream: Any, limit: int) -> bytes:
    data = stream.read(limit + 1)
    if len(data) > limit:
        raise ControllerError(f"response exceeded the {limit}-byte limit")
    return data


def _safe_api_detail(raw: bytes) -> str:
    text = raw.decode("utf-8", "replace")[:1_000]
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict) and isinstance(parsed.get("message"), str):
            return parsed["message"][:500]
    except json.JSONDecodeError:
        pass
    return " ".join(text.split())[:500] or "empty response"


def _strict_key_values(text: str, label: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in text.splitlines():
        if not line:
            continue
        key, separator, value = line.partition("=")
        if not separator or not key or key in result:
            raise ControllerError(f"{label} contains an invalid or duplicate key")
        result[key] = value
    return result


def _clean_summary(value: str, limit: int) -> str:
    translations = str.maketrans(
        {"|": "/", "@": "＠", "`": "'", "<": "(", ">": ")", "[": "(", "]": ")"}
    )
    return " ".join(value.translate(translations).split())[:limit]


def validate_candidate(candidate: Any) -> bytes:
    if not isinstance(candidate, str):
        raise ControllerError("candidate must be a UTF-8 JSON string")
    try:
        encoded = candidate.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ControllerError("candidate is not valid UTF-8") from exc
    if not encoded or len(encoded) > MAX_CANDIDATE_BYTES:
        raise ControllerError(
            f"candidate must be between 1 and {MAX_CANDIDATE_BYTES} UTF-8 bytes"
        )
    if b"\0" in encoded:
        raise ControllerError("candidate must not contain NUL bytes")
    return encoded


def validate_hypothesis(hypothesis: Any) -> str:
    if not isinstance(hypothesis, str):
        raise ControllerError("hypothesis must be a string")
    cleaned = " ".join(hypothesis.split())
    if not cleaned or len(cleaned) > MAX_HYPOTHESIS_CHARS:
        raise ControllerError(
            f"hypothesis must be between 1 and {MAX_HYPOTHESIS_CHARS} characters"
        )
    return cleaned


def validate_title(title: Any) -> str:
    if not isinstance(title, str):
        raise ControllerError("title must be a string")
    cleaned = " ".join(title.split())
    if not cleaned or len(cleaned) > MAX_TITLE_CHARS:
        raise ControllerError(f"title must be between 1 and {MAX_TITLE_CHARS} characters")
    return cleaned


def validate_timeout(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ControllerError("timeout_seconds must be an integer")
    if value < MIN_WAIT_SECONDS or value > MAX_WAIT_SECONDS:
        raise ControllerError(
            f"timeout_seconds must be between {MIN_WAIT_SECONDS} and {MAX_WAIT_SECONDS}"
        )
    return value


class AppAuthentication:
    """Mint narrowly scoped, one-hour GitHub installation tokens."""

    def __init__(self, app_id: int, installation_id: int, repository: str, key_path: Path):
        self.app_id = app_id
        self.installation_id = installation_id
        self.repository = repository
        self.key_path = key_path
        self._token: str | None = None
        self._expires_at = 0.0
        self._validate_key()

    def _validate_key(self) -> None:
        if self.key_path.is_symlink():
            raise ControllerError("GitHub App key must not be a symlink")
        try:
            key_stat = self.key_path.stat()
        except FileNotFoundError as exc:
            raise ControllerError(f"GitHub App key not found: {self.key_path}") from exc
        if not stat.S_ISREG(key_stat.st_mode) or key_stat.st_uid != 0:
            raise ControllerError("GitHub App key must be a regular file owned by root")
        if key_stat.st_mode & 0o027:
            raise ControllerError("GitHub App key must not be writable by group or accessible by others")

    def _jwt(self) -> str:
        now = int(time.time())
        header = _b64url(_json_bytes({"alg": "RS256", "typ": "JWT"}))
        payload = _b64url(
            _json_bytes({"iat": now - 60, "exp": now + 540, "iss": self.app_id})
        )
        signing_input = f"{header}.{payload}".encode("ascii")
        try:
            completed = subprocess.run(
                ["/usr/bin/openssl", "dgst", "-sha256", "-sign", str(self.key_path)],
                input=signing_input,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ControllerError("failed to invoke OpenSSL for GitHub App authentication") from exc
        if completed.returncode != 0 or not completed.stdout:
            raise ControllerError("OpenSSL could not sign the GitHub App JWT")
        return f"{header}.{payload}.{_b64url(completed.stdout)}"

    def token(self) -> str:
        if self._token and time.time() < self._expires_at - 120:
            return self._token

        path = f"/app/installations/{self.installation_id}/access_tokens"
        body = {
            "repositories": [self.repository.split("/", 1)[1]],
            "permissions": {
                "actions": "read",
                "contents": "write",
                "pull_requests": "write",
            },
        }
        for attempt in range(3):
            request = urllib.request.Request(
                f"{API_ROOT}{path}",
                data=_json_bytes(body),
                method="POST",
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {self._jwt()}",
                    "Content-Type": "application/json",
                    "User-Agent": USER_AGENT,
                    "X-GitHub-Api-Version": API_VERSION,
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    raw = _read_limited(response, 1_000_000)
                break
            except urllib.error.HTTPError as exc:
                raw = _read_limited(exc, 64_000)
                if exc.code in (500, 502, 503, 504) and attempt < 2:
                    time.sleep(1 + attempt)
                    continue
                raise GitHubError(exc.code, "POST", path, _safe_api_detail(raw)) from exc
            except urllib.error.URLError as exc:
                if attempt < 2:
                    # Minting another short-lived, identically down-scoped token
                    # is safe if a response was lost. Verification remains strict
                    # on every fresh TLS connection.
                    time.sleep(1 + attempt)
                    continue
                raise ControllerError(
                    f"could not reach GitHub while minting a token: {exc.reason}"
                ) from exc

        try:
            parsed = json.loads(raw)
            token = parsed["token"]
            expires = parsed["expires_at"]
            if not isinstance(token, str) or not token or not isinstance(expires, str):
                raise ValueError
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise ControllerError("GitHub returned an invalid installation-token response") from exc

        # Installation tokens currently last one hour.  Use a conservative local
        # lifetime so clock skew or a response-format change cannot extend access.
        self._token = token
        self._expires_at = time.time() + 3_300
        return token


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


class GitHubClient:
    def __init__(self, repository: str, authentication: AppAuthentication):
        self.repository = repository
        self.owner, self.repo = repository.split("/", 1)
        self.authentication = authentication

    def api(self, method: str, path: str, body: Any = None, limit: int = 8_000_000) -> Any:
        data = None if body is None else _json_bytes(body)
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.authentication.token()}",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": API_VERSION,
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        attempts = 3 if method == "GET" else 1
        for attempt in range(attempts):
            request = urllib.request.Request(
                f"{API_ROOT}{path}", data=data, method=method, headers=headers
            )
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    raw = _read_limited(response, limit)
                break
            except urllib.error.HTTPError as exc:
                raw = _read_limited(exc, 64_000)
                if exc.code in (500, 502, 503, 504) and attempt + 1 < attempts:
                    time.sleep(1 + attempt)
                    continue
                raise GitHubError(exc.code, method, path, _safe_api_detail(raw)) from exc
            except urllib.error.URLError as exc:
                if attempt + 1 < attempts:
                    # Verification remains strict on every attempt.  In
                    # particular, never install an unverified SSL context to
                    # work around a transient certificate-chain failure.
                    time.sleep(1 + attempt)
                    continue
                raise ControllerError(
                    f"could not reach GitHub for {method} {path}: {exc.reason}"
                ) from exc
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ControllerError(f"GitHub returned invalid JSON for {method} {path}") from exc

    def repo_path(self, suffix: str) -> str:
        return f"/repos/{self.owner}/{self.repo}{suffix}"

    def main_ref(self) -> str:
        data = self.api("GET", self.repo_path(f"/git/ref/heads/{BASE_BRANCH}"))
        sha = data.get("object", {}).get("sha") if isinstance(data, dict) else None
        if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
            raise ControllerError("GitHub returned an invalid main ref")
        return sha

    def ref(self, branch: str) -> str:
        encoded = urllib.parse.quote(branch, safe="")
        data = self.api("GET", self.repo_path(f"/git/ref/heads/{encoded}"))
        sha = data.get("object", {}).get("sha") if isinstance(data, dict) else None
        if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
            raise ControllerError("GitHub returned an invalid branch ref")
        return sha

    def commit_tree(self, commit_sha: str) -> str:
        if not SHA_RE.fullmatch(commit_sha):
            raise ControllerError("refusing invalid commit SHA")
        data = self.api("GET", self.repo_path(f"/git/commits/{commit_sha}"))
        tree_sha = data.get("tree", {}).get("sha") if isinstance(data, dict) else None
        if not isinstance(tree_sha, str) or not SHA_RE.fullmatch(tree_sha):
            raise ControllerError("GitHub returned an invalid commit tree")
        return tree_sha

    def create_candidate_commit(self, parent_sha: str, candidate: bytes, message: str) -> str:
        tree_sha = self.commit_tree(parent_sha)
        blob = self.api(
            "POST",
            self.repo_path("/git/blobs"),
            {"content": candidate.decode("utf-8"), "encoding": "utf-8"},
        )
        blob_sha = blob.get("sha") if isinstance(blob, dict) else None
        if not isinstance(blob_sha, str) or not SHA_RE.fullmatch(blob_sha):
            raise ControllerError("GitHub returned an invalid candidate blob SHA")
        tree = self.api(
            "POST",
            self.repo_path("/git/trees"),
            {
                "base_tree": tree_sha,
                "tree": [
                    {
                        "path": CANDIDATE_PATH,
                        "mode": "100644",
                        "type": "blob",
                        "sha": blob_sha,
                    }
                ],
            },
        )
        new_tree = tree.get("sha") if isinstance(tree, dict) else None
        if not isinstance(new_tree, str) or not SHA_RE.fullmatch(new_tree):
            raise ControllerError("GitHub returned an invalid new tree SHA")
        if new_tree == tree_sha:
            raise ControllerError("candidate is identical to the current branch candidate")
        commit = self.api(
            "POST",
            self.repo_path("/git/commits"),
            {"message": message, "tree": new_tree, "parents": [parent_sha]},
        )
        commit_sha = commit.get("sha") if isinstance(commit, dict) else None
        if not isinstance(commit_sha, str) or not SHA_RE.fullmatch(commit_sha):
            raise ControllerError("GitHub returned an invalid candidate commit SHA")
        return commit_sha

    def create_branch(self, branch: str, commit_sha: str) -> None:
        self.api(
            "POST",
            self.repo_path("/git/refs"),
            {"ref": f"refs/heads/{branch}", "sha": commit_sha},
        )

    def update_branch(self, branch: str, commit_sha: str) -> None:
        encoded = urllib.parse.quote(branch, safe="")
        self.api(
            "PATCH",
            self.repo_path(f"/git/refs/heads/{encoded}"),
            {"sha": commit_sha, "force": False},
        )

    def create_pull_request(self, branch: str, title: str, body: str) -> dict[str, Any]:
        data = self.api(
            "POST",
            self.repo_path("/pulls"),
            {"title": title, "head": branch, "base": BASE_BRANCH, "body": body, "draft": False},
        )
        number = data.get("number") if isinstance(data, dict) else None
        html_url = data.get("html_url") if isinstance(data, dict) else None
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise ControllerError("GitHub returned an invalid pull-request number")
        if not isinstance(html_url, str) or not html_url.startswith("https://github.com/"):
            raise ControllerError("GitHub returned an invalid pull-request URL")
        return {"number": number, "html_url": html_url}

    def pull_request(self, number: int) -> dict[str, Any]:
        data = self.api("GET", self.repo_path(f"/pulls/{number}"))
        if not isinstance(data, dict):
            raise ControllerError("GitHub returned an invalid pull request")
        return data

    def update_pull_request_body(self, number: int, body: str) -> None:
        self.api("PATCH", self.repo_path(f"/pulls/{number}"), {"body": body})

    def workflow_runs(self, candidate_sha: str) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode(
            {
                "event": "pull_request_target",
                "head_sha": candidate_sha,
                "per_page": 20,
            }
        )
        workflow = urllib.parse.quote(WORKFLOW_FILE, safe="")
        data = self.api(
            "GET", self.repo_path(f"/actions/workflows/{workflow}/runs?{query}")
        )
        runs = data.get("workflow_runs") if isinstance(data, dict) else None
        if not isinstance(runs, list):
            raise ControllerError("GitHub returned an invalid workflow-run list")
        return [run for run in runs if isinstance(run, dict)]

    def run_artifacts(self, run_id: int) -> list[dict[str, Any]]:
        data = self.api("GET", self.repo_path(f"/actions/runs/{run_id}/artifacts"))
        artifacts = data.get("artifacts") if isinstance(data, dict) else None
        if not isinstance(artifacts, list):
            raise ControllerError("GitHub returned an invalid artifact list")
        return [artifact for artifact in artifacts if isinstance(artifact, dict)]

    def download_artifact(self, artifact_id: int) -> bytes:
        path = self.repo_path(f"/actions/artifacts/{artifact_id}/zip")
        opener = urllib.request.build_opener(_NoRedirect)
        location = None
        for attempt in range(3):
            request = urllib.request.Request(
                f"{API_ROOT}{path}",
                method="GET",
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {self.authentication.token()}",
                    "User-Agent": USER_AGENT,
                    "X-GitHub-Api-Version": API_VERSION,
                },
            )
            try:
                response = opener.open(request, timeout=30)
            except urllib.error.HTTPError as exc:
                if exc.code in (301, 302, 303, 307, 308):
                    location = exc.headers.get("Location")
                    break
                raw = _read_limited(exc, 64_000)
                if exc.code in (500, 502, 503, 504) and attempt < 2:
                    time.sleep(1 + attempt)
                    continue
                raise GitHubError(exc.code, "GET", path, _safe_api_detail(raw)) from exc
            except urllib.error.URLError as exc:
                if attempt < 2:
                    time.sleep(1 + attempt)
                    continue
                raise ControllerError(
                    f"could not reach GitHub artifact endpoint: {exc.reason}"
                ) from exc
            else:
                with response:
                    return _read_limited(response, MAX_ARTIFACT_BYTES)

        if not isinstance(location, str):
            raise ControllerError("GitHub artifact redirect did not include a location")
        parsed = urllib.parse.urlsplit(location)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ControllerError("GitHub returned an unsafe artifact redirect")
        # The signed redirect is intentionally fetched without the GitHub token.
        for attempt in range(3):
            redirected = urllib.request.Request(
                location, method="GET", headers={"User-Agent": USER_AGENT}
            )
            try:
                with urllib.request.urlopen(redirected, timeout=60) as response:
                    return _read_limited(response, MAX_ARTIFACT_BYTES)
            except urllib.error.HTTPError as exc:
                if exc.code in (500, 502, 503, 504) and attempt < 2:
                    time.sleep(1 + attempt)
                    continue
                raise ControllerError(f"artifact download failed with HTTP {exc.code}") from exc
            except urllib.error.URLError as exc:
                if attempt < 2:
                    time.sleep(1 + attempt)
                    continue
                raise ControllerError(f"artifact download failed: {exc.reason}") from exc
        raise ControllerError("artifact download exhausted its verified connection attempts")


class StateStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict[str, Any]:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return {"schema": 1, "active_run": None}
        if len(raw) > 2_000_000:
            raise ControllerError("controller state file is oversized")
        try:
            state_value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ControllerError("controller state file is invalid JSON") from exc
        if not isinstance(state_value, dict) or state_value.get("schema") != 1:
            raise ControllerError("controller state schema is invalid")
        active = state_value.get("active_run")
        if active is not None and not isinstance(active, dict):
            raise ControllerError("controller active-run state is invalid")
        return state_value

    def save(self, state_value: dict[str, Any]) -> None:
        encoded = json.dumps(state_value, ensure_ascii=False, indent=2, sort_keys=True).encode(
            "utf-8"
        )
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".controller-state.", dir=self.path.parent
        )
        try:
            with os.fdopen(descriptor, "wb") as output:
                os.fchmod(output.fileno(), 0o600)
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary_name, self.path)
            directory_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def _zip_entries(archive: bytes) -> tuple[zipfile.ZipFile, dict[str, zipfile.ZipInfo]]:
    try:
        bundle = zipfile.ZipFile(io.BytesIO(archive))
    except zipfile.BadZipFile as exc:
        raise ControllerError("DGX artifact is not a valid ZIP archive") from exc
    entries: dict[str, zipfile.ZipInfo] = {}
    infos = bundle.infolist()
    if len(infos) > 200:
        bundle.close()
        raise ControllerError("DGX artifact contains too many entries")
    for info in infos:
        path = PurePosixPath(info.filename)
        if (
            not info.filename
            or "\\" in info.filename
            or path.is_absolute()
            or ".." in path.parts
            or info.filename in entries
        ):
            bundle.close()
            raise ControllerError("DGX artifact contains an unsafe or duplicate path")
        entries[info.filename] = info
    return bundle, entries


def _zip_text(
    bundle: zipfile.ZipFile,
    entries: dict[str, zipfile.ZipInfo],
    name: str,
    limit: int,
    required: bool = True,
) -> str | None:
    info = entries.get(name)
    if info is None:
        if required:
            raise ControllerError(f"DGX artifact is missing {name}")
        return None
    if info.is_dir() or info.file_size > limit:
        raise ControllerError(f"DGX artifact entry {name} is invalid or oversized")
    with bundle.open(info, "r") as source:
        raw = _read_limited(source, limit)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ControllerError(f"DGX artifact entry {name} is not UTF-8") from exc


CORRECTNESS_HEADER = [
    "case",
    "status",
    "reference",
    "max_abs_error",
    "max_rel_error",
    "mismatches",
    "elements_checked",
    "guard_corruptions",
    "input_mutations",
    "exit_code",
]
PERFORMANCE_HEADER = [
    "case",
    "M",
    "N",
    "K",
    "iterations",
    "candidate_ms",
    "candidate_gflops",
    "cublas_ms",
    "cublas_gflops",
    "ratio_to_cublas",
]


def _parse_csv(text: str, expected_header: list[str], label: str) -> list[dict[str, str]]:
    source = io.StringIO(text, newline="")
    reader = csv.DictReader(source)
    if reader.fieldnames != expected_header:
        raise ControllerError(f"{label} has an unexpected header")
    rows: list[dict[str, str]] = []
    for row in reader:
        if len(rows) >= 1_000 or None in row or any(value is None for value in row.values()):
            raise ControllerError(f"{label} contains an invalid row")
        rows.append(dict(row))
    if not rows:
        raise ControllerError(f"{label} contains no result rows")
    return rows


def parse_artifact(
    archive: bytes,
    repository: str,
    candidate_sha: str,
    workflow_url: str,
    workflow_conclusion: str,
) -> dict[str, Any]:
    bundle, entries = _zip_entries(archive)
    try:
        identity_text = _zip_text(bundle, entries, "identity.txt", 65_536)
        sandbox_text = _zip_text(bundle, entries, "sandbox.txt", 65_536)
        assert identity_text is not None and sandbox_text is not None
        identity = _strict_key_values(identity_text, "identity.txt")
        sandbox = _strict_key_values(sandbox_text, "sandbox.txt")

        if identity.get("candidate_repository") != repository:
            raise ControllerError("DGX artifact candidate repository does not match")
        if identity.get("candidate_sha") != candidate_sha:
            raise ControllerError("DGX artifact candidate SHA does not match")
        verifier_sha = identity.get("verifier_sha", "")
        if not SHA_RE.fullmatch(verifier_sha):
            raise ControllerError("DGX artifact verifier SHA is invalid")

        required_sandbox = {
            "rootless": "true",
            "network": "none",
            "read_only_root": "true",
            "capabilities": "none",
        }
        if any(sandbox.get(key) != value for key, value in required_sandbox.items()):
            raise ControllerError("DGX artifact does not attest the required sandbox properties")
        sandbox_exit = sandbox.get("container_exit_code", "")
        if not re.fullmatch(r"[0-9]{1,3}", sandbox_exit):
            raise ControllerError("DGX artifact contains an invalid sandbox exit code")

        correctness_text = _zip_text(
            bundle, entries, "correctness.csv", 8_388_608, required=False
        )
        base_result: dict[str, Any] = {
            "candidate_sha": candidate_sha,
            "verifier_sha": verifier_sha,
            "workflow_url": workflow_url,
            "workflow_conclusion": workflow_conclusion,
            "sandbox": {
                "rootless": True,
                "network": "none",
                "read_only_root": True,
                "capabilities": "none",
                "container_exit_code": int(sandbox_exit),
            },
        }
        if correctness_text is None:
            diagnostic = _zip_text(
                bundle, entries, "verifier_compile.log", 4_096, required=False
            )
            base_result.update(
                {
                    "status": "build_failed",
                    "correctness": False,
                    "score_geomean_vs_cublas": None,
                    "score_percent_of_cublas": None,
                    "worst_ratio": None,
                    "worst_case": None,
                    "failed_cases": [],
                    "diagnostic": " ".join((diagnostic or "").split())[:2_000],
                }
            )
            return base_result

        correctness_rows = _parse_csv(
            correctness_text, CORRECTNESS_HEADER, "correctness.csv"
        )
        failed_cases = [row["case"] for row in correctness_rows if row["status"] != "PASS"]
        if failed_cases:
            base_result.update(
                {
                    "status": "correctness_failed",
                    "correctness": False,
                    "correctness_cases": len(correctness_rows),
                    "score_geomean_vs_cublas": None,
                    "score_percent_of_cublas": None,
                    "worst_ratio": None,
                    "worst_case": None,
                    "failed_cases": failed_cases[:100],
                }
            )
            return base_result

        if sandbox_exit != "0" or workflow_conclusion != "success":
            raise ControllerError("correct results are inconsistent with workflow or sandbox status")
        performance_text = _zip_text(bundle, entries, "performance.csv", 8_388_608)
        score_text = _zip_text(bundle, entries, "score.txt", 65_536)
        assert performance_text is not None and score_text is not None
        performance_rows = _parse_csv(
            performance_text, PERFORMANCE_HEADER, "performance.csv"
        )
        score = _strict_key_values(score_text, "score.txt")
        try:
            geomean = float(score["score_geomean_vs_cublas"])
            percent = float(score["score_percent_of_cublas"])
            worst = float(score["worst_ratio"])
            cases = int(score["performance_cases"])
            worst_case = score["worst_case"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ControllerError("score.txt contains invalid values") from exc
        if (
            not all(math.isfinite(value) and value >= 0 for value in (geomean, percent, worst))
            or cases != len(performance_rows)
            or not worst_case
            or abs(percent - 100.0 * geomean) > 0.011
        ):
            raise ControllerError("score.txt is internally inconsistent")

        base_result.update(
            {
                "status": "passed",
                "correctness": True,
                "correctness_cases": len(correctness_rows),
                "performance_cases": cases,
                "score_geomean_vs_cublas": geomean,
                "score_percent_of_cublas": percent,
                "worst_ratio": worst,
                "worst_case": worst_case,
                "failed_cases": [],
            }
        )
        return base_result
    finally:
        bundle.close()


def build_pr_body(run: dict[str, Any]) -> str:
    history = run.get("history", [])
    lines = [
        "This PR is managed by the isolated GEMM autoresearch controller.",
        "It may change only `candidate/candidate_gemm.cu` and cannot merge itself.",
        "",
        f"Run ID: `{run['id']}`",
        "",
        "| Iteration | Hypothesis | Correct | Score vs cuBLAS | Worst | Decision |",
        "|---:|---|:---:|---:|---:|---|",
    ]
    for item in history[-50:]:
        correct = "yes" if item.get("correctness") else "no"
        score = item.get("score_geomean_vs_cublas")
        worst = item.get("worst_ratio")
        score_display = f"{score:.6f}" if isinstance(score, (int, float)) else "—"
        worst_display = f"{worst:.6f}" if isinstance(worst, (int, float)) else "—"
        lines.append(
            "| {iteration} | {hypothesis} | {correct} | {score} | {worst} | {decision} |".format(
                iteration=item["iteration"],
                hypothesis=_clean_summary(item["hypothesis"], 160),
                correct=correct,
                score=score_display,
                worst=worst_display,
                decision=_clean_summary(item.get("decision", "rejected"), 40),
            )
        )
    best = run.get("best")
    lines.extend(["", "The trusted workflow result is authoritative; this table is a controller summary."])
    if isinstance(best, dict):
        lines.append(
            f"Current best: iteration {best['iteration']} at "
            f"{best['score_geomean_vs_cublas']:.6f}× cuBLAS."
        )
    return "\n".join(lines)


class Controller:
    def __init__(
        self,
        github: GitHubClient,
        state_store: StateStore,
        max_iterations: int,
    ):
        self.github = github
        self.state_store = state_store
        self.max_iterations = max_iterations

    def dispatch(self, request: Any) -> dict[str, Any]:
        if not isinstance(request, dict) or request.get("version") != PROTOCOL_VERSION:
            raise ControllerError("invalid controller request or protocol version")
        operation = request.get("operation")
        allowed_keys = {
            "status": {"version", "operation"},
            "start": {"version", "operation", "title"},
            "resume": {"version", "operation", "hypothesis", "timeout_seconds"},
            "submit": {
                "version",
                "operation",
                "candidate",
                "hypothesis",
                "timeout_seconds",
            },
        }
        if not isinstance(operation, str) or operation not in allowed_keys:
            raise ControllerError("unsupported controller operation")
        unexpected = set(request) - allowed_keys[operation]
        if unexpected:
            raise ControllerError(f"unexpected request fields: {', '.join(sorted(unexpected))}")
        if operation == "status":
            return self.status()
        if operation == "start":
            return self.start(validate_title(request.get("title")))
        if operation == "resume":
            fallback_hypothesis = request.get("hypothesis")
            if fallback_hypothesis is not None:
                fallback_hypothesis = validate_hypothesis(fallback_hypothesis)
            return self.resume(
                validate_timeout(request.get("timeout_seconds", DEFAULT_WAIT_SECONDS)),
                fallback_hypothesis,
            )
        return self.submit(
            validate_candidate(request.get("candidate")),
            validate_hypothesis(request.get("hypothesis")),
            validate_timeout(request.get("timeout_seconds", DEFAULT_WAIT_SECONDS)),
        )

    def status(self) -> dict[str, Any]:
        state_value = self.state_store.load()
        active = state_value.get("active_run")
        if active is None:
            return {"active_run": None}
        return {
            "active_run": {
                "id": active.get("id"),
                "title": active.get("title"),
                "branch": active.get("branch"),
                "pr_number": active.get("pr_number"),
                "pr_url": active.get("pr_url"),
                "iteration": active.get("iteration", 0),
                "pending_candidate_sha": active.get("pending", {}).get("candidate_sha")
                if isinstance(active.get("pending"), dict)
                else None,
                "best": active.get("best"),
                "last_result": active.get("history", [])[-1]
                if active.get("history")
                else None,
            }
        }

    def start(self, title: str) -> dict[str, Any]:
        state_value = self.state_store.load()
        active = state_value.get("active_run")
        if isinstance(active, dict):
            pr_number = active.get("pr_number")
            if isinstance(pr_number, int):
                pr = self.github.pull_request(pr_number)
                if pr.get("state") == "open":
                    raise ControllerError(
                        f"research run {active.get('id')} still has open PR #{pr_number}"
                    )
        now = time.gmtime()
        run_id = time.strftime("%Y%m%d-%H%M%S", now) + f"-{secrets.token_hex(4)}"
        branch = f"autoresearch/run-{run_id}"
        run = {
            "id": run_id,
            "title": title,
            "branch": branch,
            "created_at": int(time.time()),
            "pr_number": None,
            "pr_url": None,
            "head_sha": None,
            "iteration": 0,
            "history": [],
            "best": None,
        }
        state_value["active_run"] = run
        self.state_store.save(state_value)
        return {"run_id": run_id, "branch": branch, "title": title}

    def _ensure_active_run(self, state_value: dict[str, Any]) -> dict[str, Any]:
        run = state_value.get("active_run")
        if run is None:
            self.start("GEMM autoresearch run")
            state_value.clear()
            state_value.update(self.state_store.load())
            run = state_value.get("active_run")
        if not isinstance(run, dict) or not BRANCH_RE.fullmatch(str(run.get("branch", ""))):
            raise ControllerError("active research-run state is invalid")
        return run

    def _wait_for_run(self, candidate_sha: str, submitted_at: float, timeout: int) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        selected: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            for run in self.github.workflow_runs(candidate_sha):
                if (
                    run.get("event") == "pull_request_target"
                    and run.get("head_sha") == candidate_sha
                    and run.get("path") == WORKFLOW_PATH
                ):
                    created = run.get("created_at")
                    if isinstance(created, str):
                        # A SHA match is already unique; the timestamp guard only
                        # protects against corrupted local state reusing an old SHA.
                        try:
                            created_epoch = calendar.timegm(
                                time.strptime(created, "%Y-%m-%dT%H:%M:%SZ")
                            )
                        except ValueError:
                            continue
                        if created_epoch + 120 < submitted_at:
                            continue
                    selected = run
                    break
            if selected is not None and selected.get("status") == "completed":
                return selected
            time.sleep(POLL_SECONDS)
        raise ControllerError(f"timed out waiting for DGX workflow for {candidate_sha}")

    def _artifact_for_run(self, run_id: int, candidate_sha: str) -> dict[str, Any]:
        expected = f"gemm-results-{candidate_sha}"
        matches = [
            artifact
            for artifact in self.github.run_artifacts(run_id)
            if artifact.get("name") == expected and artifact.get("expired") is False
        ]
        if len(matches) != 1:
            raise ControllerError(
                f"expected exactly one non-expired {expected} artifact, found {len(matches)}"
            )
        artifact_id = matches[0].get("id")
        if isinstance(artifact_id, bool) or not isinstance(artifact_id, int) or artifact_id < 1:
            raise ControllerError("GitHub returned an invalid artifact ID")
        return matches[0]

    def _complete_submission(
        self,
        state_value: dict[str, Any],
        run: dict[str, Any],
        pending: dict[str, Any],
        timeout: int,
    ) -> dict[str, Any]:
        candidate_sha = pending.get("candidate_sha")
        hypothesis = pending.get("hypothesis")
        next_iteration = pending.get("iteration")
        submitted_at = pending.get("submitted_at")
        pr_number = run.get("pr_number")
        if (
            not isinstance(candidate_sha, str)
            or not SHA_RE.fullmatch(candidate_sha)
            or not isinstance(hypothesis, str)
            or isinstance(next_iteration, bool)
            or not isinstance(next_iteration, int)
            or next_iteration < 1
            or not isinstance(submitted_at, (int, float))
            or isinstance(submitted_at, bool)
            or isinstance(pr_number, bool)
            or not isinstance(pr_number, int)
            or pr_number < 1
        ):
            raise ControllerError("pending submission state is invalid")
        if run.get("head_sha") != candidate_sha or run.get("iteration") != next_iteration:
            raise ControllerError("pending submission does not match the active research head")
        if any(
            item.get("candidate_sha") == candidate_sha
            for item in run.get("history", [])
            if isinstance(item, dict)
        ):
            raise ControllerError("active candidate result is already recorded")

        workflow = self._wait_for_run(candidate_sha, float(submitted_at), timeout)
        workflow_run_id = workflow.get("id")
        workflow_url = workflow.get("html_url")
        conclusion = workflow.get("conclusion")
        if (
            isinstance(workflow_run_id, bool)
            or not isinstance(workflow_run_id, int)
            or workflow_run_id < 1
            or not isinstance(workflow_url, str)
            or not workflow_url.startswith("https://github.com/")
            or not isinstance(conclusion, str)
        ):
            raise ControllerError("GitHub returned invalid completed-workflow metadata")

        artifact = self._artifact_for_run(workflow_run_id, candidate_sha)
        archive = self.github.download_artifact(artifact["id"])
        result = parse_artifact(
            archive,
            self.github.repository,
            candidate_sha,
            workflow_url,
            conclusion,
        )

        previous_best = run.get("best")
        score_value = result.get("score_geomean_vs_cublas")
        if result.get("correctness") is True and isinstance(score_value, float):
            if not isinstance(previous_best, dict) or score_value > previous_best.get(
                "score_geomean_vs_cublas", -1.0
            ):
                decision = "new best"
                run["best"] = {
                    "iteration": next_iteration,
                    "candidate_sha": candidate_sha,
                    "score_geomean_vs_cublas": score_value,
                    "worst_ratio": result.get("worst_ratio"),
                }
            else:
                decision = "not improved"
        else:
            decision = "rejected"

        history_item = {
            "iteration": next_iteration,
            "hypothesis": hypothesis,
            "candidate_sha": candidate_sha,
            "verifier_sha": result.get("verifier_sha"),
            "workflow_url": workflow_url,
            "status": result.get("status"),
            "correctness": result.get("correctness"),
            "score_geomean_vs_cublas": result.get("score_geomean_vs_cublas"),
            "worst_ratio": result.get("worst_ratio"),
            "worst_case": result.get("worst_case"),
            "decision": decision,
            "completed_at": int(time.time()),
        }
        run.setdefault("history", []).append(history_item)
        run.pop("pending", None)
        self.state_store.save(state_value)

        body_warning = None
        try:
            self.github.update_pull_request_body(pr_number, build_pr_body(run))
        except ControllerError:
            # The immutable result is already durable locally.  A discussion-
            # body synchronization failure must not turn a valid measurement
            # into an apparently missing result or invite a duplicate commit.
            body_warning = "result recorded, but PR summary synchronization failed"

        response = {
            "run_id": run["id"],
            "iteration": next_iteration,
            "pr_number": pr_number,
            "pr_url": run["pr_url"],
            "decision": decision,
            "previous_best": previous_best,
            "best": run.get("best"),
            **result,
        }
        if body_warning is not None:
            response["warning"] = body_warning
        return response

    def resume(self, timeout: int, fallback_hypothesis: str | None = None) -> dict[str, Any]:
        state_value = self.state_store.load()
        run = self._ensure_active_run(state_value)
        candidate_sha = run.get("head_sha")
        iteration = run.get("iteration")
        pr_number = run.get("pr_number")
        if (
            not isinstance(candidate_sha, str)
            or not SHA_RE.fullmatch(candidate_sha)
            or isinstance(iteration, bool)
            or not isinstance(iteration, int)
            or iteration < 1
            or isinstance(pr_number, bool)
            or not isinstance(pr_number, int)
            or pr_number < 1
        ):
            raise ControllerError("there is no submitted candidate to resume")
        if any(
            item.get("candidate_sha") == candidate_sha
            for item in run.get("history", [])
            if isinstance(item, dict)
        ):
            raise ControllerError("active candidate result is already recorded")

        pr = self.github.pull_request(pr_number)
        if pr.get("state") != "open" or pr.get("head", {}).get("sha") != candidate_sha:
            raise ControllerError("research PR is closed or its head changed outside the controller")
        if self.github.ref(run["branch"]) != candidate_sha:
            raise ControllerError("research branch does not match the pending candidate")

        pending = run.get("pending")
        if not isinstance(pending, dict):
            # Backward-compatible recovery for a submission made by the first
            # controller release, which saved the SHA but not its hypothesis.
            if fallback_hypothesis is None:
                raise ControllerError(
                    "legacy pending submission requires --hypothesis for recovery"
                )
            pending = {
                "candidate_sha": candidate_sha,
                "iteration": iteration,
                "hypothesis": fallback_hypothesis,
                "submitted_at": 0,
            }
            run["pending"] = pending
            self.state_store.save(state_value)
        return self._complete_submission(state_value, run, pending, timeout)

    def submit(self, candidate: bytes, hypothesis: str, timeout: int) -> dict[str, Any]:
        state_value = self.state_store.load()
        run = self._ensure_active_run(state_value)
        if isinstance(run.get("pending"), dict) or (
            isinstance(run.get("head_sha"), str)
            and not any(
                item.get("candidate_sha") == run.get("head_sha")
                for item in run.get("history", [])
                if isinstance(item, dict)
            )
        ):
            raise ControllerError("a submitted candidate is pending; use gemmctl resume")
        iteration = run.get("iteration", 0)
        if isinstance(iteration, bool) or not isinstance(iteration, int) or iteration < 0:
            raise ControllerError("active research iteration is invalid")
        if iteration >= self.max_iterations:
            raise ControllerError(
                f"research run reached its {self.max_iterations}-iteration submission budget"
            )

        pr_number = run.get("pr_number")
        if isinstance(pr_number, int):
            pr = self.github.pull_request(pr_number)
            if pr.get("state") != "open":
                raise ControllerError(f"research PR #{pr_number} is no longer open; start a new run")
            remote_head = pr.get("head", {}).get("sha")
            if remote_head != run.get("head_sha"):
                raise ControllerError("research PR head changed outside the trusted controller")
            parent_sha = self.github.ref(run["branch"])
            if parent_sha != remote_head:
                raise ControllerError("research branch and PR head do not match")
        else:
            parent_sha = self.github.main_ref()

        next_iteration = iteration + 1
        commit_message = (
            f"Experiment {next_iteration:03d}: {_clean_summary(hypothesis, 180)}\n\n"
            f"Controller-Run: {run['id']}\n"
        )
        candidate_sha = self.github.create_candidate_commit(
            parent_sha, candidate, commit_message
        )
        submitted_at = time.time()
        if pr_number is None:
            self.github.create_branch(run["branch"], candidate_sha)
            pending = dict(run)
            pending["iteration"] = next_iteration
            pr = self.github.create_pull_request(
                run["branch"], run["title"], build_pr_body(pending)
            )
            pr_number = pr["number"]
            run["pr_number"] = pr_number
            run["pr_url"] = pr["html_url"]
        else:
            self.github.update_branch(run["branch"], candidate_sha)

        # Persist the immutable submitted SHA before waiting.  A service restart
        # can therefore detect external branch changes instead of overwriting them.
        run["head_sha"] = candidate_sha
        run["iteration"] = next_iteration
        run["pending"] = {
            "candidate_sha": candidate_sha,
            "iteration": next_iteration,
            "hypothesis": hypothesis,
            "submitted_at": submitted_at,
        }
        self.state_store.save(state_value)
        return self._complete_submission(state_value, run, run["pending"], timeout)


def _recv_exact(
    connection: socket.socket, length: int, deadline: float | None = None
) -> bytes:
    chunks = bytearray()
    while len(chunks) < length:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ControllerError("client exceeded the request transmission deadline")
            connection.settimeout(min(remaining, 5.0))
        chunk = connection.recv(length - len(chunks))
        if not chunk:
            raise ControllerError("client disconnected before completing its request")
        chunks.extend(chunk)
    return bytes(chunks)


def recv_frame(connection: socket.socket, limit: int) -> bytes:
    deadline = time.monotonic() + 15
    header = _recv_exact(connection, 4, deadline)
    (length,) = struct.unpack("!I", header)
    if length < 2 or length > limit:
        raise ControllerError("invalid or oversized protocol frame")
    return _recv_exact(connection, length, deadline)


def send_frame(connection: socket.socket, payload: bytes, limit: int) -> None:
    if len(payload) > limit:
        payload = _json_bytes({"ok": False, "error": "controller response exceeded limit"})
    connection.sendall(struct.pack("!I", len(payload)) + payload)


def _positive_integer_env(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ControllerError(f"{name} must be an integer") from exc
    if value < 1 or value > 1_000:
        raise ControllerError(f"{name} must be between 1 and 1000")
    return value


def build_controller() -> Controller:
    try:
        app_id = int(os.environ["GITHUB_APP_ID"])
        installation_id = int(os.environ["GITHUB_INSTALLATION_ID"])
        repository = os.environ["GITHUB_REPOSITORY"]
    except (KeyError, ValueError) as exc:
        raise ControllerError("GitHub App environment is missing or invalid") from exc
    if app_id < 1 or installation_id < 1 or not REPOSITORY_RE.fullmatch(repository):
        raise ControllerError("GitHub App environment contains invalid identifiers")
    key_path = Path(os.environ.get("GITHUB_PRIVATE_KEY_FILE", str(PRIVATE_KEY_PATH)))
    authentication = AppAuthentication(app_id, installation_id, repository, key_path)
    github = GitHubClient(repository, authentication)
    state_store = StateStore(Path(os.environ.get("CONTROLLER_STATE_FILE", str(STATE_PATH))))
    return Controller(
        github,
        state_store,
        _positive_integer_env("MAX_ITERATIONS_PER_RUN", 50),
    )


def serve() -> None:
    controller = build_controller()
    socket_path = Path(os.environ.get("CONTROLLER_SOCKET", str(SOCKET_PATH)))
    socket_path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    if socket_path.exists() or socket_path.is_symlink():
        existing = socket_path.lstat()
        if not stat.S_ISSOCK(existing.st_mode):
            raise ControllerError(f"refusing to replace non-socket path: {socket_path}")
        socket_path.unlink()

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(socket_path))
        os.chmod(socket_path, 0o660)
        server.listen(4)
        while True:
            connection, _ = server.accept()
            with connection:
                connection.settimeout(30)
                try:
                    request = json.loads(recv_frame(connection, MAX_REQUEST_BYTES))
                    result = controller.dispatch(request)
                    response = {"ok": True, "result": result}
                except ControllerError as exc:
                    response = {"ok": False, "error": str(exc)}
                except (json.JSONDecodeError, UnicodeDecodeError):
                    response = {"ok": False, "error": "request is not valid UTF-8 JSON"}
                except Exception:
                    # Do not return tracebacks, paths, environment data, or token
                    # material to the untrusted client.  The traceback stays in
                    # the protected service journal for the administrator.
                    print("unexpected controller error", file=sys.stderr)
                    import traceback

                    traceback.print_exc()
                    response = {"ok": False, "error": "internal controller error"}
                send_frame(connection, _json_bytes(response), MAX_RESPONSE_BYTES)
    finally:
        server.close()
        try:
            socket_path.unlink()
        except FileNotFoundError:
            pass


def main() -> int:
    if sys.argv != [sys.argv[0], "serve"]:
        print(f"usage: {sys.argv[0]} serve", file=sys.stderr)
        return 2
    try:
        serve()
    except ControllerError as exc:
        print(f"controller startup failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
