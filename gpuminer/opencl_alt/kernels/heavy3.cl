// gpuminer/opencl_alt/kernels/heavy3.cl
// Bismuth Heavy3 GPU mining kernel — hf2 dual-algo (sha224 pre-fork / blake2b post-fork).
//
// Reproduces the consensus PoW mining_heavy3.diffme_heavy3 EXACTLY, on-device:
//   inner = blake2b(addr+nonce+blockhash, digest_size=28) if new_pow else sha224(...)   (224-bit)
//   anneal3: XOR the 7 little-endian-from-big-endian inner words with 7 consecutive 32-bit words of the
//            ~1GB heavy3a.bin map (uploaded to __global memory) at offset ((low32 & ~0x7) % RND_LEN)*4
//   bin_convert: each of the 56 hex chars -> its 8-bit ASCII binary (448 bits)
//   difficulty: longest prefix of bin_convert(db_block_hash) that is a SUBSTRING of those 448 bits
//
// VALIDATED on pocl (CPU OpenCL) against the Python consensus function: blake2b vs hashlib (2000 inputs),
// full Heavy3 vs diffme_heavy3 (sha224 + blake2b, thousands of inputs), and the mine_heavy3 emission
// (0 false-positives / 0 false-negatives over 262144 nonces). GPU-hardware confirmation pending (see
// gpuminer/opencl_alt/tests/). Endianness/encoding are load-bearing — see the consensus contract above.
//
// Kernels: mine_heavy3() = production miner; test_heavy3_dual() = correctness harness.

// blake2b (RFC 7693), unkeyed, digest_size=28 — the post-fork Heavy3 inner hash.
// Validated on pocl (CPU OpenCL) against hashlib.blake2b(digest_size=28). 64-bit (ulong) arithmetic.
// Input capped at 256 bytes (mining input address[:56]+nonce(<129)+blockhash(<=64) < 256 => <=2 blocks).

__constant ulong BLAKE2B_IV[8] = {
    0x6a09e667f3bcc908UL, 0xbb67ae8584caa73bUL, 0x3c6ef372fe94f82bUL, 0xa54ff53a5f1d36f1UL,
    0x510e527fade682d1UL, 0x9b05688c2b3e6c1fUL, 0x1f83d9abfb41bd6bUL, 0x5be0cd19137e2179UL };

__constant uchar BLAKE2B_SIGMA[12][16] = {
    { 0, 1, 2, 3, 4, 5, 6, 7, 8, 9,10,11,12,13,14,15},
    {14,10, 4, 8, 9,15,13, 6, 1,12, 0, 2,11, 7, 5, 3},
    {11, 8,12, 0, 5, 2,15,13,10,14, 3, 6, 7, 1, 9, 4},
    { 7, 9, 3, 1,13,12,11,14, 2, 6, 5,10, 4, 0,15, 8},
    { 9, 0, 5, 7, 2, 4,10,15,14, 1,11,12, 6, 8, 3,13},
    { 2,12, 6,10, 0,11, 8, 3, 4,13, 7, 5,15,14, 1, 9},
    {12, 5, 1,15,14,13, 4,10, 0, 7, 6, 3, 9, 2, 8,11},
    {13,11, 7,14,12, 1, 3, 9, 5, 0,15, 4, 8, 6, 2,10},
    { 6,15,14, 9,11, 3, 0, 8,12, 2,13, 7, 1, 4,10, 5},
    {10, 2, 8, 4, 7, 6, 1, 5,15,11, 9,14, 3,12,13, 0},
    { 0, 1, 2, 3, 4, 5, 6, 7, 8, 9,10,11,12,13,14,15},
    {14,10, 4, 8, 9,15,13, 6, 1,12, 0, 2,11, 7, 5, 3} };

static inline ulong rotr64(ulong x, uint n) { return (x >> n) | (x << (64 - n)); }

#define B2B_G(a,b,c,d,x,y) \
    do { a = a + b + x; d = rotr64(d ^ a, 32); c = c + d; b = rotr64(b ^ c, 24); \
         a = a + b + y; d = rotr64(d ^ a, 16); c = c + d; b = rotr64(b ^ c, 63); } while (0)

