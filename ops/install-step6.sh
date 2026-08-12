#!/usr/bin/env bash

set -euo pipefail
umask 077
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    echo "install-step6.sh must run as root" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
readonly RUNNER_SERVICE=actions.runner.cryptogakusei-gemm-autoresearch.spark-4959.service
readonly AGENT_HOME=/var/lib/gemm-agent
readonly WORKSPACE="$AGENT_HOME/workspace/gemm-autoresearch"
readonly LIBEXEC_DIR=/usr/local/libexec/gemm-autoresearch
readonly SHARE_DIR=/usr/local/share/gemm-autoresearch

for required in \
    "$SOURCE_ROOT/controller/install-controller.sh" \
    "$SOURCE_ROOT/agent/run_codex_trial.sh" \
    "$SOURCE_ROOT/ops/run_autoresearch_batch.py" \
    "$SOURCE_ROOT/agent/codex-config.toml" \
    "$SOURCE_ROOT/agent/ONE_ITERATION_PROMPT.md" \
    "$SOURCE_ROOT/agent/gemm-autoresearch-agent.service" \
    "$SOURCE_ROOT/agent/gemm-autoresearch-batch@.service" \
    "$SOURCE_ROOT/ops/gemm-autoresearch-tmpfiles.conf"; do
    if [[ ! -f "$required" || -L "$required" ]]; then
        echo "missing or unsafe Step 6 input: $required" >&2
        exit 1
    fi
done
"$SOURCE_ROOT/controller/install-controller.sh"
for account in dgx-ci gemm-agent; do
    if ! id "$account" >/dev/null 2>&1; then
        echo "required account is missing: $account" >&2
        exit 1
    fi
done
if ! /usr/bin/systemctl cat "$RUNNER_SERVICE" >/dev/null; then
    echo "GitHub runner service not found: $RUNNER_SERVICE" >&2
    exit 1
fi
if ! getent group gemm-gpu >/dev/null; then
    /usr/sbin/groupadd --system gemm-gpu
fi
/usr/sbin/usermod --append --groups gemm-gpu dgx-ci

/usr/bin/install -o root -g root -m 0644 \
    "$SOURCE_ROOT/ops/gemm-autoresearch-tmpfiles.conf" \
    /etc/tmpfiles.d/gemm-autoresearch.conf
/usr/bin/systemd-tmpfiles --create /etc/tmpfiles.d/gemm-autoresearch.conf

/usr/bin/install -d -o gemm-agent -g gemm-agent -m 0700 \
    "$AGENT_HOME/.codex" \
    "$AGENT_HOME/.local" \
    "$AGENT_HOME/runs" \
    "$AGENT_HOME/workspace"
if [[ ! -e "$WORKSPACE" ]]; then
    /usr/sbin/runuser --user gemm-agent -- \
        /usr/bin/env HOME="$AGENT_HOME" \
        /usr/bin/git clone --depth 1 --branch main \
        https://github.com/cryptogakusei/gemm-autoresearch.git "$WORKSPACE"
elif [[ ! -d "$WORKSPACE/.git" || -L "$WORKSPACE/.git" ]]; then
    echo "refusing unsafe existing workspace: $WORKSPACE" >&2
    exit 1
fi

/usr/bin/install -d -o root -g root -m 0755 "$LIBEXEC_DIR" "$SHARE_DIR"
/usr/bin/install -o root -g root -m 0755 \
    "$SOURCE_ROOT/agent/run_codex_trial.sh" \
    "$LIBEXEC_DIR/run_codex_trial.sh"
/usr/bin/install -o root -g root -m 0755 \
    "$SOURCE_ROOT/ops/run_autoresearch_batch.py" \
    "$LIBEXEC_DIR/run_autoresearch_batch.py"
/usr/bin/install -o root -g root -m 0644 \
    "$SOURCE_ROOT/agent/codex-config.toml" \
    "$AGENT_HOME/.codex/config.toml"
/usr/bin/install -o root -g root -m 0644 \
    "$SOURCE_ROOT/agent/ONE_ITERATION_PROMPT.md" \
    "$SHARE_DIR/ONE_ITERATION_PROMPT.md"
/usr/bin/install -o root -g root -m 0644 \
    "$SOURCE_ROOT/agent/gemm-autoresearch-agent.service" \
    /etc/systemd/system/gemm-autoresearch-agent.service
/usr/bin/install -o root -g root -m 0644 \
    "$SOURCE_ROOT/agent/gemm-autoresearch-batch@.service" \
    /etc/systemd/system/gemm-autoresearch-batch@.service

/usr/bin/systemctl daemon-reload
/usr/bin/systemctl restart "$RUNNER_SERVICE"

if [[ "$(stat -c '%U:%G:%a' /run/lock/gemm-autoresearch)" != "root:gemm-gpu:750" ]]; then
    echo "GPU lock directory has unexpected ownership or mode" >&2
    exit 1
fi
if [[ "$(stat -c '%U:%G:%a' /run/lock/gemm-autoresearch/gpu.lock)" != "root:gemm-gpu:660" ]]; then
    echo "GPU lock file has unexpected ownership or mode" >&2
    exit 1
fi
if [[ "$(stat -c '%U:%G:%a' /run/lock/gemm-autoresearch/batch.lock)" != "root:root:600" ]]; then
    echo "batch lock file has unexpected ownership or mode" >&2
    exit 1
fi
if [[ "$(stat -c '%U:%G:%a' "$AGENT_HOME/.codex/config.toml")" != "root:root:644" ]]; then
    echo "Codex policy has unexpected ownership or mode" >&2
    exit 1
fi
if ! /usr/bin/systemctl is-active --quiet "$RUNNER_SERVICE"; then
    echo "GitHub runner did not restart successfully" >&2
    exit 1
fi

echo "Step 6 host isolation installed."
echo "Run one iteration with:"
echo "  systemctl start --no-block gemm-autoresearch-agent.service"
echo "Run a bounded batch with:"
echo "  systemctl start --no-block gemm-autoresearch-batch@10.service"
