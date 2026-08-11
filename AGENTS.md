# Autonomous GEMM researcher instructions

Your sole editable competition artifact is:

```text
candidate/candidate_gemm.cu
```

Treat every other repository file as read-only evidence. Never modify or try to
bypass the ABI, verifier, benchmark, case manifests, workflow, controller,
score parser, sandbox, or these instructions. Never use `git push`, GitHub
credentials, SSH credentials, or a generic GitHub API client.

Use `/usr/local/bin/gemmctl` as the only submission and measurement interface.
The controller, not you, creates commits and PR updates. If a submission was
created but result collection was interrupted, run `gemmctl resume`; do not
create a duplicate candidate commit.

Do not execute candidate GPU code locally. Do not call cuBLAS, cuBLASLt,
CUTLASS GEMM, or another prebuilt matrix multiplication from the candidate.
Do not special-case published seeds, matrix values, or case names. Optimize the
general contiguous row-major FP32 contract for independent power-of-two M, N,
and K values from 16 through 4096.

For every experiment, state one concrete optimization hypothesis before
editing. A result is eligible only when every correctness case passes. Compare
both geometric-mean and worst-case ratios, and clearly report whether the
candidate became the controller's new best.
