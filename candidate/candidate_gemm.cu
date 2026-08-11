#include "candidate_api.h"

namespace {

constexpr int TILE = 16;

// Safe baseline candidate. Autoresearch agents may replace this implementation
// but must preserve launch_candidate_gemm and the published contract.
__global__ void candidate_kernel(const float *A, const float *B, float *C,
                                 float alpha, float beta,
                                 int M, int N, int K) {
    __shared__ float tile_a[TILE][TILE];
    __shared__ float tile_b[TILE][TILE];

    const int row = blockIdx.y * TILE + threadIdx.y;
    const int column = blockIdx.x * TILE + threadIdx.x;
    float accumulator = 0.0f;

    for (int tile = 0; tile < (K + TILE - 1) / TILE; ++tile) {
        const int a_column = tile * TILE + threadIdx.x;
        const int b_row = tile * TILE + threadIdx.y;
        tile_a[threadIdx.y][threadIdx.x] =
            row < M && a_column < K ? A[row * K + a_column] : 0.0f;
        tile_b[threadIdx.y][threadIdx.x] =
            b_row < K && column < N ? B[b_row * N + column] : 0.0f;
        __syncthreads();

#pragma unroll
        for (int inner = 0; inner < TILE; ++inner) {
            accumulator += tile_a[threadIdx.y][inner]
                         * tile_b[inner][threadIdx.x];
        }
        __syncthreads();
    }

    if (row < M && column < N) {
        const int index = row * N + column;
        C[index] = alpha * accumulator + beta * C[index];
    }
}

}  // namespace

extern "C" cudaError_t launch_candidate_gemm(
    const float *A, const float *B, float *C,
    float alpha, float beta, int M, int N, int K,
    cudaStream_t stream) {
    if (M <= 0 || N <= 0 || K <= 0) return cudaErrorInvalidValue;
    const dim3 block(TILE, TILE);
    const dim3 grid((N + TILE - 1) / TILE, (M + TILE - 1) / TILE);
    candidate_kernel<<<grid, block, 0, stream>>>(
        A, B, C, alpha, beta, M, N, K);
    return cudaGetLastError();
}
