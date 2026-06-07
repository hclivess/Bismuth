#include "bis.h"

#define SHA2_SHFR(x, n)    (x >> n)
#define SHA2_ROTR(x, n)   ((x >> n) | (x << ((sizeof(x) << 3) - n)))
#define SHA2_ROTL(x, n)   ((x << n) | (x >> ((sizeof(x) << 3) - n)))
#define SHA2_CH(x, y, z)        ((x & (y ^ z)) ^ z) //form https://github.com/leocalm/Lyra/blob/master/GPU_attacks/yescryptCUDA/sha256.cu
#define SHA2_MAJ(x, y, z) ((x & (y | z)) | (y & z)) //form https://github.com/leocalm/Lyra/blob/master/GPU_attacks/yescryptCUDA/sha256.cu

char const hex_chars_small_h[] = { '0','1','2','3','4','5','6','7','8','9','a','b','c','d','e','f' };

__device__ __host__ __forceinline__ uint32_t rotr(uint32_t x, uint32_t n) {
#ifdef  __CUDA_ARCH__
        uint32_t result;
        asm("shf.r.wrap.b32  %0, %1, %2, %3;" : "=r"(result) : "r"(x), "r"(x), "r"(n));
        return result;
#else
        return SHA2_ROTR(x, n);
#endif
}

#define SHA256_F1(x) (rotr(x,  2) ^ rotr(x, 13) ^ rotr(x, 22))
#define SHA256_F2(x) (rotr(x,  6) ^ rotr(x, 11) ^ rotr(x, 25))
#define SHA256_F3(x) (rotr(x,  7) ^ rotr(x, 18) ^ SHA2_SHFR(x,  3))
#define SHA256_F4(x) (rotr(x, 17) ^ rotr(x, 19) ^ SHA2_SHFR(x, 10))

#define SHA2_PACK32(str, x)                     \
{                                               \
    *(x) =   ((uint32_t) *((str) + 3)      )    \
           | ((uint32_t) *((str) + 2) <<  8)    \
           | ((uint32_t) *((str) + 1) << 16)    \
           | ((uint32_t) *((str) + 0) << 24);   \
}

__device__ __constant__ uint32_t const sha256_k[64] = {
        0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5,
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
        0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2
};

uint32_t const sha256_k_cpu[64] = {
        0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5,
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
        0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2
};

template<class T, class T2>
__device__ __host__ __forceinline__ void memset_simple12(T* arr, T2 val) {
#pragma unroll 12
        for (uint32_t i = 0; i < 12; i++) {
                arr[i] = val;
        }
}

__device__ __host__ __forceinline__ void sha224_init(uint32_t* m_h) {
        m_h[0] = 0xc1059ed8;
        m_h[1] = 0x367cd507;
        m_h[2] = 0x3070dd17;
        m_h[3] = 0xf70e5939;
        m_h[4] = 0xffc00b31;
        m_h[5] = 0x68581511;
        m_h[6] = 0x64f98fa7;
        m_h[7] = 0xbefa4fa4;
}

__device__  __forceinline__ void transform(const uint32_t* message_int, uint32_t* m_h) {
        uint32_t w[64];
        uint32_t wv[8];
        uint32_t t1, t2;

#pragma unroll 16
        for (uint32_t j = 0; j < 16; j++) {
                w[j] = message_int[j];
        }

#pragma unroll 48
        for (uint32_t j = 16; j < 64; j++) {
                w[j] = SHA256_F4(w[j - 2]) + w[j - 7] + SHA256_F3(w[j - 15]) + w[j - 16];
        }
#pragma unroll 8
        for (uint32_t j = 0; j < 8; j++) {
                wv[j] = m_h[j];
        }
#pragma unroll 64
        for (uint32_t j = 0; j < 64; j++) {
                t1 = wv[7] + SHA256_F2(wv[4]) + SHA2_CH(wv[4], wv[5], wv[6])
                        + sha256_k[j] + w[j];
                t2 = SHA256_F1(wv[0]) + SHA2_MAJ(wv[0], wv[1], wv[2]);
                wv[7] = wv[6];
                wv[6] = wv[5];
                wv[5] = wv[4];
                wv[4] = wv[3] + t1;
                wv[3] = wv[2];
                wv[2] = wv[1];
                wv[1] = wv[0];
                wv[0] = t1 + t2;
        }
#pragma unroll 8
        for (uint32_t j = 0; j < 8; j++) {
                m_h[j] += wv[j];
        }
}

