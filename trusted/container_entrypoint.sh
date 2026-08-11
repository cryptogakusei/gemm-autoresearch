#!/usr/bin/env bash

set -uo pipefail

if [[ $# -ne 2 ]]; then
    echo "usage: $0 CANDIDATE_SOURCE RESULTS_DIR" >&2
    exit 2
fi

candidate_source="$1"
results_dir="$2"

set +e
/competition/trusted/run_competition.sh "$candidate_source" "$results_dir"
competition_status=$?
set -e

printf '%s\n' "$competition_status" >/state/competition_exit_code
touch /state/complete

# The host copies allow-listed results while tmpfs is still mounted, then
# creates this file through a read-only bind mount to release the container.
for ((attempt = 0; attempt < 3000; attempt++)); do
    if [[ -e /control/release ]]; then
        exit "$competition_status"
    fi
    sleep 0.1
done

echo "host did not release completed sandbox" >&2
exit 125
