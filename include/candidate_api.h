#pragma once

#include <cuda_runtime.h>

// Stable competition ABI. Autoresearch candidates implement this function.
// Matrices are contiguous FP32 row-major arrays and may alias no other input.
// The launcher must enqueue work on `stream` and return the launch status.
extern "C" cudaError_t launch_candidate_gemm(
    const float *A,
    const float *B,
    float *C,
    float alpha,
    float beta,
    int M,
    int N,
    int K,
    cudaStream_t stream);
