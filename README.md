# Power-of-Two FP32 GEMM Autoresearch Competition

## Objective

Implement the fastest general-purpose contiguous row-major FP32 GEMM on the
NVIDIA GB10 (`sm_121`):

```text
C = alpha * A * B + beta * C
```

`M`, `N`, and `K` are independent powers of two in the inclusive range
`[16, 4096]`. Candidates must support every in-contract shape, not only square
or benchmarked shapes.

## Candidate boundary

Autoresearch agents may modify only:

```text
candidate/candidate_gemm.cu
```

The function declared in `include/candidate_api.h` is the stable ABI. The
candidate may use custom CUDA kernels and CUDA runtime APIs, but may not invoke
cuBLAS, cuBLASLt, CUTLASS GEMM, or another prebuilt matrix-multiplication
implementation. Tensor Cores and candidate-authored helper kernels are allowed.

Allocations, compilation, and host/device copies are outside the timed region.
Any candidate-side allocation or dispatch overhead inside
`launch_candidate_gemm` is included in its measurement. Temporary workspace is
currently limited implicitly by available device memory; a fixed limit should
be added before admitting third-party competitors.

## Correctness gate

Every case must pass before performance is measured. The trusted verifier:

- uses deterministic random FP32 values in `[-1, 1]`;
- uses scalar CPU GEMM for smaller cases and pedantic cuBLAS for large cases;
- checks every output element with `abs_tol=1e-2` and `rel_tol=1e-4`;
- validates nontrivial `alpha` and `beta`;
- detects mutations of A or B;
- detects writes into guard regions around A, B, and C;
- runs each case in a separate process with a timeout.

Any mismatch, CUDA error, timeout, input mutation, or guard corruption rejects
the candidate. There are no XFAIL cases inside the published contract.

## Performance score

Each performance case is measured against default cuBLAS in the same process.
The final score is the geometric mean of:

```text
candidate_GFLOP/s / cuBLAS_GFLOP/s
```

A score of `0.70` means that the candidate achieves 70% of cuBLAS performance
on average across the suite. Correctness failure gives no score.

## Running on the DGX Spark

```bash
cd <gemm-autoresearch-repository>
./trusted/run_competition.sh
```

Results are written under `results/<UTC timestamp>/`.

## Verifier integrity

The repository copy is reviewable documentation, not the final trust boundary.
For CI, install `trusted/`, `include/candidate_api.h`, and the private hidden
case manifest into a separate root-owned or verifier-owned directory on the
DGX. CI must execute that trusted copy against the PR candidate. Protect the
workflow and verifier paths with CODEOWNERS/rulesets, and require the trusted
check before merging.

The active workflow in `.github/workflows/gemm-autoresearch.yml` checks out
verifier code from the PR base commit rather than the candidate commit.

## Autonomous controller

The optional trusted broker under `controller/` lets an unprivileged research
agent submit candidate bytes without receiving GitHub credentials. It creates
or advances one `autoresearch/run-*` branch, opens one PR, waits for the exact
candidate-SHA workflow run, validates the bounded DGX artifact, and records the
best correct score. See `controller/README.md` for installation and use.

The broker exposes no generic GitHub operation and no merge action. Its source
is owner-protected, while the PR workflow continues to reject every changed
path except `candidate/candidate_gemm.cu`.

## Autonomous iteration and GPU exclusivity

`ops/install-step6.sh` creates a root-owned host GPU lease shared with the
dedicated Actions runner. Every trusted measurement holds that lease and
checks three times that no other CUDA compute process exists before starting the
sandbox. A contaminated or uncoordinated GPU fails closed instead of producing
a score.

The optional `gemm-autoresearch-agent.service` runs one Codex experiment as the
unprivileged `gemm-agent` account. It has a read-only host, write access only to
its Codex state, run logs, and candidate directory, and a private `/dev` with
no NVIDIA devices. The agent can reach the narrow controller socket but still
cannot access GitHub credentials or execute candidate code on the host GPU.
Its root-owned Codex permission profile also denies model-generated commands
read access outside the minimal runtime and trusted workspace, including the
Codex authentication cache, and denies every network destination except the
controller's exact Unix socket. The only additional read grant is the Codex
standalone release directory, which bubblewrap needs to re-execute the sandbox
launcher; the rest of `.codex`, including `auth.json`, remains denied. The host
wrapper accepts the installer's launcher symlink only when its canonical target
is a regular executable under that release directory, and systemd mounts the
release subtree read-only during a run.

Codex 0.147 routes command execution through its V8-backed code-mode host, so
the service permits executable JIT memory. The surrounding isolation remains:
the agent is unprivileged and its bubblewrap/seccomp profile still blocks the
internet, GPU, credential cache, and writes outside the candidate directory.
The outer service exposes the full `/proc` API because bubblewrap needs the
kernel overflow UID/GID values, while `ProtectKernelTunables=true` keeps kernel
tunables read-only and `ProtectProc=invisible` hides other users' processes.
After Codex exits, the root-installed wrapper also compares trusted controller
state from before and after the run. The unit fails unless a new, completed
experiment result was recorded, even if Codex itself exits zero.

Before each Codex session, the wrapper atomically restores
`candidate/candidate_gemm.cu` from the exact commit recorded as the active
run's best accepted result, falling back to the exact protected `main` commit
when no result has been accepted yet. Incorrect or slower experiments remain
auditable in the PR history but do not become the baseline for the following
iteration.

For bounded continuous research, the root-owned
`gemm-autoresearch-batch@.service` sequentially starts the same isolated
one-iteration service and fails closed on the first missing or failed trusted
result. The instance name specifies the number of additional iterations:

```bash
sudo systemctl start --no-block gemm-autoresearch-batch@10.service
```

Counts are limited to 1–50 and may not exceed the controller's remaining
per-run submission budget. A global lock prevents overlapping batches. See
[`docs/CONTINUOUS_AUTORESEARCH.md`](docs/CONTINUOUS_AUTORESEARCH.md) for
monitoring, stop-after-current, failure recovery, and isolation details.

On Ubuntu 24.04 with
`kernel.apparmor_restrict_unprivileged_userns=1`, install the Ubuntu
`apparmor-profiles` package and enable its disabled-by-default
`bwrap-userns-restrict` profile before starting the agent. This allows bwrap to
set up the sandbox namespace while stripping capabilities from the command it
launches. Do not disable the host-wide user-namespace restriction.

For the complete systemd/Codex failure timeline, explanations of each fix,
retained security boundaries, and future diagnostic commands, see
[`docs/STEP6_SYSTEMD_TROUBLESHOOTING.md`](docs/STEP6_SYSTEMD_TROUBLESHOOTING.md).