void transform_cpu(const uint32_t* message_int, uint32_t* m_h) {
        uint32_t w[64];
        uint32_t wv[8];
        uint32_t t1, t2;

        for (uint32_t j = 0; j < 16; j++) {
                w[j] = message_int[j];
        }

        for (uint32_t j = 16; j < 64; j++) {
                w[j] = SHA256_F4(w[j - 2]) + w[j - 7] + SHA256_F3(w[j - 15]) + w[j - 16];
        }

        for (uint32_t j = 0; j < 8; j++) {
                wv[j] = m_h[j];
        }

        for (uint32_t j = 0; j < 64; j++) {
                t1 = wv[7] + SHA256_F2(wv[4]) + SHA2_CH(wv[4], wv[5], wv[6])
                        + sha256_k_cpu[j] + w[j];
                t2 = SHA256_F1(wv[0]) + SHA2_MAJ(wv[0], wv[1], wv[2]);
                wv[7] = wv[6];
                wv[6] = wv[5];
                wv[5] = wv[4];
                wv[4] = wv[3] + t1;
                wv[3] = wv[2];
                wv[2] = wv[1];
                wv[1] = wv[0];
                wv[0] = t1 + t2;
        }

        for (uint32_t j = 0; j < 8; j++) {
                m_h[j] += wv[j];
        }
}

__device__  __forceinline__ void transform_sep_nonce(const uint32_t* message_int, const uint32_t* nonce, uint32_t* m_h) {
        uint32_t w[64];
        uint32_t wv[8];
        uint32_t t1, t2;

#pragma unroll 4
        for (int j = 0; j < 4; j++) {
                w[j] = message_int[j];
        }
#pragma unroll 10
        for (int j = 6; j < 16; j++) {
                w[j] = message_int[j];
        }

        w[4] = nonce[0];
        w[5] = nonce[1];

#pragma unroll 48
        for (int j = 16; j < 64; j++) {
                w[j] = SHA256_F4(w[j - 2]) + w[j - 7] + SHA256_F3(w[j - 15]) + w[j - 16];
        }
#pragma unroll 8
        for (int j = 0; j < 8; j++) {
                wv[j] = m_h[j];
        }
#pragma unroll 64
        for (int j = 0; j < 64; j++) {
                t1 = wv[7] + SHA256_F2(wv[4]) + SHA2_CH(wv[4], wv[5], wv[6])
                        + sha256_k[j] + w[j];
                t2 = SHA256_F1(wv[0]) + SHA2_MAJ(wv[0], wv[1], wv[2]);
                wv[7] = wv[6];
                wv[6] = wv[5];
                wv[5] = wv[4];
                wv[4] = wv[3] + t1;
                wv[3] = wv[2];
                wv[2] = wv[1];
                wv[1] = wv[0];
                wv[0] = t1 + t2;
        }
#pragma unroll 8
        for (int j = 0; j < 8; j++) {
                m_h[j] += wv[j];
        }
}

__device__  __forceinline__ void sha224_final(uint32_t* m_block, uint32_t* m_h) {
        memset_simple12(m_block + 16 / 4, 0);
        m_block[16 / 4] = m_block[16 / 4] | 0x80000000;
        m_block[60 / 4] = 1152;
        transform(m_block, m_h);
}

__device__  __forceinline__ void modNonce(const uint32_t* nonce, uint32_t* new_nonce, uint32_t const th_id) {
        char const hex_chars_small[] = { '0','1','2','3','4','5','6','7','8','9','a','b','c','d','e','f' };
        new_nonce[0] = 0;

        uint8_t chIdx = (uint8_t)(th_id & 0xF);
        uint32_t val = (uint32_t)hex_chars_small[chIdx];
        new_nonce[0] = new_nonce[0] | (val << 24);        // 0xXX000000

        chIdx = (uint8_t)((th_id >> 4) & 0xF);
        val = (uint32_t)hex_chars_small[chIdx];
        new_nonce[0] = new_nonce[0] | (val << 16); // 0xXXXX0000

        chIdx = (uint8_t)((th_id >> 8) & 0xF);
        val = (uint32_t)hex_chars_small[chIdx];
        new_nonce[0] = new_nonce[0] | (val << 8);  // 0xXXXXXX00

        chIdx = (uint8_t)((th_id >> 12) & 0xF);
        val = (uint32_t)hex_chars_small[chIdx];
        new_nonce[0] = new_nonce[0] | val;                // 0xXXXXXXXX


        new_nonce[1] = nonce[5] & 0x0000FFFF;
        chIdx = (uint8_t)((th_id >> 16) & 0xF);
        val = (uint32_t)hex_chars_small[chIdx];
        new_nonce[1] = new_nonce[1] | (val << 24);        // 0xXX00????

        chIdx = (uint8_t)((th_id >> 20) & 0xF);
        val = (uint32_t)hex_chars_small[chIdx];
        new_nonce[1] = new_nonce[1] | (val << 16);        // 0xXXXX????
}

