# Step 6 systemd and Codex sandbox troubleshooting history

This document records the failures encountered while bringing up the isolated
Codex autoresearch service on the DGX Spark, how each failure was diagnosed,
which fix was applied, and which security boundaries remain in place. It is
both a historical record and a runbook for diagnosing similar failures after
future Codex, Ubuntu, systemd, or AppArmor upgrades.

The successful end-to-end run described here used:

- DGX Spark host `spark-4959`, Ubuntu 24.04 on AArch64;
- Codex CLI 0.147.0;
- `gemm-autoresearch-agent.service` as a systemd `Type=oneshot` unit;
- the unprivileged `gemm-agent` Linux account;
- the trusted controller at `/run/gemm-autoresearch/controller.sock`;
- the separately isolated `dgx-ci` GitHub Actions runner account; and
- the trusted DGX workflow in `.github/workflows/gemm-autoresearch.yml`.

No GitHub token, App private key, ChatGPT credential, or sudo credential is
included in this document.

## 1. The important distinction: systemd reported the failures, but did not always cause them

The complete execution path is nested:

```text
systemd
  `- root-installed run_codex_trial.sh wrapper
       `- Codex CLI
            `- Codex code-mode host (V8)
                 `- Codex bubblewrap/seccomp command sandbox
                      `- gemmctl
                           `- trusted controller Unix socket
                                `- GitHub PR and trusted DGX workflow
```

`systemctl status` reports the final status of the whole unit. A lower layer
can therefore make the unit fail even when systemd itself started the process
correctly. For example, a bubblewrap initialization error prevents `gemmctl`
from running, which prevents a new trusted result, which makes the wrapper
return exit code 1, which systemd reports as `failed`.

The final wrapper intentionally fails closed. Before starting Codex it records
the current controller result. After Codex exits, it records the controller
result again. The service succeeds only if the second snapshot contains a new,
completed result signature. A friendly Codex final message or Codex exit code
0 is not enough.

This behavior explains the recurring journal message:

```text
Codex exited without a new trusted controller result
```

That message is a **result-guard decision**, not the underlying cause. The
underlying cause must be found in the run's `final.txt`, `stderr.log`, and
`events.jsonl`.

Relevant implementation:

- `agent/gemm-autoresearch-agent.service`
- `agent/run_codex_trial.sh`
- `agent/codex-config.toml`

## 2. How to interpret this one-shot service

The unit uses `Type=oneshot`. Its states mean:

| systemd state | Meaning for this unit |
|---|---|
| `activating (start)` | The Codex experiment is still running. This is normal, even for several minutes. |
| `inactive (dead)`, `Result=success` | The one experiment completed successfully and the process exited. This is the expected terminal state; it is not supposed to remain running. |
| `failed`, `Result=exit-code` | The wrapper returned nonzero. Inspect the journal and the protected run directory. |
| `Unit ... not loaded` | systemd does not currently know the unit file. Install it and run `systemctl daemon-reload`; `reset-failed` cannot load a missing unit. |

Useful read-only checks:

```bash
systemctl show gemm-autoresearch-agent.service \
  -p ActiveState -p SubState -p Result -p ExecMainStatus \
  -p ExecMainStartTimestamp -p ExecMainExitTimestamp --no-pager

systemctl status gemm-autoresearch-agent.service --no-pager -l

journalctl -u gemm-autoresearch-agent.service -n 100 --no-pager
```

`systemctl reset-failed` only clears systemd's remembered failed state and
restart counters. It does not reinstall the unit, change its sandbox, or start
a new experiment.

## 3. Failure timeline and fixes

### 3.1 `Unit gemm-autoresearch-agent.service not loaded`

Observed while running:

```text
Failed to reset failed state of unit gemm-autoresearch-agent.service:
Unit gemm-autoresearch-agent.service not loaded.
```

#### Cause

The agent unit had not yet been installed into `/etc/systemd/system`, or
systemd had not reloaded unit files after it was installed. This was not a
Codex runtime failure.

#### Fix

Run the Step 6 installer, which copies the unit and performs a daemon reload:

```bash
cd ~/gemm-step5-controller-install
sudo ./ops/install-step6.sh
```

The relevant installer action is equivalent to:

```bash
sudo systemctl daemon-reload
```

After the unit is loaded, `reset-failed` and `start` are meaningful:

```bash
sudo systemctl reset-failed gemm-autoresearch-agent.service
sudo systemctl start --no-block gemm-autoresearch-agent.service
```

### 3.2 Wrapper rejected the installed Codex launcher

Observed in the journal:

```text
missing or unsafe agent input: /var/lib/gemm-agent/.local/bin/codex
```

#### Cause

The standalone Codex installer creates
`/var/lib/gemm-agent/.local/bin/codex` as a symlink into a versioned release
directory. The original wrapper's generic input check rejected symlinks. The
check was intentionally strict, but did not match the official standalone
installer layout.

#### Fix

Commit `3739d97` changed the wrapper to accept only this specific structure:

```text
/var/lib/gemm-agent/.local/bin/codex
  -> /var/lib/gemm-agent/.codex/packages/standalone/releases/<version>/bin/codex
