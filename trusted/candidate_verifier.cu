#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <vector>

#include <cublas_v2.h>
#include <cuda_runtime.h>

#include "candidate_api.h"

namespace {

constexpr size_t GUARD_ELEMENTS = 16384;
constexpr float GUARD_VALUE = 12345.25f;
constexpr double ABSOLUTE_TOLERANCE = 1.0e-2;
constexpr double RELATIVE_TOLERANCE = 1.0e-4;
constexpr uint64_t CPU_REFERENCE_FLOP_LIMIT = 200000000ULL;

[[noreturn]] void fail_cuda(const char *operation, cudaError_t error) {
    std::fprintf(stderr, "%s: %s\n", operation, cudaGetErrorString(error));
    std::exit(3);
}

void check_cuda(cudaError_t error, const char *operation) {
    if (error != cudaSuccess) fail_cuda(operation, error);
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

bool is_power_of_two(int value) {
    return value >= 16 && value <= 4096 && (value & (value - 1)) == 0;
}

void cpu_reference(const std::vector<float> &A,
                   const std::vector<float> &B,
                   const std::vector<float> &initial_C,
                   std::vector<float> &reference,
                   float alpha, float beta, int M, int N, int K) {
    for (int row = 0; row < M; ++row) {
        for (int column = 0; column < N; ++column) {
            float accumulator = 0.0f;
            for (int inner = 0; inner < K; ++inner) {
                accumulator += A[static_cast<size_t>(row) * K + inner]
                             * B[static_cast<size_t>(inner) * N + column];
            }
            const size_t index = static_cast<size_t>(row) * N + column;
            reference[index] = alpha * accumulator + beta * initial_C[index];
        }
    }
}

}  // namespace

int main(int argc, char **argv) {
    if (argc != 8) {
        std::fprintf(stderr, "usage: %s CASE M N K ALPHA BETA SEED\n", argv[0]);
        return 2;
    }

    const char *case_name = argv[1];
    const int M = std::atoi(argv[2]);
    const int N = std::atoi(argv[3]);
    const int K = std::atoi(argv[4]);
    const float alpha = std::strtof(argv[5], nullptr);
    const float beta = std::strtof(argv[6], nullptr);
    uint32_t random_state = static_cast<uint32_t>(
        std::strtoul(argv[7], nullptr, 10)) + 1u;

    if (!is_power_of_two(M) || !is_power_of_two(N) || !is_power_of_two(K)) {
        std::fprintf(stderr,
                     "dimensions must be powers of two in the inclusive range [16,4096]\n");
        return 2;
    }

    const size_t size_A = static_cast<size_t>(M) * K;
    const size_t size_B = static_cast<size_t>(K) * N;
    const size_t size_C = static_cast<size_t>(M) * N;
    std::vector<float> host_A(size_A);
    std::vector<float> host_B(size_B);
    std::vector<float> initial_C(size_C);
    std::vector<float> reference(size_C);
    for (float &value : host_A) value = random_float(random_state);
    for (float &value : host_B) value = random_float(random_state);
    for (float &value : initial_C) value = random_float(random_state);

    std::vector<float> padded_A(size_A + 2 * GUARD_ELEMENTS, GUARD_VALUE);
    std::vector<float> padded_B(size_B + 2 * GUARD_ELEMENTS, GUARD_VALUE);
    std::vector<float> padded_C(size_C + 2 * GUARD_ELEMENTS, GUARD_VALUE);
    std::copy(host_A.begin(), host_A.end(), padded_A.begin() + GUARD_ELEMENTS);
    std::copy(host_B.begin(), host_B.end(), padded_B.begin() + GUARD_ELEMENTS);
    std::copy(initial_C.begin(), initial_C.end(), padded_C.begin() + GUARD_ELEMENTS);

    float *base_A = nullptr;
    float *base_B = nullptr;
    float *base_C = nullptr;
    check_cuda(cudaMalloc(&base_A, padded_A.size() * sizeof(float)), "cudaMalloc A");
    check_cuda(cudaMalloc(&base_B, padded_B.size() * sizeof(float)), "cudaMalloc B");
    check_cuda(cudaMalloc(&base_C, padded_C.size() * sizeof(float)), "cudaMalloc C");
    check_cuda(cudaMemcpy(base_A, padded_A.data(), padded_A.size() * sizeof(float),
                          cudaMemcpyHostToDevice), "copy A");
    check_cuda(cudaMemcpy(base_B, padded_B.data(), padded_B.size() * sizeof(float),
                          cudaMemcpyHostToDevice), "copy B");
    check_cuda(cudaMemcpy(base_C, padded_C.data(), padded_C.size() * sizeof(float),
                          cudaMemcpyHostToDevice), "copy C");
    float *device_A = base_A + GUARD_ELEMENTS;
    float *device_B = base_B + GUARD_ELEMENTS;
    float *device_C = base_C + GUARD_ELEMENTS;

    const uint64_t operation_count =
        2ULL * static_cast<uint64_t>(M) * N * K;
    const char *reference_source = "cpu_naive";
    if (operation_count <= CPU_REFERENCE_FLOP_LIMIT) {
        cpu_reference(host_A, host_B, initial_C, reference,
                      alpha, beta, M, N, K);
    } else {
        reference_source = "cublas_pedantic";
        float *device_reference = nullptr;
        check_cuda(cudaMalloc(&device_reference, size_C * sizeof(float)),
                   "cudaMalloc reference");
        check_cuda(cudaMemcpy(device_reference, initial_C.data(), size_C * sizeof(float),
                              cudaMemcpyHostToDevice), "copy reference C");
        cublasHandle_t handle;
        check_cublas(cublasCreate(&handle), "cublasCreate");
        check_cublas(cublasSetMathMode(handle, CUBLAS_PEDANTIC_MATH),
                     "cublasSetMathMode");
        check_cublas(cublasSgemm(handle, CUBLAS_OP_N, CUBLAS_OP_N,
                                 N, M, K, &alpha,
                                 device_B, N, device_A, K,
                                 &beta, device_reference, N), "cublasSgemm");
        check_cuda(cudaDeviceSynchronize(), "synchronize reference");
        check_cuda(cudaMemcpy(reference.data(), device_reference, size_C * sizeof(float),
                              cudaMemcpyDeviceToHost), "copy reference result");
        check_cublas(cublasDestroy(handle), "cublasDestroy");
        check_cuda(cudaFree(device_reference), "free reference");
    }

    check_cuda(launch_candidate_gemm(device_A, device_B, device_C,
                                     alpha, beta, M, N, K, nullptr),
               "candidate launch");
    check_cuda(cudaDeviceSynchronize(), "candidate synchronize");
    check_cuda(cudaMemcpy(padded_A.data(), base_A, padded_A.size() * sizeof(float),
                          cudaMemcpyDeviceToHost), "copy checked A");
    check_cuda(cudaMemcpy(padded_B.data(), base_B, padded_B.size() * sizeof(float),
                          cudaMemcpyDeviceToHost), "copy checked B");
    check_cuda(cudaMemcpy(padded_C.data(), base_C, padded_C.size() * sizeof(float),
                          cudaMemcpyDeviceToHost), "copy checked C");

    double max_absolute = 0.0;
    double max_relative = 0.0;
    size_t mismatches = 0;
    for (size_t index = 0; index < size_C; ++index) {
        const double expected = reference[index];
        const double actual = padded_C[GUARD_ELEMENTS + index];
        const double error = std::fabs(actual - expected);
        const double relative = error / std::max(std::fabs(expected), 1.0e-12);
        max_absolute = std::max(max_absolute, error);
        max_relative = std::max(max_relative, relative);
        if (!std::isfinite(actual) ||
            error > ABSOLUTE_TOLERANCE + RELATIVE_TOLERANCE * std::fabs(expected)) {
            ++mismatches;
        }
    }

    size_t guard_corruptions = 0;
    auto count_guard_changes = [&](const std::vector<float> &padded, size_t payload) {
        size_t changes = 0;
        for (size_t index = 0; index < GUARD_ELEMENTS; ++index)
            if (padded[index] != GUARD_VALUE) ++changes;
        for (size_t index = GUARD_ELEMENTS + payload; index < padded.size(); ++index)
            if (padded[index] != GUARD_VALUE) ++changes;
        return changes;
    };
    guard_corruptions += count_guard_changes(padded_A, size_A);
    guard_corruptions += count_guard_changes(padded_B, size_B);
    guard_corruptions += count_guard_changes(padded_C, size_C);

    size_t input_mutations = 0;
    for (size_t index = 0; index < size_A; ++index)
        if (padded_A[GUARD_ELEMENTS + index] != host_A[index]) ++input_mutations;
    for (size_t index = 0; index < size_B; ++index)
        if (padded_B[GUARD_ELEMENTS + index] != host_B[index]) ++input_mutations;

    const bool pass = mismatches == 0 && guard_corruptions == 0 && input_mutations == 0;
    std::printf("%s,%s,%s,%.9e,%.9e,%zu,%zu,%zu,%zu\n",
                case_name, pass ? "PASS" : "FAIL", reference_source,
                max_absolute, max_relative, mismatches, size_C,
                guard_corruptions, input_mutations);

    cudaFree(base_A);
    cudaFree(base_B);
    cudaFree(base_C);
    return pass ? 0 : 1;
}
