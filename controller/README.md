# Trusted autoresearch controller

The controller is the only component allowed to hold the repository-scoped
GitHub App private key. An untrusted research process runs as `gemm-agent` and
can communicate through `/run/gemm-autoresearch/controller.sock` only.

The socket protocol deliberately offers four operations:

- `gemmctl start --title TITLE` starts local state for one research PR;
- `gemmctl submit FILE --hypothesis TEXT` submits the bytes of `FILE` at the
  hard-coded `candidate/candidate_gemm.cu` path and waits for the DGX result;
- `gemmctl resume` resumes result collection for the exact already-submitted
  SHA after a controller restart or transient API failure;
- `gemmctl status` reports the last and best accepted measurements.

There is no generic GitHub API proxy and no operation to choose a repository,
path, workflow, base branch, ref namespace, or merge action. Installation
tokens are minted in memory, expire after one hour, and are down-scoped to this
repository with Actions read, Contents write, and Pull requests write.

## Install on the DGX Spark

The existing protected files must already be present:

```text
/etc/gemm-autoresearch/controller.env
/etc/gemm-autoresearch/github-app.pem
```

From a trusted checkout of protected `main`:

```bash
sudo ./controller/install-controller.sh
sudo -u gemm-agent /usr/local/bin/gemmctl status
```

The installer creates a non-login `gemm-agent` account and a `gemm-submit`
socket group. The systemd service runs as `gemm-control`; `gemm-agent` cannot
read the App key, controller state, or installation token.

## One iteration

```bash
sudo -u gemm-agent gemmctl submit candidate/candidate_gemm.cu \
  --hypothesis "Use a 32x32 shared-memory tile"
```

The JSON response includes correctness, score, worst case, candidate and
verifier SHAs, PR URL, workflow URL, and whether the result became the new
best. A correctness failure never receives or replaces a score.

The controller never merges. Close or merge the current PR as a human before
starting another run.