static void blake2b_compress(ulong h[8], const ulong m[16], ulong t0, ulong t1, int last) {
    ulong v[16];
    for (int i = 0; i < 8; i++) { v[i] = h[i]; v[i + 8] = BLAKE2B_IV[i]; }
    v[12] ^= t0; v[13] ^= t1;
    if (last) v[14] = ~v[14];
    for (int r = 0; r < 12; r++) {
        __constant uchar *s = BLAKE2B_SIGMA[r];
        B2B_G(v[0], v[4], v[ 8], v[12], m[s[ 0]], m[s[ 1]]);
        B2B_G(v[1], v[5], v[ 9], v[13], m[s[ 2]], m[s[ 3]]);
        B2B_G(v[2], v[6], v[10], v[14], m[s[ 4]], m[s[ 5]]);
        B2B_G(v[3], v[7], v[11], v[15], m[s[ 6]], m[s[ 7]]);
        B2B_G(v[0], v[5], v[10], v[15], m[s[ 8]], m[s[ 9]]);
        B2B_G(v[1], v[6], v[11], v[12], m[s[10]], m[s[11]]);
        B2B_G(v[2], v[7], v[ 8], v[13], m[s[12]], m[s[13]]);
        B2B_G(v[3], v[4], v[ 9], v[14], m[s[14]], m[s[15]]);
    }
    for (int i = 0; i < 8; i++) h[i] ^= v[i] ^ v[i + 8];
}

// Compute blake2b-28 over `in[0..inlen)`. Writes 28 bytes to `out`.
static void blake2b_28(const uchar *in, uint inlen, uchar *out) {
    ulong h[8];
    for (int i = 0; i < 8; i++) h[i] = BLAKE2B_IV[i];
    h[0] ^= 0x01010000UL ^ (ulong)28;            // unkeyed, fanout=depth=1, digest_size=28

    uchar buf[256];
    for (int i = 0; i < 256; i++) buf[i] = (i < (int)inlen) ? in[i] : 0;

    uint nblocks = (inlen + 127) / 128;
    if (nblocks == 0) nblocks = 1;
    for (uint b = 0; b < nblocks; b++) {
        ulong m[16];
        const uchar *p = buf + b * 128;
        for (int i = 0; i < 16; i++) {              // little-endian 64-bit words
            m[i] = ((ulong)p[i*8+0])       | ((ulong)p[i*8+1] << 8)  | ((ulong)p[i*8+2] << 16) |
                   ((ulong)p[i*8+3] << 24) | ((ulong)p[i*8+4] << 32) | ((ulong)p[i*8+5] << 40) |
                   ((ulong)p[i*8+6] << 48) | ((ulong)p[i*8+7] << 56);
        }
        int last = (b == nblocks - 1);
        ulong t0 = last ? (ulong)inlen : (ulong)((b + 1) * 128);
        blake2b_compress(h, m, t0, 0UL, last);
    }
    for (int i = 0; i < 28; i++) out[i] = (uchar)(h[i >> 3] >> (8 * (i & 7)));  // little-endian, first 28
}

// Test harness kernel: one work-item per input. ins is [N*256] bytes, lens[N], outs[N*28].

// anneal3 + bin_convert + substring-prefix difficulty — appended after blake2b.cl.
// Reproduces mining_heavy3.diffme_heavy3 exactly (validated on pocl vs the Python consensus fn).

// 28-byte big-endian view -> seven 32-bit words, word0 = least significant (raw[24..27] BE), like
// int.from_bytes(raw,'big') then repeatedly taking &0xffffffff and >>32.
static inline uint be32_word(const uchar *raw, int word) {  // word 0..6
    int o = 24 - 4 * word;
    return ((uint)raw[o] << 24) | ((uint)raw[o+1] << 16) | ((uint)raw[o+2] << 8) | (uint)raw[o+3];
}

static inline uchar hexchar(uint nib) { return (nib < 10) ? (uchar)('0' + nib) : (uchar)('a' + (nib - 10)); }

