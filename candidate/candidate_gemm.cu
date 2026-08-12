#include "candidate_api.h"

namespace {
constexpr int BM=128, BN=64, BK=32, TX=16, TY=16, TM=8, TN=4;

template <bool UnitAlphaZeroBeta, bool FullTiles>
__global__ void candidate_kernel(const float *__restrict__ A,
                                 const float *__restrict__ B,
                                 float *__restrict__ C, float alpha, float beta,
                                 int M, int N, int K) {
    __shared__ float As[BM][BK+1];
    __shared__ float Bs[BK][BN];
    const int tid=threadIdx.y*TX+threadIdx.x;
    const int br=blockIdx.y*BM, bc=blockIdx.x*BN;
    const int tr=threadIdx.y*TM, tc=threadIdx.x*TN;
    float acc[TM][TN]={};
    for (int k0=0; k0<K; k0+=BK) {
        for (int x=tid; x<BM*(BK/4); x+=TX*TY) {
            const int r=x/(BK/4), q=x%(BK/4), gr=br+r;
            float4 v;
            if constexpr (FullTiles)
                v=*reinterpret_cast<const float4*>(A+gr*K+k0+4*q);
            else
                v=(gr<M && k0+4*q+3<K)
                    ? *reinterpret_cast<const float4*>(A+gr*K+k0+4*q)
                    : make_float4(0.0f,0.0f,0.0f,0.0f);
            As[r][4*q+0]=v.x;
            As[r][4*q+1]=v.y;
            As[r][4*q+2]=v.z;
            As[r][4*q+3]=v.w;
        }
        for (int x=tid; x<BK*(BN/4); x+=TX*TY) {
            const int k=x/(BN/4), q=x%(BN/4), gc=bc+4*q;
            float4 v;
            if constexpr (FullTiles)
                v=*reinterpret_cast<const float4*>(B+(k0+k)*N+gc);
            else
                v=(k0+k<K && gc+3<N)
                    ? *reinterpret_cast<const float4*>(B+(k0+k)*N+gc)
                    : make_float4(0.0f,0.0f,0.0f,0.0f);
            *reinterpret_cast<float4*>(&Bs[k][4*q])=v;
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
    if constexpr (UnitAlphaZeroBeta) {
        if constexpr (FullTiles) {
#pragma unroll
            for (int i=0;i<TM;++i) {
                const int x=(br+tr+i)*N+bc+tc;
                *reinterpret_cast<float4*>(C+x)=
                    make_float4(acc[i][0],acc[i][1],acc[i][2],acc[i][3]);
            }
        } else {
#pragma unroll
            for (int i=0;i<TM;++i) {
                const int r=br+tr+i;
#pragma unroll
                for (int j=0;j<TN;++j) {
                    const int c=bc+tc+j;
                    if (r<M && c<N) C[r*N+c]=acc[i][j];
                }
            }
        }
    } else {
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
}
}  // namespace

extern "C" cudaError_t launch_candidate_gemm(
    const float *A, const float *B, float *C, float alpha, float beta,
    int M, int N, int K, cudaStream_t stream) {
    if (M<=0 || N<=0 || K<=0) return cudaErrorInvalidValue;
    const dim3 grid((N+BN-1)/BN,(M+BM-1)/BM), block(TX,TY);
    const bool full=(M%BM==0 && N%BN==0 && K%BK==0);
    if (alpha==1.0f && beta==0.0f) {
        if (full)
            candidate_kernel<true,true><<<grid,block,0,stream>>>(A,B,C,alpha,beta,M,N,K);
        else
            candidate_kernel<true,false><<<grid,block,0,stream>>>(A,B,C,alpha,beta,M,N,K);
    } else {
        if (full)
            candidate_kernel<false,true><<<grid,block,0,stream>>>(A,B,C,alpha,beta,M,N,K);
        else
            candidate_kernel<false,false><<<grid,block,0,stream>>>(A,B,C,alpha,beta,M,N,K);
    }
    return cudaGetLastError();
}