```

The wrapper now resolves the link with `readlink -e` and verifies that:

1. the launcher itself is a symlink;
2. the canonical target stays under the expected standalone release path;
3. the target is a regular executable file; and
4. systemd exposes the release subtree read-only.

This preserved the safety goal: replacing the symlink with a target elsewhere
does not pass validation.

### 3.3 Bubblewrap could not create/configure its network namespace

Observed during the sandbox preflight:

```text
bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted
```

#### Cause

Ubuntu had restricted unprivileged user namespaces enabled:

```text
kernel.apparmor_restrict_unprivileged_userns=1
```

Codex's Linux command sandbox uses bubblewrap. Bubblewrap needed permitted user,
mount, PID, IPC, UTS, and network namespaces, plus Ubuntu's AppArmor mediation
profile. The original service used `RestrictNamespaces=true`, which prohibited
all namespace creation, and the required Ubuntu bubblewrap AppArmor profile was
not yet active.

#### Fix

The Ubuntu `apparmor-profiles` package was installed and its restricted
bubblewrap profile was enabled. `aa-status` then showed the `bwrap` and
`unpriv_bwrap` profiles.

The systemd unit was also changed in commit `5d59344` from a blanket namespace
ban to a narrow allowlist:

```ini
RestrictNamespaces=user mnt pid ipc uts net
```

The cgroup namespace remains prohibited because it is omitted from the
allowlist. The host-wide unprivileged-user-namespace restriction was **not**
disabled.

### 3.4 Bubblewrap could not re-execute the versioned Codex binary

Observed after the namespace issue was fixed:

```text
bwrap: execvp /var/lib/gemm-agent/.codex/packages/standalone/releases/
0.147.0-aarch64-unknown-linux-musl/bin/codex: No such file or directory
```

The line is wrapped here for readability; it was one path in the original
error.

#### Cause

The executable existed on the host, but Codex's inner filesystem permission
profile did not expose its versioned release directory. Inside the bubblewrap
mount namespace, an inaccessible file often appears as `No such file or
directory`, even when it is present outside the sandbox.

#### Fix

Commit `5d59344` added a read grant for only the standalone releases subtree:

```toml
"/var/lib/gemm-agent/.codex/packages/standalone/releases" = "read"
```

Commit `3739d97` also added the outer systemd read-only mount:

```ini
ReadOnlyPaths=/var/lib/gemm-agent/.codex/packages/standalone/releases
```

The rest of `.codex`, including `auth.json`, remains unavailable to
model-generated commands.

### 3.5 The sandbox could not reach the trusted controller socket

Observed during preflight:

```json
{
  "ok": false,
  "error": "cannot connect to trusted controller at
  /run/gemm-autoresearch/controller.sock: [Errno 1] Operation not permitted"
}
```

#### Cause

The Codex permission profile had networking completely disabled. In this
profile system, the Unix-socket allowlist is enforced by the sandbox network
machinery. Setting `enabled=false` disabled the machinery needed to proxy the
explicitly allowed local socket as well as blocking internet access.

#### Fix

Commit `9b963d0` enabled the sandbox network layer but supplied no domain or TCP
allowlist entries:

```toml
[permissions.gemm-autoresearch.network]
enabled = true