void modNonceCpu(unsigned char* nonce, uint32_t const th_id) {
        unsigned char chIdx = (th_id & 0xF);
        uint8_t i = 24;

        chIdx = (uint8_t)(th_id & 0xF);
        nonce[i] = hex_chars_small_h[chIdx];      // 0xXX000000

        chIdx = (uint8_t)((th_id >> 4) & 0xF);
        i++;
        nonce[i] = hex_chars_small_h[chIdx]; // 0xXXXX0000

        chIdx = (uint8_t)((th_id >> 8) & 0xF);
        i++;
        nonce[i] = hex_chars_small_h[chIdx];  // 0xXXXXXX00

        chIdx = (uint8_t)((th_id >> 12) & 0xF);
        i++;
        nonce[i] = hex_chars_small_h[chIdx];              // 0xXXXXXXXX

        chIdx = (uint8_t)((th_id >> 16) & 0xF);
        i++;
        nonce[i] = hex_chars_small_h[chIdx];      // 0xXX00????

        chIdx = (uint8_t)((th_id >> 20) & 0xF);
        i++;
        nonce[i] = hex_chars_small_h[chIdx];      // 0xXXXX????
}

__device__ __forceinline__ void shiftLeft4Bytes7Ints(uint32_t* uintArr) {
#pragma unroll 6
        for (int i = 0; i < 6; i++) {
                uintArr[i] = uintArr[i] << 4;

                uintArr[i] = uintArr[i] | (uintArr[i + 1] >> 28);
        }
        uintArr[6] = uintArr[6] << 4;
}

__device__ __forceinline__ void anneal3(const uint32_t* map, uint32_t* hash) {
        int index = ((hash[6] & ~0x7) % RND_LEN) + 6;
#pragma unroll
        for (int i = 0; i < 7; ++i) {
                hash[i] ^= map[index - i];
        }
}

__global__ void sha224_find(const uint32_t* message_int, uint32_t* hash, uint32_t* mining_cond, const int32_t tail_length_in_chars, uint32_t* map, uint32_t* nonce_found_on_th_id) {
        uint32_t const one_pad_x_chars_lsb_constants_loc[] = { 0x0, 0xF, 0xFF, 0xFFF, 0xFFFF, 0xFFFFF, 0xFFFFFF, 0xFFFFFFF, 0xFFFFFFFF };
        uint32_t const th_id = blockIdx.x * blockDim.x + threadIdx.x;

        uint32_t nonce_local[2];
        __shared__ uint32_t message_int_16_to_36[20];
        uint32_t m_block[SHA224_256_BLOCK_SIZE / 4];
        uint32_t hash_local[8];
        uint32_t sum = 0;
        uint32_t rightPadOfHashForLastCondition = one_pad_x_chars_lsb_constants_loc[tail_length_in_chars];
        uint32_t mining_cond_local[N_32BITS_IN_MINING_CONDITION];


        if ((threadIdx.x > 15) && (threadIdx.x < 36)) {
                message_int_16_to_36[threadIdx.x - 16] = message_int[threadIdx.x];
        }
        __syncthreads();

        //Local copy of message
#pragma unroll 8
        for (int i = 0; i < 8; i++) { hash_local[i] = hash[i]; }

        //Randomize message by editing nonce
        modNonce((const uint32_t*)&message_int_16_to_36[0], nonce_local, th_id);

        //Update
        transform_sep_nonce((const uint32_t*)message_int_16_to_36, nonce_local, hash_local);

#pragma unroll 4
        for (int i = 0; i < 4; i++) {
                m_block[i] = message_int_16_to_36[16 + i];
        }
        //EO UPDATE

        //Final
        sha224_final(m_block, hash_local);


        //Anneal3
        anneal3(map, hash_local);
        //Eo anneal3

        //copy mining condition local
#pragma unroll 2
        for (uint32_t i = 0; i < 2; i++) { mining_cond_local[i] = mining_cond[i]; }

#pragma unroll 8
        for (int shifts = 0; shifts < 8; shifts++) {
#pragma unroll 6
                for (int h_i = 0; h_i < 6; h_i++) {
                        sum = 0;
                        sum = sum | (hash_local[h_i] ^ mining_cond_local[0]);
                        int h1 = (hash_local[h_i + 1] | rightPadOfHashForLastCondition);
                        sum = sum | (h1 ^ mining_cond_local[1]);
                        if (sum == 0) {
                                *nonce_found_on_th_id = th_id;
                        }
                }
                shiftLeft4Bytes7Ints(hash_local);
        }
}