// Compute the Heavy3 difficulty for one (preimage -> digest) given the map. cond/cond_len = the
// mining_condition bit-string (bin_convert(db_block_hash)) and its length. RND_LEN = filesize/4.
static uint heavy3_diff(const uchar *digest /*28*/, __global const uchar *mmap, uint rnd_len,
                        __global const uchar *cond, uint cond_len) {
    uint w0 = be32_word(digest, 0);
    uint index = (uint)(((ulong)(w0 & ~0x7u) % (ulong)rnd_len) * 4UL);

    // 7 annealed 32-bit values, value[k] = word[k] ^ LE32(map[index + 4k]); res = hex8(v6)..hex8(v0)
    uint vals[7];
    for (int k = 0; k < 7; k++) {
        uint off = index + 4u * k;
        uint mw = (uint)mmap[off] | ((uint)mmap[off+1] << 8) | ((uint)mmap[off+2] << 16) | ((uint)mmap[off+3] << 24);
        vals[k] = be32_word(digest, k) ^ mw;
    }
    // 56-hex string res = hex8(vals[6]) + hex8(vals[5]) + ... + hex8(vals[0])
    uchar hexs[56];
    for (int k = 0; k < 7; k++) {
        uint v = vals[6 - k];                          // most significant group first
        for (int d = 0; d < 8; d++) hexs[k*8 + d] = hexchar((v >> (28 - 4*d)) & 0xf);
    }
    // bin_convert: each hex char -> its 8-bit ASCII code, MSB first -> 448 '0'/'1' chars
    uchar bits[448];
    for (int i = 0; i < 56; i++) {
        uchar c = hexs[i];
        for (int b = 0; b < 8; b++) bits[i*8 + b] = (uchar)('0' + ((c >> (7 - b)) & 1));
    }
    // diff = longest L (>=1) such that cond[0:L] is a substring of bits[0:448] (monotonic -> break on miss)
    uint diff = 0;
    uint maxL = cond_len < 448u ? cond_len : 448u;
    for (uint L = 1; L <= maxL; L++) {
        int found = 0;
        for (uint s = 0; s + L <= 448u; s++) {
            int match = 1;
            for (uint k = 0; k < L; k++) { if (bits[s + k] != cond[k]) { match = 0; break; } }
            if (match) { found = 1; break; }
        }
        if (found) diff = L; else break;
    }
    return diff;
}

// Test kernel: full diffme_heavy3 reproduction. ins[N*256], lens[N] (utf-8 preimage = addr+nonce+bh).

// Generic multi-block SHA-224 (pre-fork Heavy3 inner hash), for inputs up to 311 bytes (<=5 blocks).
// Big-endian words + big-endian 28-byte digest, matching hashlib.sha224. Appended after blake2b.cl.

__constant uint SHA224_H[8] = {
    0xc1059ed8u, 0x367cd507u, 0x3070dd17u, 0xf70e5939u,
    0xffc00b31u, 0x68581511u, 0x64f98fa7u, 0xbefa4fa4u };

__constant uint SHA256_K[64] = {
    0x428a2f98u,0x71374491u,0xb5c0fbcfu,0xe9b5dba5u,0x3956c25bu,0x59f111f1u,0x923f82a4u,0xab1c5ed5u,
    0xd807aa98u,0x12835b01u,0x243185beu,0x550c7dc3u,0x72be5d74u,0x80deb1feu,0x9bdc06a7u,0xc19bf174u,
    0xe49b69c1u,0xefbe4786u,0x0fc19dc6u,0x240ca1ccu,0x2de92c6fu,0x4a7484aau,0x5cb0a9dcu,0x76f988dau,
    0x983e5152u,0xa831c66du,0xb00327c8u,0xbf597fc7u,0xc6e00bf3u,0xd5a79147u,0x06ca6351u,0x14292967u,
    0x27b70a85u,0x2e1b2138u,0x4d2c6dfcu,0x53380d13u,0x650a7354u,0x766a0abbu,0x81c2c92eu,0x92722c85u,
    0xa2bfe8a1u,0xa81a664bu,0xc24b8b70u,0xc76c51a3u,0xd192e819u,0xd6990624u,0xf40e3585u,0x106aa070u,
    0x19a4c116u,0x1e376c08u,0x2748774cu,0x34b0bcb5u,0x391c0cb3u,0x4ed8aa4au,0x5b9cca4fu,0x682e6ff3u,
    0x748f82eeu,0x78a5636fu,0x84c87814u,0x8cc70208u,0x90befffau,0xa4506cebu,0xbef9a3f7u,0xc67178f2u };

static inline uint rotr32(uint x, uint n) { return (x >> n) | (x << (32 - n)); }
#define S224_S0(x) (rotr32(x,2) ^ rotr32(x,13) ^ rotr32(x,22))
#define S224_S1(x) (rotr32(x,6) ^ rotr32(x,11) ^ rotr32(x,25))
#define S224_s0(x) (rotr32(x,7) ^ rotr32(x,18) ^ ((x) >> 3))
#define S224_s1(x) (rotr32(x,17) ^ rotr32(x,19) ^ ((x) >> 10))

