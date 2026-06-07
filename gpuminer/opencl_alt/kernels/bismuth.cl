#define SHR(v,b)  ((v)>>(b))
#define ROTR(v,b) rotate((uint)(v),(uint)(32-(b)))

#define S0(v) (ROTR((v),2)^ROTR((v),13)^ROTR((v),22))
#define S1(v) (ROTR((v),6)^ROTR((v),11)^ROTR((v),25))
#define S2(v) (ROTR((v),7)^ROTR((v),18)^SHR((v),3))
#define S3(v) (ROTR((v),17)^ROTR((v),19)^SHR((v),10))

#define CH(x,y,z)  (((x)&((y)^(z)))^(z))
#define MAJ(x,y,z) (((x)&((y)|(z)))|((y)&(z)))

#ifdef __ENDIAN_LITTLE__
#define ENDIAN_SWAP(n) (rotate(n & 0x00ff00ff, 24u)|(rotate(n, 8u) & 0x00ff00ff))
#else
#define ENDIAN_SWAP(n) (n)
#endif

#define SEMIMASK(X,N) (((X) - 0x01010101u) & ~(X) & (N))
#define NULLMASK(X) SEMIMASK((X), 0x80808080u)
//#define NULLMASK(X) (((X) - 0x01010101u) & ~(X) & 0x80808080u)

__constant uint K[] = { 0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5,
                        0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
                        0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
                        0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
                        0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc,
                        0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
                        0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7,
                        0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
                        0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
                        0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
                        0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3,
                        0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
                        0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5,
                        0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
                        0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
                        0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2 };

#define H0 ((uint8) ( 0xc1059ed8u, 0x367cd507u, 0x3070dd17u, 0xf70e5939u, \
                      0xffc00b31u, 0x68581511u, 0x64f98fa7u, 0xbefa4fa4u ))


#define SHA224_SIZE 28
#define SHA224_BLOCK 64
#define RET_SIZE 16
void expandBlock( uint* W );
void processLoop( uint* W, uint* h );
uint rand( ulong* seed );
ulong tohex( uint rnd );
ulong tohex1( uint rnd );
ulong tohex2( uint rnd );
uchar fromhex( uchar2 hex );
void fillMonoblocks( __global const uchar* header, __global const uchar* tail, uint* monoblocks );

inline void expandBlock( uint* W )
{
    #pragma unroll
    for( uint i = 0; i < 48; ++i )
    {
        W[i+16] = W[i] + S2(W[i+1]) + W[i+9] + S3(W[i+14]);
    }
}

inline void processLoop( uint* W, uint* h )
{
    uint H, G, F, E, D, C, B, A;

    A = h[0]; B = h[1]; C = h[2]; D = h[3];
    E = h[4]; F = h[5]; G = h[6]; H = h[7];

    #pragma unroll
    for( uint i = 0; i < 64; ++i )
    {
        uint t1 = H + S1(E) + CH(E,F,G) + K[i] + W[i];
        uint t2 = S0(A) + MAJ(A,B,C);
        H = G; G = F; F = E; E = D + t1;
        D = C; C = B; B = A; A = t1 + t2;
    }

    h[0] += A; h[1] += B; h[2] += C; h[3] += D;
    h[4] += E; h[5] += F; h[6] += G; h[7] += H;
}

inline uint rand( ulong* seed )
{
    uint2* cx = (uint2*) seed;
#ifdef __ENDIAN_LITTLE__
    uint c = (*cx).y, x = (*cx).x;
#else
    uint c = (*cx).x, x = (*cx).y;
#endif
    *seed = mad_sat( (ulong) x, 4294883355ul, (ulong) c );
    return x^c;
}

inline ulong tohex( uint rnd )
{
    uint2 ret = (uint2) rnd;
    ret &= (uint2) ( 0xf0f0f0f0, 0x0f0f0f0f );
    ret.x >>= 4;
    uchar8* pret = (uchar8*) &ret;
    *pret = shuffle( *pret, (uchar8)( 6, 2, 7, 3, 4, 0, 5, 1 ) );
    *pret = *pret > (uchar8)9 ? *pret + (uchar8)0x57 : *pret + (uchar8)0x30;
    return *((ulong*)&ret);
}

inline ulong tohex1( uint rnd )
{
    uint2 ret = (uint2) rnd;
    ret &= (uint2) ( 0xf0f0f0f0, 0x0f0f0f0f );
    ret.x >>= 4;

    uint2 mask = ((ret + (uint2)0x06060606u) >> 4) & (uint2) 0x01010101u;
    ret = mad_sat( (uint2) 0x27u, mask, ret | (uint2) 0x30303030u );

    uchar8* pret = (uchar8*) &ret;
    *pret = shuffle( *pret, (uchar8)( 6, 2, 7, 3, 4, 0, 5, 1 ) );

    return *((ulong*)&ret);
}

ulong tohex2( uint rnd )
{
    ulong ret = (ulong) rnd;

    ret = ((ret & 0x000000000000fffful) << 32) | (ret >> 16);
    ret = ((ret & 0x0000ff000000ff00ul) << 8)  | (ret & 0x000000ff000000fful);
    ret = ((ret & 0x00f000f000f000f0ul) << 4)  | (ret & 0x000f000f000f000ful);

    ulong mask = ((ret + 0x0606060606060606ul) >> 4) & 0x0101010101010101ul;
    ret = mad_sat( 0x27ul, mask, ret | 0x3030303030303030ul );

    return ret;
}

uchar fromhex( uchar2 hex )
{
    hex = hex > (uchar2) 0x39 ? hex - (uchar2)0x57 : hex - (uchar2) 0x30;
    return (hex.x << 4) + hex.y;
}