std::string nonceFromThreadId(std::string nonce, uint32_t th_id) {
        unsigned char nonce_buf[32];
        for (int ii = 0; ii < 32; ii++) { nonce_buf[ii] = nonce[ii]; }
        modNonceCpu(nonce_buf, th_id);
        for (int ii = 0; ii < 32; ii++) { nonce[ii] = nonce_buf[ii]; }
        return nonce;
}

uint32_t StringToInts(std::string hex_str, uint32_t* ret, bool onePad = true) {
        //pad with 0's
        while (hex_str.length() % 8 != 0) {
                if (onePad) {
                        hex_str = hex_str + "f";
                }
                else {
                        hex_str = hex_str + "0";
                }
        }

        int len = 0;
        for (unsigned i = 0; i < hex_str.length(); i += 8) {
                std::string hex32_numb = hex_str.substr(i, 8);
                unsigned int x = std::stoul(hex32_numb, nullptr, 16);
                ret[len] = x;
                len++;
        }
        return len;
}

void cur(cudaError_t error) {
        if (error != cudaSuccess) {
                std::cout << cudaGetErrorString(error);
                throw std::runtime_error(cudaGetErrorString(error));
        }
}

int32_t BisCuda::GetNumDevices() {
        int32_t deviceCount = -1;
        cudaError_t err = cudaGetDeviceCount(&deviceCount);
        if (err == cudaSuccess)
                return deviceCount;

        if (err == cudaErrorInsufficientDriver) {
                int driverVersion = -1;
                cudaDriverGetVersion(&driverVersion);
                if (driverVersion == 0)
                        throw std::runtime_error{ "No CUDA driver found" };
                throw std::runtime_error{ "Insufficient CUDA driver: " + std::to_string(driverVersion) };
        }

        throw std::runtime_error{ cudaGetErrorString(err) };
}

void packMsg(const unsigned char* message, uint32_t* msg_int) {
        for (int j = 0; j < 144 / 4; j++) {
                SHA2_PACK32(&message[j << 2], &msg_int[j]);
        }
}

std::string BisCuda::GenerateNonce_DoNotCall() {
        std::string nonce = "";
        for (int i = 0; i < 32; i++) {

                nonce += hex_chars_small_h[rand() % 16];
        }
        return nonce;
}

void BisCuda::ResetAllDevices() {
        int nDev = GetNumDevices();
        for (int dev_id = 0; dev_id < nDev; dev_id++) {
                cur(cudaSetDevice(dev_id));
                cur(cudaDeviceReset());
        }
}

void BisCuda::Init() {
        n_devices = GetNumDevices();

        //Load MAP
        cur(cudaMallocHost(&map_host, map_size));
        printf("Loading heavy3a.bin into memory\n");
        FILE* in_file = fopen("heavy3a.bin", "rb");
        if (in_file != NULL) {
                fread(map_host, BIN_SIZE, 1, in_file);
                fclose(in_file);
                printf("Loaded heavy3a.bin");
        }
        else {
                printf("Could not load heavy3a.bin, exiting..");
                exit(0);
        }
        //EO load map

        //This is only first time "random" initial values, will be overwritten by the next call to Update
        Update("ddf74f55fb386d29037b010966d2424268a5efc6c8be2b8c6c9c9de9", "25293edc281c3de8ecd41047ea01288603c5aafa13f8f73c15b7871", "7cd4ab66b1a");

        for (uint32_t dev_id = 0; dev_id < n_devices; dev_id++) {
                InitDevice(dev_id);
        }
}

void BisCuda::InitDevice(uint32_t dev_id) {
        cur(cudaSetDevice(dev_id));

        cur(cudaMalloc(&mining_cond_uint_dev[dev_id], mining_cond_uint_size));
        cur(cudaMalloc(&nonce_found_on_th_id_dev[dev_id], sizeof(uint32_t)));
        cur(cudaMalloc(&hash_dev[dev_id], m_h_size));
        cur(cudaMalloc(&message_int_dev[dev_id], msg_int_size));

        cur(cudaMallocHost((void**)&nonce_found_on_th_id_host[dev_id], sizeof(uint32_t)));
        cur(cudaMallocHost((void**)&message_int_host[dev_id], msg_int_size));
        cur(cudaMallocHost((void**)&hash_host[dev_id], msg_int_size));

        //Map
        cur(cudaMalloc(&map_dev[dev_id], map_size));
        cur(cudaMemcpy(map_dev[dev_id], map_host, map_size, cudaMemcpyHostToDevice));

}

