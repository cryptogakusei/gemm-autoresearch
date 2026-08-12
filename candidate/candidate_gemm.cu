#include "candidate_api.h"

namespace {
constexpr int BM=64, BN=64, BK=16, TX=16, TY=16, TM=4, TN=4;

__global__ void candidate_kernel(const float *__restrict__ A,
                                 const float *__restrict__ B,
                                 float *__restrict__ C, float alpha, float beta,
                                 int M, int N, int K) {
    __shared__ float As[BM][BK];
    __shared__ float Bs[BK][BN];
    const int tid=threadIdx.y*TX+threadIdx.x;
    const int br=blockIdx.y*BM, bc=blockIdx.x*BN;
    const int tr=threadIdx.y*TM, tc=threadIdx.x*TN;
    float acc[TM][TN]={};
    for (int k0=0; k0<K; k0+=BK) {
        for (int x=tid; x<BM*BK; x+=TX*TY) {
            const int r=x/BK, k=x%BK, gr=br+r;
            As[r][k]=(gr<M && k0+k<K) ? A[gr*K+k0+k] : 0.0f;
        }
        for (int x=tid; x<BK*BN; x+=TX*TY) {
            const int k=x/BN, c=x%BN, gc=bc+c;
            Bs[k][c]=(k0+k<K && gc<N) ? B[(k0+k)*N+gc] : 0.0f;
        }
        __syncthreads();
#pragma unroll
        for (int k=0; k<BK; ++k) {
            float av[TM], bv[TN];
#pragma unroll
            for (int i=0;i<TM;++i) av[i]=As[tr+i][k];
#pragma unroll
            for (int j=0;j<TN;++j) bv[j]=Bs[k][tc+j];
#pragma unroll
            for (int i=0;i<TM;++i)
#pragma unroll
                for (int j=0;j<TN;++j) acc[i][j]+=av[i]*bv[j];
        }
        __syncthreads();
    }
#pragma unroll
    for (int i=0;i<TM;++i) {
        const int r=br+tr+i;
#pragma unroll
        for (int j=0;j<TN;++j) {
            const int c=bc+tc+j;
            if (r<M && c<N) {
                const int x=r*N+c;
                C[x]=alpha*acc[i][j]+beta*C[x];
            }
        }
    }
}
}  // namespace

extern "C" cudaError_t launch_candidate_gemm(
    const float *A, const float *B, float *C, float alpha, float beta,
    int M, int N, int K, cudaStream_t stream) {
    if (M<=0 || N<=0 || K<=0) return cudaErrorInvalidValue;
    candidate_kernel<<<dim3((N+BN-1)/BN,(M+BM-1)/BM),dim3(TX,TY),0,stream>>>(
        A,B,C,alpha,beta,M,N,K);
    return cudaGetLastError();
}
