Perform exactly one autonomous GEMM optimization experiment.

Read `AGENTS.md`, `README.md`, `AUTORESEARCH_PROMPT.md`, the candidate ABI,
published case manifests, benchmark implementation, and current candidate.
Run `gemmctl status` to inspect controller history. Attempt
`gemmctl start --title "Autonomous general-purpose GEMM research"`; if it says
an existing research PR is still open, continue that existing run.

Choose one technically justified optimization that should improve the
geometric-mean score across the varied power-of-two shapes without sacrificing
the general contract. Edit only `candidate/candidate_gemm.cu`. Do not run the
GPU verifier or benchmark locally, do not use any prebuilt GEMM, and do not use
network or GitHub commands.

Submit exactly once with:

```text
gemmctl submit candidate/candidate_gemm.cu --hypothesis "YOUR HYPOTHESIS"
```

The command may take several minutes. If it reports that this exact candidate
is pending because result collection was interrupted, use `gemmctl resume`
instead of submitting again. Do not attempt a second optimization in this run,
even if the candidate fails.

In your final response, report the hypothesis, key implementation change,
candidate SHA, verifier SHA, correctness result, geometric-mean ratio, worst
ratio/case, controller decision, PR URL, and workflow URL. Never claim success
without the structured `gemmctl` result.
