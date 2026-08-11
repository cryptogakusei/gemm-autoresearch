#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <vector>

#include <cublas_v2.h>
#include <cuda_runtime.h>

#include "candidate_api.h"

namespace {

void check_cuda(cudaError_t error, const char *operation) {
    if (error != cudaSuccess) {
        std::fprintf(stderr, "%s: %s\n", operation, cudaGetErrorString(error));
        std::exit(3);
    }
}

void check_cublas(cublasStatus_t status, const char *operation) {
    if (status != CUBLAS_STATUS_SUCCESS) {
        std::fprintf(stderr, "%s: cuBLAS status=%d\n", operation,
                     static_cast<int>(status));
        std::exit(4);
    }
}

uint32_t next_random(uint32_t &state) {
    state ^= state << 13;
    state ^= state >> 17;
    state ^= state << 5;
    return state;
}

float random_float(uint32_t &state) {
    return static_cast<float>(next_random(state)) /
               static_cast<float>(UINT32_MAX) * 2.0f - 1.0f;
}

template <typename Launcher>
float time_launches(Launcher launch, int iterations) {
    for (int warmup = 0; warmup < 5; ++warmup) launch();
    check_cuda(cudaDeviceSynchronize(), "warmup synchronize");
    cudaEvent_t start;
    cudaEvent_t stop;
    check_cuda(cudaEventCreate(&start), "create start event");
    check_cuda(cudaEventCreate(&stop), "create stop event");
    check_cuda(cudaEventRecord(start), "record start");
    for (int iteration = 0; iteration < iterations; ++iteration) launch();
    check_cuda(cudaEventRecord(stop), "record stop");
    check_cuda(cudaEventSynchronize(stop), "synchronize stop");
    float milliseconds = 0.0f;
    check_cuda(cudaEventElapsedTime(&milliseconds, start, stop), "elapsed time");
    check_cuda(cudaEventDestroy(start), "destroy start event");
    check_cuda(cudaEventDestroy(stop), "destroy stop event");
    return milliseconds / iterations;
}

}  // namespace

int main(int argc, char **argv) {
    if (argc != 6) {
        std::fprintf(stderr, "usage: %s CASE M N K SEED\n", argv[0]);
        return 2;
    }
    const char *case_name = argv[1];
    const int M = std::atoi(argv[2]);
    const int N = std::atoi(argv[3]);
    const int K = std::atoi(argv[4]);
    uint32_t state = static_cast<uint32_t>(std::strtoul(argv[5], nullptr, 10)) + 1u;
    const size_t size_A = static_cast<size_t>(M) * K;
    const size_t size_B = static_cast<size_t>(K) * N;
    const size_t size_C = static_cast<size_t>(M) * N;
    std::vector<float> host_A(size_A);
    std::vector<float> host_B(size_B);
    for (float &value : host_A) value = random_float(state);
    for (float &value : host_B) value = random_float(state);

    float *device_A = nullptr;
    float *device_B = nullptr;
    float *device_C = nullptr;
    check_cuda(cudaMalloc(&device_A, size_A * sizeof(float)), "cudaMalloc A");
    check_cuda(cudaMalloc(&device_B, size_B * sizeof(float)), "cudaMalloc B");
    check_cuda(cudaMalloc(&device_C, size_C * sizeof(float)), "cudaMalloc C");
    check_cuda(cudaMemcpy(device_A, host_A.data(), size_A * sizeof(float),
                          cudaMemcpyHostToDevice), "copy A");
    check_cuda(cudaMemcpy(device_B, host_B.data(), size_B * sizeof(float),
                          cudaMemcpyHostToDevice), "copy B");
    check_cuda(cudaMemset(device_C, 0, size_C * sizeof(float)), "clear C");

    constexpr float alpha = 1.0f;
    constexpr float beta = 0.0f;
    const double flops = 2.0 * M * N * K;
    const int iterations = std::max(10, std::min(1000,
        static_cast<int>(2000000000000.0 / flops)));

    auto candidate_launch = [&] {
        check_cuda(launch_candidate_gemm(device_A, device_B, device_C,
                                         alpha, beta, M, N, K, nullptr),
                   "candidate launch");
    };
    const float candidate_ms = time_launches(candidate_launch, iterations);

    cublasHandle_t handle;
    check_cublas(cublasCreate(&handle), "cublasCreate");
    check_cublas(cublasSetMathMode(handle, CUBLAS_DEFAULT_MATH),
                 "cublasSetMathMode");
    auto cublas_launch = [&] {
        check_cublas(cublasSgemm(handle, CUBLAS_OP_N, CUBLAS_OP_N,
                                 N, M, K, &alpha,
                                 device_B, N, device_A, K,
                                 &beta, device_C, N), "cublasSgemm");
    };
    const float cublas_ms = time_launches(cublas_launch, iterations);
    check_cublas(cublasDestroy(handle), "cublasDestroy");

    const double candidate_gflops = flops / (candidate_ms * 1.0e6);
    const double cublas_gflops = flops / (cublas_ms * 1.0e6);
    const double ratio = candidate_gflops / cublas_gflops;
    std::printf("%s,%d,%d,%d,%d,%.6f,%.1f,%.6f,%.1f,%.6f\n",
                case_name, M, N, K, iterations,
                candidate_ms, candidate_gflops,
                cublas_ms, cublas_gflops, ratio);

    cudaFree(device_A);
    cudaFree(device_B);
    cudaFree(device_C);
    return 0;
}
