#!/usr/bin/env bash

set -uo pipefail

TRUSTED_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPETITION_DIR="$(cd "$TRUSTED_DIR/.." && pwd)"
CANDIDATE_SOURCE="${1:-$COMPETITION_DIR/candidate/candidate_gemm.cu}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RESULTS_DIR="${2:-$COMPETITION_DIR/results/$STAMP}"
CORRECTNESS_CASES="${CORRECTNESS_CASES:-$TRUSTED_DIR/cases/correctness.tsv}"
PERFORMANCE_CASES="${PERFORMANCE_CASES:-$TRUSTED_DIR/cases/performance.tsv}"
NVCC_BIN="${NVCC:-/usr/local/cuda/bin/nvcc}"
CUDA_ARCH="${CUDA_ARCH:-sm_121}"
CASE_TIMEOUT="${CASE_TIMEOUT:-180}"
BUILD_DIR="$RESULTS_DIR/build"

if [[ ! -f "$CANDIDATE_SOURCE" ]]; then
    echo "candidate source not found: $CANDIDATE_SOURCE" >&2
    exit 2
fi
if [[ ! -x "$NVCC_BIN" ]]; then
    echo "nvcc not found at $NVCC_BIN" >&2
    exit 2
fi

mkdir -p "$BUILD_DIR" "$RESULTS_DIR/logs"
VERIFIER="$BUILD_DIR/candidate_verifier"
BENCHMARK="$BUILD_DIR/candidate_benchmark"

echo "Compiling candidate and trusted verifier..."
"$NVCC_BIN" -O3 -std=c++17 -arch="$CUDA_ARCH" \
    -I"$COMPETITION_DIR/include" \
    "$CANDIDATE_SOURCE" "$TRUSTED_DIR/candidate_verifier.cu" \
    -lcublas -o "$VERIFIER" >"$RESULTS_DIR/verifier_compile.log" 2>&1 || {
        cat "$RESULTS_DIR/verifier_compile.log" >&2
        exit 1
    }

"$NVCC_BIN" -O3 -std=c++17 -arch="$CUDA_ARCH" \
    -I"$COMPETITION_DIR/include" \
    "$CANDIDATE_SOURCE" "$TRUSTED_DIR/candidate_benchmark.cu" \
    -lcublas -o "$BENCHMARK" >"$RESULTS_DIR/benchmark_compile.log" 2>&1 || {
        cat "$RESULTS_DIR/benchmark_compile.log" >&2
        exit 1
    }

CORRECTNESS_CSV="$RESULTS_DIR/correctness.csv"
printf '%s\n' "case,status,reference,max_abs_error,max_rel_error,mismatches,elements_checked,guard_corruptions,input_mutations,exit_code" >"$CORRECTNESS_CSV"
echo "Running correctness gate..."
while read -r case_name M N K alpha beta seed extra; do
    [[ -z "${case_name:-}" || "$case_name" == \#* ]] && continue
    if [[ -n "${extra:-}" ]]; then
        echo "invalid correctness case: $case_name" >&2
        exit 2
    fi
    stdout_log="$RESULTS_DIR/logs/correctness.${case_name}.out"
    stderr_log="$RESULTS_DIR/logs/correctness.${case_name}.err"
    timeout "$CASE_TIMEOUT" "$VERIFIER" "$case_name" "$M" "$N" "$K" \
        "$alpha" "$beta" "$seed" >"$stdout_log" 2>"$stderr_log"
    exit_code=$?
    row="$(tail -n 1 "$stdout_log" 2>/dev/null || true)"
    if [[ "$row" == "$case_name,"* ]]; then
        printf '%s,%d\n' "$row" "$exit_code" >>"$CORRECTNESS_CSV"
    else
        printf '%s\n' "$case_name,RUNTIME_ERROR,,,,,,,,$exit_code" >>"$CORRECTNESS_CSV"
    fi
done <"$CORRECTNESS_CASES"

if awk -F, 'NR > 1 && $2 != "PASS" { failed=1 } END { exit !failed }' "$CORRECTNESS_CSV"; then
    echo "Correctness gate FAILED; performance scoring skipped." >&2
    exit 1
fi

PERFORMANCE_CSV="$RESULTS_DIR/performance.csv"
printf '%s\n' "case,M,N,K,iterations,candidate_ms,candidate_gflops,cublas_ms,cublas_gflops,ratio_to_cublas" >"$PERFORMANCE_CSV"
echo "Correctness passed. Running performance suite..."
while read -r case_name M N K seed extra; do
    [[ -z "${case_name:-}" || "$case_name" == \#* ]] && continue
    if [[ -n "${extra:-}" ]]; then
        echo "invalid performance case: $case_name" >&2
        exit 2
    fi
    "$BENCHMARK" "$case_name" "$M" "$N" "$K" "$seed" \
        >>"$PERFORMANCE_CSV" 2>"$RESULTS_DIR/logs/performance.${case_name}.err" || exit 1
done <"$PERFORMANCE_CASES"

SCORE_TXT="$RESULTS_DIR/score.txt"
awk -F, '
    NR == 1 { next }
    {
        count++
        log_sum += log($10)
        if (count == 1 || $10 < worst) { worst=$10; worst_case=$1 }
    }
    END {
        score = count ? exp(log_sum/count) : 0
        printf "score_geomean_vs_cublas=%.6f\n", score
        printf "score_percent_of_cublas=%.2f\n", 100*score
        printf "worst_ratio=%.6f\n", worst
        printf "worst_case=%s\n", worst_case
        printf "performance_cases=%d\n", count
    }
' "$PERFORMANCE_CSV" | tee "$SCORE_TXT"

{
    echo "timestamp_utc=$STAMP"
    echo "candidate=$CANDIDATE_SOURCE"
    echo "cuda_arch=$CUDA_ARCH"
    "$NVCC_BIN" --version | tail -n 1
    nvidia-smi --query-gpu=name,uuid,driver_version,compute_cap --format=csv,noheader
} >"$RESULTS_DIR/system.txt" 2>&1

echo "Competition results: $RESULTS_DIR"