void fillMonoblocks( __global const uchar* header, __global const uchar* tail, uint* monoblocks )
{
    __global uint* h4 = (__global uint*) header;
    __global uint* t4 = (__global uint*) tail;
    for( uint i = 0; i < 14; ++i )
    {
        monoblocks[ i ] = ENDIAN_SWAP( h4[ i ] );
    }

    for( uint i = 70; i < 80; ++i )
    {
        monoblocks[ i ] = ENDIAN_SWAP( *t4 );
        ++t4;
    }

    for( uint i = 128; i < 132; ++i )
    {
        monoblocks[ i ] = ENDIAN_SWAP( *t4 );
        ++t4;
    }

    monoblocks[ 132 ] = 0x80000000;
    for( uint i = 133; i < 143; ++i )
    {
        monoblocks[ i ] = 0;
    }

    monoblocks[ 143 ] = 1152; // 144 bytes
}

// header size = 56
// tail size = 56
// random size = 32
__kernel
void bismuth( __global const uchar* header, __global const uchar* tail,
              __global ulong* seed, uint hashcnt, uint searchKey,
              __global uint* retCnt,
              __global uint4* ret, __global uchar* retMap )
{
    //printf( "->  in %d\n", __LINE__ );
    uint monoblocks[ 3 * SHA224_BLOCK ];
    uint8 baseh = H0;

    fillMonoblocks( header, tail, monoblocks );
    ulong lseed = seed[ get_global_id( 0 ) ];

#ifdef BISMUTH_FULL_GPU_CHECK
    uchar fullkey[8];
    uint keysize = clamp( searchKey >> 4, 3u, 8u );
    __global const uchar2* t2 = (__global const uchar2*)tail;
    for( uint i = 0; i < keysize; ++i )
    {
        fullkey[i] = fromhex( t2[i] );
    }

    uint maxstart = SHA224_SIZE - keysize - ((searchKey & 0xf)?1 : 0);
    uint end = (searchKey > 64)? 6 : 7;
    uchar4 tk = (uchar4)fullkey[0];
    uint fk0 = *((uint*)&tk);
#ifdef SEARCH_KEY_OVER_5
    // Is faster compare fullkey[4] instead fullkey[1].
    // However, today's minimun is 3. If the mininum raise to 5, we could change this.
    tk = (uchar4)fullkey[4];
    uint fk4 = *((uint*)&tk);
#else
    tk = (uchar4)fullkey[1];
    uint fk1 = *((uint*)&tk);
#endif
#else
    uint key = ENDIAN_SWAP( searchKey );
#endif

    // Zero return
    ret[ get_global_id( 0 ) ] = (uint4)0;
    retMap[ get_global_id( 0 ) ] = 0;

    uint4 nonce;
    nonce.x = rand( &lseed );
    ulong* pmono = (ulong*)(monoblocks + 14);
    *pmono = tohex( nonce.x );
    pmono = (ulong*)(monoblocks + 64);

    expandBlock( monoblocks );
    expandBlock( monoblocks + 2* SHA224_BLOCK );
    processLoop( monoblocks, (uint*)&baseh );

    for( uint cnt = 0; cnt < hashcnt; ++cnt )
    {
        uint8 h = baseh;
        uint* ph = (uint*) &h;

        nonce.y = rand( &lseed );
        nonce.z = rand( &lseed );
        nonce.w = rand( &lseed );

        pmono[ 0 ] = tohex1( nonce.y );
        pmono[ 1 ] = tohex1( nonce.z );
        pmono[ 2 ] = tohex1( nonce.w );

        expandBlock( monoblocks + SHA224_BLOCK );
        processLoop( monoblocks + SHA224_BLOCK, ph );
        processLoop( monoblocks + 2* SHA224_BLOCK, ph );

        uchar sha224[ SHA224_SIZE ];
        uint* psha = (uint*)sha224;
        #pragma unroll
        for( uint i = 0; i < 7; ++i )
        {
            psha[ i ] = ENDIAN_SWAP( ph[ i ] );
        }

        bool found = false;
#ifdef BISMUTH_FULL_GPU_CHECK
        for( uint k = 0; !found & (k < end); ++k )
        {
            uint nullmask = NULLMASK( psha[ k ] ^ fk0 );
#ifdef SEARCH_KEY_OVER_5
            if( SEMIMASK( psha[ k+1 ] ^ fk4, nullmask ) )
#else
            bool c1 = nullmask;
            bool c2 = SEMIMASK( psha[ k ] ^ fk1, nullmask << 8 );
            bool c3 = nullmask & 0x80000000u;
            if( c1 & (c2 | c3) )
#endif
            {
                do
                {
                    uint zeros = clz( nullmask );
                    nullmask &= 0x7fffffffu >> zeros;
                    uint n = mad24( k, 4u, (uint) (3u - (zeros >> 3)) );
                    if( n > maxstart )
                        break;

                    uint m = 0;
                    while( ( sha224[ n ] == fullkey[ m ] ) & ( m < keysize ) )
                    {
                        ++m;
                        ++n;
                    }

                    found = ( m == keysize );

                } while( !found & nullmask );
            }
        }
#else
        for( uint i = 0; i < SHA224_SIZE - 3; ++i )
        {
            uint* psha224 = (uint*)(sha224 + i);
            if( *psha224 == key )
            {
                found = true;
                break;
            }
        }
#endif

        if( found )
        {
            ret[ get_global_id( 0 ) ] = ENDIAN_SWAP( nonce );
            retMap[ get_global_id( 0 ) ] = 1;
            atomic_inc( retCnt );
            break;
        }
    }

    // set seed back (useful for next call)
    seed[ get_global_id( 0 ) ] = lseed;
}