void BisCuda::DeletePointers() {
        for (uint32_t i = 0; i < n_devices; i++) {
                // Device
                cur(cudaFree(mining_cond_uint_dev[i]));
                cur(cudaFree(message_int_dev[i]));
                cur(cudaFree(hash_dev[i]));
                cur(cudaFree(nonce_found_on_th_id_dev[i]));

                // Host
                cur(cudaFreeHost(nonce_found_on_th_id_host[i]));
                cur(cudaFreeHost(message_int_host[i]));
                cur(cudaFreeHost(hash_host[i]));
        }
}

void BisCuda::Update(std::string address, std::string db_block_hash, std::string mining_condition_hex_string) {
        address_h = address;
        db_block_hash_h = db_block_hash;
        GenerateNonces();

        //Convert mining condition to int 32
        mining_cond_uint_host[0] = 0xFFFFFFFF;
        mining_cond_uint_host[1] = 0xFFFFFFFF;
        mining_condition_n_ints = StringToInts(mining_condition_hex_string, mining_cond_uint_host);
        tail_length_in_chars_host = 8 - (mining_condition_hex_string.length() % 8);
        if (tail_length_in_chars_host == 8) { tail_length_in_chars_host = 0; }

        for (uint32_t dev_id = 0; dev_id < n_devices; dev_id++) {
                shouldUpdateDevices[dev_id] = true;
        }
}

void BisCuda::UpdateDevice(uint32_t dev_id, bool set_device = true) {
        if (set_device) {
                cur(cudaSetDevice(dev_id));
        }

        //Update message with new noce (this must be done each loop in order to find new solutions on a new message)
        nonce_h[dev_id] = GetNonce();
        message_string[dev_id] = address_h + nonce_h[dev_id] + db_block_hash_h;

        if (shouldUpdateDevices[dev_id]) {
                cur(cudaMemcpy(mining_cond_uint_dev[dev_id], mining_cond_uint_host, mining_cond_uint_size, cudaMemcpyHostToDevice));
                shouldUpdateDevices[dev_id] = false;
        }

        packMsg((const unsigned char*)message_string[dev_id].c_str(), message_int_host[dev_id]);
        sha224_init(hash_host[dev_id]);
        transform_cpu(message_int_host[dev_id], hash_host[dev_id]);
        cur(cudaMemcpy(message_int_dev[dev_id], message_int_host[dev_id], msg_int_size, cudaMemcpyHostToDevice));
        cur(cudaMemcpy(hash_dev[dev_id], hash_host[dev_id], m_h_size, cudaMemcpyHostToDevice));
}

void BisCuda::StartAllGPUs() {
        run_threads = true;
        for (uint32_t dev_id = 0; dev_id < n_devices; dev_id++) {
                threads[dev_id] = std::thread(&BisCuda::LoopDeviceThread, this, dev_id);
        }
}

void BisCuda::LoopDeviceThread(uint32_t dev_id) {
        cur(cudaSetDevice(dev_id));
        while (run_threads) {
                //Update device each loop, in order to generate new nonces and messages
                UpdateDevice(dev_id, false);
                LoopDevice(dev_id, false);
                n_hashes_executed += N_THREADS;
        }
}

void BisCuda::LoopDevice(uint32_t dev_id, bool set_device = true) {
        if (set_device) {
                cur(cudaSetDevice(dev_id));
        }

        *nonce_found_on_th_id_host[dev_id] = 0xFFFFFFFF;
        cur(cudaMemcpy(nonce_found_on_th_id_dev[dev_id], nonce_found_on_th_id_host[dev_id], sizeof(uint32_t), cudaMemcpyHostToDevice));

        sha224_find << <N_BLOCKS, N_THREADS_PER_BLOCK >> > (message_int_dev[dev_id], hash_dev[dev_id], mining_cond_uint_dev[dev_id], tail_length_in_chars_host, map_dev[dev_id], nonce_found_on_th_id_dev[dev_id]);

        cudaDeviceSynchronize();

        cur(cudaMemcpy(nonce_found_on_th_id_host[dev_id], nonce_found_on_th_id_dev[dev_id], sizeof(uint32_t), cudaMemcpyDeviceToHost));

        if (*nonce_found_on_th_id_host[dev_id] != 0xFFFFFFFF) {
                std::string validNonce = nonceFromThreadId(nonce_h[dev_id], *nonce_found_on_th_id_host[dev_id]) + "\n";
                ValidNoncesAdd(validNonce);
                std::cout << "\nNonce found!" << std::flush;
        }
}
