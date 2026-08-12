#!/usr/bin/env bash

set -euo pipefail
umask 077

readonly EXPECTED_USER=gemm-agent
readonly AGENT_HOME=/var/lib/gemm-agent
readonly WORKSPACE="$AGENT_HOME/workspace/gemm-autoresearch"
readonly RUNS_DIR="$AGENT_HOME/runs"
readonly CODEX_HOME_DIR="$AGENT_HOME/.codex"
readonly CODEX_BIN="$AGENT_HOME/.local/bin/codex"
readonly PROMPT_FILE=/usr/local/share/gemm-autoresearch/ONE_ITERATION_PROMPT.md
readonly CODEX_CONFIG="$CODEX_HOME_DIR/config.toml"

if [[ "$(id -un)" != "$EXPECTED_USER" ]]; then
    echo "run_codex_trial.sh must run as $EXPECTED_USER" >&2
    exit 1
fi
for required in \
    "$CODEX_CONFIG" \
    "$PROMPT_FILE" \
    "$WORKSPACE/.git" \
    "$WORKSPACE/candidate/candidate_gemm.cu"; do
    if [[ ! -e "$required" || -L "$required" ]]; then
        echo "missing or unsafe agent input: $required" >&2
        exit 1
    fi
done
if [[ ! -L "$CODEX_BIN" ]]; then
    echo "Codex launcher is not the expected installer symlink: $CODEX_BIN" >&2
    exit 1
fi
CODEX_REAL="$(/usr/bin/readlink -e -- "$CODEX_BIN")" || {
    echo "Codex launcher target cannot be resolved: $CODEX_BIN" >&2
    exit 1
}
if [[ ! "$CODEX_REAL" =~ ^/var/lib/gemm-agent/\.codex/packages/standalone/releases/[^/]+/bin/codex$ ]]; then
    echo "Codex launcher resolves outside the standalone release directory: $CODEX_REAL" >&2
    exit 1
fi
if [[ ! -f "$CODEX_REAL" || ! -x "$CODEX_REAL" ]]; then
    echo "Codex CLI target is not a regular executable: $CODEX_REAL" >&2
    exit 1
fi
if [[ "$(stat -c '%U:%G:%a' "$CODEX_CONFIG")" != "root:root:644" ]]; then
    echo "Codex policy has unexpected ownership or mode" >&2
    exit 1
fi
export HOME="$AGENT_HOME"
export CODEX_HOME="$CODEX_HOME_DIR"
export PATH="$AGENT_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"

"$CODEX_BIN" login status >/dev/null

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_DIR="$RUNS_DIR/$STAMP-$$"
mkdir -m 700 -- "$RUN_DIR"

set +e
"$CODEX_BIN" --strict-config --ask-for-approval never exec \
    --cd "$WORKSPACE" \
    --ignore-rules \
    --ephemeral \
    --json \
    --output-last-message "$RUN_DIR/final.txt" \
    - <"$PROMPT_FILE" >"$RUN_DIR/events.jsonl" 2>"$RUN_DIR/stderr.log"
CODEX_STATUS=$?
set -e

/usr/local/bin/gemmctl status >"$RUN_DIR/controller-status.json" 2>&1 || true
printf '%s\n' "$CODEX_STATUS" >"$RUN_DIR/exit-code.txt"
printf 'Codex trial log: %s\n' "$RUN_DIR"
exit "$CODEX_STATUS"