[permissions.gemm-autoresearch.network.unix_sockets]
"/run/gemm-autoresearch/controller.sock" = "allow"
```

This does **not** give the agent general internet access. The exact controller
Unix socket is allowed; HTTP/TCP destinations remain unlisted and blocked.

The successful preflight after this change was:

```text
PASS: candidate writable; auth blocked; internet blocked; controller reachable
```

### 3.6 Code-mode host crashed with `SIGTRAP`

Observed in a protected run's `stderr.log`:

```text
code-mode host closed its stdout
code-mode host exited with status signal: 5 (SIGTRAP) (core dumped)
```

No AppArmor, kernel, or seccomp denial accompanied the crash.

#### Cause

The code-mode helper bundled with Codex 0.147.0 contains V8 and WebAssembly
runtime components. The systemd unit had:

```ini
MemoryDenyWriteExecute=true
```

V8 requires executable JIT memory. Based on the repeated `SIGTRAP`, absence of
other denials, and inspection of the bundled helper, we inferred that systemd's
W^X restriction prevented the V8 helper from initializing correctly.

This was a diagnosis from the observed binary/runtime behavior, not a generic
claim that every future Codex version will use the same implementation.

#### First attempted fix: disable code mode

Commit `afa65c8` tried to retain `MemoryDenyWriteExecute=true` by setting:

```toml
code_mode_host = false
unified_exec = true
```

This attempt was intentionally conservative, but Codex 0.147.0 did not provide
a working fallback for this autonomous execution path.

### 3.7 Disabling code mode produced `code-mode host is disabled`

Observed in the next run:

```text
code-mode host is disabled
```

Every repository read and `gemmctl status` attempt was rejected before command
execution.

#### Cause

Codex 0.147.0 routed these model-generated command calls through the code-mode
host. Enabling the unified-exec feature did not replace that route.

#### Final fix

Commit `012b84b` restored the required host:

```toml
code_mode_host = true
```

and changed the outer service setting to:

```ini
MemoryDenyWriteExecute=false
```

This relaxes one systemd hardening directive for the unprivileged agent service
so V8 can allocate JIT memory. It does not grant a Linux capability, device,
filesystem path, network destination, or credential. The inner Codex
bubblewrap/seccomp sandbox and the other outer systemd restrictions remain.

### 3.8 Bubblewrap could not read `overflowuid`

Observed in the next run's final message:

```text
bwrap: Can't read /proc/sys/kernel/overflowuid: No such file or directory
```

#### Cause

The outer service used:

```ini
ProcSubset=pid
```

That setting hides non-process portions of `/proc`, including `/proc/sys`.
Bubblewrap reads the kernel overflow UID/GID values while constructing its user
namespace. Because the path was removed from the service's view, bubblewrap
received `ENOENT` (`No such file or directory`) rather than a permission error.

#### Fix

Commit `01ee159` changed the setting to:

```ini
ProcSubset=all
```

The following protections remain active:

```ini
ProtectKernelTunables=true
ProtectProc=invisible
```

Consequently, `/proc/sys/kernel/overflowuid` and `overflowgid` are visible to
bubblewrap, kernel tunables stay read-only, and processes belonging to other
users remain hidden. This is visibility required for sandbox initialization,
not permission to change kernel settings.

### 3.9 Generic result-guard failures during the above attempts

Several failed launches ended with systemd output like:

```text
gemm-autoresearch-agent.service: Main process exited, code=exited,
status=1/FAILURE
gemm-autoresearch-agent.service: Failed with result 'exit-code'.
```

and wrapper output:

```text
Codex exited without a new trusted controller result
```

These were expected secondary failures. The guard compared:

- active run ID;
- iteration number;
- candidate SHA; and
- result completion timestamp.

If the signature did not change, it converted the run to a failure even when
Codex itself exited 0. This prevented infrastructure errors from being recorded
as successful research iterations.

## 4. Related failures that were not systemd sandbox failures

These occurred during the same installation but should not be conflated with
the systemd issues above.

### 4.1 GitHub API SSL certificate verification

Observed from `gemmctl submit` or `gemmctl resume`:

```text
[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed:
self-signed certificate
```

This came from the trusted controller's Python HTTPS path to GitHub, not from
the agent unit's bubblewrap sandbox. Verifying
`urllib.request.urlopen("https://api.github.com/meta")` as the
`gemm-control` service account returned HTTP 200 after the service's trust
environment was corrected and the controller was restarted.

### 4.2 Tailscale/SSH connection drops

Examples included:

```text
Read from remote host ...: Connection reset by peer
client_loop: send disconnect: Broken pipe
```

These interrupted the administrator's SSH session, not the systemd services.
The Docker pull resumed successfully, and a running systemd experiment
continued after an SSH monitoring connection stalled.

### 4.3 `darkbloom` could not read protected run logs

The run directory is mode 0700 and owned by `gemm-agent`. A normal login saw
`Permission denied`. This is intentional credential/result isolation, not a
service fault. An administrator can inspect a specific known run with sudo:

```bash
sudo sh -c '
run=/var/lib/gemm-agent/runs/<run-id>
cat "$run/exit-code.txt"
cat "$run/final.txt"
sed -n "1,240p" "$run/stderr.log"
cat "$run/controller-status.json"
'
```

Resolve the exact run directory from the journal first. Do not broadly change
ownership or permissions on `/var/lib/gemm-agent` just to read logs.

## 5. Final working compatibility settings

The settings that were essential for Codex 0.147.0 on this host are:

```ini
# Allow the namespace types bubblewrap needs; cgroup is still omitted.
RestrictNamespaces=user mnt pid ipc uts net

# Let bubblewrap read required /proc/sys UID/GID values.
ProcSubset=all
ProtectKernelTunables=true
ProtectProc=invisible

# Let the V8-backed code-mode helper use JIT memory.
MemoryDenyWriteExecute=false

# Make the verified standalone release visible but immutable.
ReadOnlyPaths=/var/lib/gemm-agent/.codex/packages/standalone/releases
```

```toml
[features]
code_mode_host = true

[permissions.gemm-autoresearch.network]
enabled = true

[permissions.gemm-autoresearch.network.unix_sockets]
"/run/gemm-autoresearch/controller.sock" = "allow"
```

These are compatibility settings, not the whole security policy. The complete
unit and profile are authoritative.

## 6. Security properties retained after all fixes

The final service still has these outer systemd controls:

- unprivileged `User=gemm-agent` and `Group=gemm-agent`;
- an empty capability bounding set and no ambient capabilities;
- `NoNewPrivileges=true`;
- private devices and `DevicePolicy=closed`, so no NVIDIA GPU device;
- strict filesystem protection;
- explicit write access only for Codex state, run logs, and the candidate
  directory;
- `/etc/gemm-autoresearch` inaccessible, protecting controller secrets;
- kernel logs, modules, control groups, hostname, clock, realtime scheduling,
  SUID/SGID transitions, and non-native syscall architectures restricted;
- task, memory, file-descriptor, and four-hour runtime limits.

The root-owned Codex permission profile additionally retains:

- read-only repository access except `candidate/`, which is writable;
- denial of `/tmp`, the general filesystem root, and the Codex auth cache;
- disabled web search, apps, remote plugins, memories, hooks, and multi-agent
  behavior;
- no allowed internet domain;
- only the exact trusted controller Unix socket allowed.

The controller and CI retain separate trust boundaries:

- the agent receives no GitHub credential or GitHub API capability;
- `gemmctl` exposes only narrow research operations;
- the controller has no merge operation;
- a PR may change only `candidate/candidate_gemm.cu`;
- the verifier and workflow come from the protected base commit;
- untrusted candidate code runs only inside the pinned isolated CUDA workflow;
- the Actions runner and verifier coordinate through the trusted GPU lease;
- correctness must pass before performance is accepted;
- a human must review and merge a winning PR.

## 7. Successful end-to-end verification

After commit `01ee159` was installed, the unit ran from 06:56:42 to 07:00:25
PDT on 2026-08-12 and ended as:

```text
Result=success
ActiveState=inactive
SubState=dead
```

The wrapper accepted the run because the trusted controller recorded a new
completed result. The autonomous agent created PR #4, and the trusted workflow
completed successfully.

Recorded result:

- correctness: passed;
- score: 0.416775x cuBLAS;
- previous baseline: 0.195291x cuBLAS;
- improvement over the baseline: approximately 2.13x;
- worst-case ratio: 0.250661x cuBLAS;
- decision: `new best`.

This final success verified the entire path rather than only service startup:

```text
Codex planning and candidate edit
  -> trusted controller submission
  -> GitHub PR
  -> isolated DGX correctness and benchmark workflow
  -> validated controller result
  -> result guard success
  -> successful systemd one-shot completion
```

## 8. Recommended diagnostic procedure for future failures

1. Check whether the unit is still running or has actually failed:

   ```bash
   systemctl show gemm-autoresearch-agent.service \
     -p ActiveState -p SubState -p Result -p ExecMainStatus --no-pager
   ```

2. Find the exact run directory in the journal:

   ```bash
   journalctl -u gemm-autoresearch-agent.service -n 100 --no-pager
   ```

3. Inspect only that protected run directory with sudo:

   ```bash
   sudo sh -c '
   run=/var/lib/gemm-agent/runs/<run-id>
   echo "=== CODEX EXIT ==="
   cat "$run/exit-code.txt"
   echo "=== FINAL ==="
   cat "$run/final.txt"
   echo "=== STDERR ==="
   sed -n "1,240p" "$run/stderr.log"
   echo "=== CONTROLLER ==="
   cat "$run/controller-status.json"
   '
   ```

4. If the message mentions bubblewrap, also check AppArmor and the kernel log:

   ```bash
   sudo aa-status | grep -E 'bwrap|unpriv_bwrap'
   sudo journalctl -k --since '-10 minutes' --no-pager \
     | grep -Ei 'apparmor|audit|denied|seccomp|bwrap'
   ```

5. Compare the installed unit and policy with the repository:

   ```bash
   systemctl cat gemm-autoresearch-agent.service --no-pager
   sudo sed -n '1,220p' /var/lib/gemm-agent/.codex/config.toml
   ```

6. Apply fixes through the repository and installer. Do not hand-edit the live
   systemd unit or root-owned Codex policy, because the next installation would
   overwrite an undocumented change:

   ```bash
   cd ~/gemm-step5-controller-install
   git pull --ff-only
   sudo ./ops/install-step6.sh
   sudo systemctl reset-failed gemm-autoresearch-agent.service
   sudo systemctl start --no-block gemm-autoresearch-agent.service
   ```

7. Do not treat `inactive (dead)` as a failure without checking `Result`. For a
   successful one-shot experiment, `inactive (dead)` is correct.

## 9. Upgrade caution

The compatibility findings above are specific to the tested Codex 0.147.0 and
Ubuntu 24.04 environment. After upgrading Codex, Ubuntu, systemd, AppArmor, or
bubblewrap:

1. rerun the candidate/auth/network/controller preflight;
2. launch one bounded experiment;
3. require a new trusted controller result;
4. review systemd and kernel logs for new denials; and
5. reassess whether `MemoryDenyWriteExecute=false`, `ProcSubset=all`, or any
   release-path allowance can be narrowed for the new version.

Do not respond to sandbox compatibility errors by granting the agent generic
network access, GitHub credentials, GPU access, root privileges, broad
filesystem writes, or the ability to edit the verifier.

## 10. References

- [OpenAI Codex permissions](https://learn.chatgpt.com/docs/permissions)
- [OpenAI Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode)
- [systemd execution environment and sandbox directives](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html)
- [Commit `5d59344`: bubblewrap compatibility](https://github.com/cryptogakusei/gemm-autoresearch/commit/5d593442f9e9e33c52110850869c1460f8ecbe66)
- [Commit `9b963d0`: controller socket proxy](https://github.com/cryptogakusei/gemm-autoresearch/commit/9b963d052f6d29de517cc3b8764b0b2f1f36a92a)
- [Commit `3739d97`: verified Codex launcher](https://github.com/cryptogakusei/gemm-autoresearch/commit/3739d972f08cdb4d05268260d1e465d4994247ac)
- [Commit `afa65c8`: unsuccessful non-JIT fallback](https://github.com/cryptogakusei/gemm-autoresearch/commit/afa65c87c5d413f3303dd68af76a301c5fdbf8c9)
- [Commit `012b84b`: code-mode JIT compatibility](https://github.com/cryptogakusei/gemm-autoresearch/commit/012b84bf2c7ee2b630340f1979692515cc72e968)
- [Commit `01ee159`: read-only `/proc/sys` visibility](https://github.com/cryptogakusei/gemm-autoresearch/commit/01ee1598f800ffc27f232f2bb1c09a46a073c364)
- [Successful autonomous PR #4](https://github.com/cryptogakusei/gemm-autoresearch/pull/4)
- [Successful trusted workflow run](https://github.com/cryptogakusei/gemm-autoresearch/actions/runs/31604313165)
