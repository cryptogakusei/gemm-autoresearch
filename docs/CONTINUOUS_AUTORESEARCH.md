# Controlled continuous autoresearch

The continuous runner is a bounded systemd template layered over the existing
one-iteration agent service. It does not turn the Codex agent into a daemon and
does not broaden its permissions. A batch asks systemd to start the same
isolated one-shot unit repeatedly, waiting for a new trusted controller result
after every attempt.

## Install

From the trusted DGX checkout:

```bash
cd ~/gemm-step5-controller-install
git pull --ff-only
sudo ./ops/install-step6.sh
```

The installer adds these root-owned files:

```text
/etc/systemd/system/gemm-autoresearch-batch@.service
/usr/local/libexec/gemm-autoresearch/run_autoresearch_batch.py
```

## Start a bounded batch

Put the number of **additional** experiments in the systemd instance name. For
example, this starts ten sequential experiments and returns immediately to the
terminal:

```bash
sudo systemctl start --no-block gemm-autoresearch-batch@10.service
```

The accepted count is a canonical integer from 1 through 50. Values such as
`0`, `01`, `51`, negative numbers, or arbitrary strings fail before any agent
iteration starts.

The controller also reports its live per-run submission budget. If the active
run has only three submissions remaining, `@4` fails before starting, while
`@3` is accepted.

## Monitor progress

For a ten-iteration batch:

```bash
systemctl status gemm-autoresearch-batch@10.service --no-pager -l

sudo journalctl -u gemm-autoresearch-batch@10.service -f
```

The batch remains `activating (start)` while iterations are in progress because
it is a systemd `Type=oneshot` job. On successful completion it becomes
`inactive (dead)` with `Result=success`.

Each completed iteration writes a bounded journal line containing:

- progress within the requested batch;
- the controller iteration number;
- the decision (`new best`, `not improved`, or `rejected`);
- the attempted score; and
- the current best score.

The underlying experiment remains visible separately:

```bash
systemctl status gemm-autoresearch-agent.service --no-pager -l
sudo journalctl -u gemm-autoresearch-agent.service -n 100 --no-pager
```

## Stop after the current iteration

Send `SIGUSR1` to only the batch scheduler's main process:

```bash
sudo systemctl kill \
  --kill-who=main \
  --signal=SIGUSR1 \
  gemm-autoresearch-batch@10.service
```

The currently running Codex/verifier iteration continues. After its trusted
result is recorded, the scheduler exits successfully without starting another
iteration.

Use the same instance count that was used to start the batch. To discover an
active batch:

```bash
systemctl list-units 'gemm-autoresearch-batch@*.service'
```

A normal `systemctl stop` terminates the scheduler immediately. The already
started agent unit belongs to its own systemd cgroup and is not deliberately
killed by the batch scheduler, but `SIGUSR1` is preferred because it records the
completed iteration before the batch exits.

## Failure behavior

The scheduler stops immediately and the batch unit fails if:

- the requested count is invalid or exceeds the controller's remaining budget;
- another batch holds the global batch lock;
- an uncollected candidate is already pending;
- the one-iteration agent is unexpectedly active before the batch starts;
- any agent service invocation fails;
- the controller result does not change after an agent invocation; or
- controller status is malformed or unreachable.

It never skips a failed iteration and continues blindly. Diagnose the first
failure before launching another batch.

If result collection was interrupted and the controller reports a pending SHA,
recover it first:

```bash
sudo -u gemm-agent -H /usr/local/bin/gemmctl status
sudo -u gemm-agent -H /usr/local/bin/gemmctl resume
```

Then start a new bounded batch.

## Best-candidate behavior

Before every child iteration, the trusted one-iteration launcher restores the
candidate from the exact commit recorded as the controller's current best. If
no candidate has passed yet, it restores the protected `main` candidate.

Therefore the sequence is:

```text
restore current best
  -> run one isolated Codex experiment
  -> verify correctness and benchmark on the DGX
  -> record result
  -> promote only if better
  -> restore current best for the next iteration
```

An incorrect or slower candidate stays in PR/controller history for audit, but
does not seed the following experiment.

## Isolation and concurrency

The batch scheduler itself:

- runs root-owned code with no Linux capabilities;
- has no IP networking and can use only Unix sockets;
- has a private device view and no GPU access;
- has a strict read-only filesystem except for its exact batch lock path;
- cannot read the controller secret directory or the agent's Codex/workspace
  directory;
- has a root-owned implementation that invokes only the fixed controller-status
  and one-iteration systemd commands; and
- holds `/run/lock/gemm-autoresearch/batch.lock` for the complete batch.

The global lock prevents `batch@5` and `batch@10` from running concurrently.
The existing agent unit, controller socket, GitHub App isolation, candidate-only
write policy, verifier protection, workflow sandbox, and GPU lease remain
unchanged.

## Examples

Run one additional iteration through the batch layer:

```bash
sudo systemctl start --no-block gemm-autoresearch-batch@1.service
```

Run twenty additional iterations:

```bash
sudo systemctl start --no-block gemm-autoresearch-batch@20.service
```

Inspect the controller's remaining budget before choosing a count:

```bash
sudo -u gemm-agent -H /usr/local/bin/gemmctl status
```

Look for:

```json
{
  "max_iterations_per_run": 50,
  "remaining_iterations": 48
}
```

The exact remaining value depends on how many candidates have already been
submitted in the active research PR.
