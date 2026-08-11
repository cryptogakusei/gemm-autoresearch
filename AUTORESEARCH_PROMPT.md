# Autoresearch Agent Contract

Your objective is to maximize the competition score while preserving complete
correctness for every published power-of-two input.

You may modify only:

```text
candidate/candidate_gemm.cu
```

Do not modify, replace, delete, shadow, or bypass the candidate ABI, verifier,
case manifests, benchmark, scoring scripts, workflow, or baseline results.

For each experiment:

1. State one optimization hypothesis.
2. Change the candidate implementation.
3. Submit it with `gemmctl submit candidate/candidate_gemm.cu --hypothesis
   "..."`. The trusted controller creates the commit and updates the PR.
4. Wait for the command to return the trusted DGX result. If result collection
   is interrupted after submission, use `gemmctl resume`; do not resubmit.
5. Reject any candidate with a correctness failure.
6. Compare the geometric-mean and worst-case scores with the previous commit.
7. Keep the change only when it improves the declared objective.
8. Record the hypothesis, result, and decision in the PR discussion.

Do not call cuBLAS, cuBLASLt, CUTLASS GEMM, or another prebuilt GEMM from the
candidate. Do not special-case published matrix values, seeds, or case names.
Do not use `git push`, a GitHub token, or a GitHub SSH key. The research account
must not possess any of them.