static void sha224_28(const uchar *in, uint inlen, uchar *out) {
    uint h[8];
    for (int i = 0; i < 8; i++) h[i] = SHA224_H[i];

    uchar buf[320];                                    // up to 5 64-byte blocks (inlen<=311)
    for (int i = 0; i < 320; i++) buf[i] = 0;
    for (uint i = 0; i < inlen; i++) buf[i] = in[i];
    buf[inlen] = 0x80;
    ulong bits = (ulong)inlen * 8;
    uint nblocks = (inlen + 9 + 63) / 64;              // room for 0x80 + 8-byte length
    uint mlen = nblocks * 64;
    for (int i = 0; i < 8; i++) buf[mlen - 1 - i] = (uchar)(bits >> (8 * i));  // big-endian length

    for (uint b = 0; b < nblocks; b++) {
        const uchar *p = buf + b * 64;
        uint w[64];
        for (int i = 0; i < 16; i++)
            w[i] = ((uint)p[i*4] << 24) | ((uint)p[i*4+1] << 16) | ((uint)p[i*4+2] << 8) | (uint)p[i*4+3];
        for (int i = 16; i < 64; i++)
            w[i] = S224_s1(w[i-2]) + w[i-7] + S224_s0(w[i-15]) + w[i-16];
        uint a=h[0],bb=h[1],c=h[2],d=h[3],e=h[4],f=h[5],g=h[6],hh=h[7];
        for (int i = 0; i < 64; i++) {
            uint t1 = hh + S224_S1(e) + ((e & f) ^ (~e & g)) + SHA256_K[i] + w[i];
            uint t2 = S224_S0(a) + ((a & bb) ^ (a & c) ^ (bb & c));
            hh=g; g=f; f=e; e=d+t1; d=c; c=bb; bb=a; a=t1+t2;
        }
        h[0]+=a; h[1]+=bb; h[2]+=c; h[3]+=d; h[4]+=e; h[5]+=f; h[6]+=g; h[7]+=hh;
    }
    for (int i = 0; i < 28; i++) out[i] = (uchar)(h[i >> 2] >> (24 - 8 * (i & 3)));  // big-endian, first 28
}

// Dual-algo Heavy3 difficulty: new_pow -> blake2b else sha224, then anneal3 + substring-prefix.
__kernel void test_heavy3_dual(__global const uchar *ins, __global const uint *lens, const uint new_pow,
                               __global const uchar *mmap, const uint rnd_len,
                               __global const uchar *cond, const uint cond_len,
                               __global uchar *digests, __global uint *diffs) {
    int g = get_global_id(0);
    uchar in[256], dg[28];
    for (int i = 0; i < 256; i++) in[i] = ins[g * 256 + i];
    if (new_pow) blake2b_28(in, lens[g], dg);
    else         sha224_28(in, lens[g], dg);
    for (int i = 0; i < 28; i++) digests[g * 28 + i] = dg[i];
    diffs[g] = heavy3_diff(dg, mmap, rnd_len, cond, cond_len);
}


// Production mining entry point: each work-item tests nonce = base + gid, emits winners whose Heavy3
// difficulty >= min_diff. nonce string hashed = cb_prefix + "%016x"%(base+gid); preimage = prefix + that
// + suffix, where prefix = address||cb_prefix and suffix = db_block_hash (so the host reconstructs the
// submitted nonce as cb_prefix + "%016x"%nonce). Validated on pocl vs mining_heavy3.diffme_heavy3.

static inline uchar lhex(uint nib) { return (nib < 10) ? (uchar)('0' + nib) : (uchar)('a' + (nib - 10)); }

__kernel void mine_heavy3(
        __global const uchar *prefix, const uint prefix_len,   // address || cb_prefix
        __global const uchar *suffix, const uint suffix_len,   // db_block_hash
        const ulong base, const uint new_pow,
        __global const uchar *mmap, const uint rnd_len,
        __global const uchar *cond, const uint cond_len, const uint min_diff,
        volatile __global uint *found_count, __global ulong *found_nonces,
        __global uint *found_diffs, const uint max_found) {
    ulong nonce = base + (ulong)get_global_id(0);
    uchar in[256];
    uint p = 0;
    for (uint i = 0; i < prefix_len; i++) in[p++] = prefix[i];
    for (int d = 0; d < 16; d++) in[p++] = lhex((uint)((nonce >> (60 - 4*d)) & 0xf));  // "%016x"
    for (uint i = 0; i < suffix_len; i++) in[p++] = suffix[i];

    uchar dg[28];
    if (new_pow) blake2b_28(in, p, dg); else sha224_28(in, p, dg);
    uint diff = heavy3_diff(dg, mmap, rnd_len, cond, cond_len);

    if (diff >= min_diff) {
        uint idx = atomic_inc(found_count);
        if (idx < max_found) { found_nonces[idx] = nonce; found_diffs[idx] = diff; }
    }
}

