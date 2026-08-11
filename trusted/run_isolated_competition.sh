#!/usr/bin/env bash

set -euo pipefail
umask 077

readonly CUDA_IMAGE="nvcr.io/nvidia/cuda@sha256:7d2f6a8c2071d911524f95061a0db363e24d27aa51ec831fcccf9e76eb72bc92"
readonly MAX_CANDIDATE_BYTES=1048576
readonly MAX_ARTIFACT_BYTES=134217728
readonly CONTAINER_TIMEOUT_SECONDS=2400
readonly DOCKER_CLI=/usr/bin/docker
readonly DOCKER_CONTEXT=rootless

if [[ $# -ne 3 ]]; then
    echo "usage: $0 CANDIDATE_SOURCE COMPETITION_DIR RESULTS_DIR" >&2
    exit 2
fi

CANDIDATE_SOURCE="$1"
COMPETITION_DIR="$2"
RESULTS_DIR="$3"

if [[ ! -f "$CANDIDATE_SOURCE" || -L "$CANDIDATE_SOURCE" ]]; then
    echo "candidate must be a regular, non-symlink file" >&2
    exit 2
fi
candidate_bytes="$(stat -c '%s' -- "$CANDIDATE_SOURCE")"
if (( candidate_bytes == 0 || candidate_bytes > MAX_CANDIDATE_BYTES )); then
    echo "candidate size must be between 1 and $MAX_CANDIDATE_BYTES bytes" >&2
    exit 2
fi

for required in \
    "$COMPETITION_DIR/include/candidate_api.h" \
    "$COMPETITION_DIR/trusted/container_entrypoint.sh" \
    "$COMPETITION_DIR/trusted/run_competition.sh" \
    "$COMPETITION_DIR/trusted/candidate_verifier.cu" \
    "$COMPETITION_DIR/trusted/candidate_benchmark.cu" \
    "$COMPETITION_DIR/trusted/cases/correctness.tsv" \
    "$COMPETITION_DIR/trusted/cases/performance.tsv"; do
    if [[ ! -f "$required" || -L "$required" ]]; then
        echo "trusted input missing or not regular: $required" >&2
        exit 2
    fi
done

if [[ -e "$RESULTS_DIR" ]]; then
    echo "results directory already exists: $RESULTS_DIR" >&2
    exit 2
fi
mkdir -m 700 -- "$RESULTS_DIR"

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
DOCKER=("$DOCKER_CLI" --context "$DOCKER_CONTEXT")
security_options="$("${DOCKER[@]}" info --format '{{json .SecurityOptions}}')"
if [[ "$security_options" != *rootless* ]]; then
    echo "refusing to run candidate without a rootless Docker daemon" >&2
    exit 2
fi
if ! "${DOCKER[@]}" image inspect "$CUDA_IMAGE" >/dev/null 2>&1; then
    echo "pinned sandbox image is not present in the rootless image store" >&2
    exit 2
fi

SANDBOX_ROOT="$(mktemp -d /tmp/gemm-sandbox.XXXXXXXX)"
CONTAINER_ID=""
cleanup() {
    if [[ -n "$CONTAINER_ID" ]]; then
        "${DOCKER[@]}" rm --force "$CONTAINER_ID" >/dev/null 2>&1 || true
    fi
    if [[ "$SANDBOX_ROOT" == /tmp/gemm-sandbox.* ]]; then
        rm -rf -- "$SANDBOX_ROOT"
    fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

install -d -m 700 \
    "$SANDBOX_ROOT/candidate" \
    "$SANDBOX_ROOT/competition" \
    "$SANDBOX_ROOT/control"
install -m 400 -- \
    "$CANDIDATE_SOURCE" \
    "$SANDBOX_ROOT/candidate/candidate_gemm.cu"
cp -a -- "$COMPETITION_DIR/include" "$SANDBOX_ROOT/competition/include"
cp -a -- "$COMPETITION_DIR/trusted" "$SANDBOX_ROOT/competition/trusted"

CONTAINER_ID="$("${DOCKER[@]}" create \
    --pull never \
    --init \
    --hostname gemm-sandbox \
    --network none \
    --read-only \
    --cap-drop ALL \
    --security-opt no-new-privileges:true \
    --pids-limit 512 \
    --memory 48g \
    --memory-swap 48g \
    --cpus 18 \
    --ulimit core=0:0 \
    --ulimit nofile=1024:1024 \
    --ulimit nproc=512:512 \
    --shm-size 128m \
    --log-driver none \
    --device nvidia.com/gpu=all \
    --mount "type=bind,src=$SANDBOX_ROOT/candidate,dst=/candidate,readonly" \
    --mount "type=bind,src=$SANDBOX_ROOT/competition,dst=/competition,readonly" \
    --mount "type=bind,src=$SANDBOX_ROOT/control,dst=/control,readonly" \
    --tmpfs /tmp:rw,nosuid,nodev,size=1g,mode=1777 \
    --tmpfs /build:rw,nosuid,nodev,size=4g,mode=700 \
    --tmpfs /results:rw,nosuid,nodev,noexec,size=512m,mode=700 \
    --tmpfs /state:rw,nosuid,nodev,noexec,size=1m,mode=700 \
    --env HOME=/tmp \
    --env BUILD_DIR=/build \
    --env CUDA_ARCH=sm_121 \
    --env CASE_TIMEOUT=180 \
    --workdir /competition \
    --entrypoint /bin/bash \
    "$CUDA_IMAGE" \
    /competition/trusted/container_entrypoint.sh \
    /candidate/candidate_gemm.cu \
    /results)"

"${DOCKER[@]}" start "$CONTAINER_ID" >/dev/null
deadline=$((SECONDS + CONTAINER_TIMEOUT_SECONDS))
completed=false
while (( SECONDS < deadline )); do
    if "${DOCKER[@]}" exec "$CONTAINER_ID" /usr/bin/test -f /state/complete \
        >/dev/null 2>&1; then
        completed=true
        break
    fi
    running="$("${DOCKER[@]}" inspect --format '{{.State.Running}}' "$CONTAINER_ID")"
    if [[ "$running" != true ]]; then
        break
    fi
    sleep 1
done

if [[ "$completed" != true ]]; then
    running="$("${DOCKER[@]}" inspect --format '{{.State.Running}}' "$CONTAINER_ID")"
    if [[ "$running" == true ]]; then
        echo "sandbox exceeded the ${CONTAINER_TIMEOUT_SECONDS}s wall-clock limit" >&2
        container_status=124
    else
        container_status="$("${DOCKER[@]}" inspect --format '{{.State.ExitCode}}' "$CONTAINER_ID")"
        echo "sandbox exited before publishing completion status" >&2
    fi
    "${DOCKER[@]}" rm --force "$CONTAINER_ID" >/dev/null 2>&1 || true
    CONTAINER_ID=""
    cat >"$RESULTS_DIR/sandbox.txt" <<EOF
image=$CUDA_IMAGE
rootless=true
network=none
read_only_root=true
capabilities=none
candidate_bytes=$candidate_bytes
artifact_bytes=0
container_exit_code=$container_status
EOF
    exit "$container_status"
fi

COPIED_BYTES=0
COPY_INDEX=0
copy_result() {
    local relative_path="$1"
    local per_file_limit="$2"
    local quarantine_file size destination

    ((COPY_INDEX += 1))
    quarantine_file="$SANDBOX_ROOT/result-$COPY_INDEX"
    if ! timeout --signal=TERM --kill-after=1s 5s \
        "${DOCKER[@]}" exec "$CONTAINER_ID" \
        /usr/bin/head --bytes="$((per_file_limit + 1))" -- \
        "/results/$relative_path" \
        >"$quarantine_file" 2>/dev/null; then
        rm -f -- "$quarantine_file"
        return 0
    fi
    size="$(stat -c '%s' -- "$quarantine_file")"
    if (( size > per_file_limit || COPIED_BYTES + size > MAX_ARTIFACT_BYTES )); then
        echo "discarding oversized result: $relative_path" >&2
        rm -f -- "$quarantine_file"
        return 0
    fi
    COPIED_BYTES=$((COPIED_BYTES + size))
    destination="$RESULTS_DIR/$relative_path"
    mkdir -p -- "$(dirname -- "$destination")"
    install -m 600 -- "$quarantine_file" "$destination"
    rm -f -- "$quarantine_file"
}

copy_result correctness.csv 8388608
copy_result performance.csv 8388608
copy_result score.txt 65536
copy_result system.txt 65536
copy_result verifier_compile.log 33554432
copy_result benchmark_compile.log 33554432

while read -r case_name _; do
    [[ -z "${case_name:-}" || "$case_name" == \#* ]] && continue
    [[ "$case_name" =~ ^[A-Za-z0-9_.-]+$ ]] || continue
    copy_result "logs/correctness.$case_name.out" 8388608
    copy_result "logs/correctness.$case_name.err" 8388608
done <"$COMPETITION_DIR/trusted/cases/correctness.tsv"

while read -r case_name _; do
    [[ -z "${case_name:-}" || "$case_name" == \#* ]] && continue
    [[ "$case_name" =~ ^[A-Za-z0-9_.-]+$ ]] || continue
    copy_result "logs/performance.$case_name.err" 8388608
done <"$COMPETITION_DIR/trusted/cases/performance.tsv"

touch -- "$SANDBOX_ROOT/control/release"
container_status="$("${DOCKER[@]}" wait "$CONTAINER_ID")"
if [[ ! "$container_status" =~ ^[0-9]+$ ]]; then
    echo "invalid container exit status: $container_status" >&2
    container_status=125
fi

cat >"$RESULTS_DIR/sandbox.txt" <<EOF
image=$CUDA_IMAGE
rootless=true
network=none
read_only_root=true
capabilities=none
candidate_bytes=$candidate_bytes
artifact_bytes=$COPIED_BYTES
container_exit_code=$container_status
EOF

exit "$container_status"
