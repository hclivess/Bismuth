#ifndef BIS_H
#define BIS_H
#include "cuda_runtime.h"
#include "device_launch_parameters.h"
#include "stdio.h"

#ifndef _WIN32
#include <sys/time.h>
#endif

#include <cstring>
#include <stdint.h>
#include <string>
#include <iostream>
#include <ctime>
#include <thread>
#include <mutex>
#include <stack>
#include <cstdlib>
#include <atomic>
#include <vector>
#include <chrono>

#define SHA224_256_BLOCK_SIZE 64  //512/8
#define SHA224_DIGEST_SIZE 28  //(224/8)
#define MAX_GPUS 10
#define N_32BITS_IN_MINING_CONDITION 2 //32bit*2 = 64bit max length on mining condition (This must be changed if difficulty if more that 128 bits)
#define N_THREADS_PER_BLOCK 512
#define N_BLOCKS  32768
#define N_THREADS (N_THREADS_PER_BLOCK * N_BLOCKS)

#define BIN_SIZE 1073741824
#define RND_LEN ( BIN_SIZE / 4 )

class BisCuda {
private:

        //Device
        uint32_t* mining_cond_uint_dev[MAX_GPUS];
        uint32_t* nonce_found_on_th_id_dev[MAX_GPUS];
        uint32_t* message_int_dev[MAX_GPUS];

        uint32_t* map_dev[MAX_GPUS];

        //Host
        uint32_t* map_host = NULL;  // Only one map file is needed for host
        uint32_t map_size = BIN_SIZE;

        uint32_t* nonce_found_on_th_id_host[MAX_GPUS]; //1 element
        uint32_t* message_int_host[MAX_GPUS]; //36 elements
        uint32_t msg_int_size = 36 * sizeof(uint32_t);
        uint32_t* hash_host[MAX_GPUS]; //8 elements
        uint32_t m_h_size = 8 * sizeof(uint32_t);
        uint32_t* hash_dev[MAX_GPUS];

        void SetPointersToNULL() {
                for (int i = 0; i < MAX_GPUS; i++) {
                        mining_cond_uint_dev[i] = NULL;
                        nonce_found_on_th_id_dev[i] = NULL;
                        message_int_dev[i] = NULL;
                        hash_dev[i] = NULL;

                        nonce_found_on_th_id_host[i] = NULL;
                        message_int_host[i] = NULL;
                        hash_host[i] = NULL;
                }
        }

        //Host variables
        std::thread threads[MAX_GPUS];
        bool run_threads;
        bool shouldUpdateDevices[MAX_GPUS];


        uint32_t n_devices; //Set in init

        std::string address_h;
        std::string db_block_hash_h;
        std::string nonce_h[MAX_GPUS];
        std::string message_string[MAX_GPUS];

        uint32_t mining_cond_uint_host[N_32BITS_IN_MINING_CONDITION];
        uint32_t mining_cond_uint_size = sizeof(uint32_t) * N_32BITS_IN_MINING_CONDITION;
        uint32_t mining_condition_n_ints;
        uint32_t tail_length_in_chars_host;

        std::mutex mtx_nonce_valid;
        std::mutex mtx_nonce;
        std::string valid_nonces; //DO not call directly!!

        std::stack<std::string> nonces_h_do_not_modify;
        std::atomic<uint64_t> n_hashes_executed{ 0 };

        void GenNoncesOnlyMutexCall() {
                if (nonces_h_do_not_modify.size() > MAX_GPUS * 200) { return; }
                while (nonces_h_do_not_modify.size() < (MAX_GPUS * 500)) {
                        nonces_h_do_not_modify.push(GenerateNonce_DoNotCall());
                }
        }

        void GenerateNonces() {
                mtx_nonce.lock();
                GenNoncesOnlyMutexCall();
                mtx_nonce.unlock();
        }

        std::string GetNonce() {
                mtx_nonce.lock();
                if (nonces_h_do_not_modify.size() == 0) {
                        GenNoncesOnlyMutexCall();
                }
                std::string n = nonces_h_do_not_modify.top();
                nonces_h_do_not_modify.pop();
                mtx_nonce.unlock();
                return n;
        }

        void ValidNoncesAdd(std::string nonces) {
                mtx_nonce_valid.lock();
                //Do not keep adding indefinitely. It is cleared by calling ValidNoncesGet
                if (valid_nonces.size() < 10000000) {
                        valid_nonces += nonces;
                }
                mtx_nonce_valid.unlock();
        }

        int32_t GetNumDevices();
        void InitDevice(uint32_t dev_id); //This is called from init for all devices
        void UpdateDevice(uint32_t dev_id, bool use_device_id); //If use_device_id is false, device is not set in method
        void LoopDevice(uint32_t dev_id, bool set_device);
        void LoopDeviceThread(uint32_t dev_id);
        void Init();
        void StartAllGPUs();
        void DeletePointers();

        std::string GenerateNonce_DoNotCall(); //Shold only be called from GenerateNonces() due to thread safety

public:

        BisCuda() {
#ifdef _WIN32
                srand((uint32_t)time(NULL)); //Psudo random generator to update seed
#else

                struct timeval  tv1;
                gettimeofday(&tv1, NULL);
                int seed_val = (int)tv1.tv_usec;
                srand(seed_val);
                std::cout << "Seeding with value tv_usec: " << std::to_string(seed_val) << "\n" << std::flush;
#endif

                GenerateNonces();
                SetPointersToNULL();
                Init();
                StartAllGPUs();
        }

        ~BisCuda() {
                run_threads = false;
                for (uint32_t i = 0; i < n_devices; i++) {
                        if (&threads[i] != NULL) {
                                threads[i].join();
                        }
                }
                DeletePointers();

                std::cout << "\nBisCuda exited\n" << std::flush;
        }

        void ResetAllDevices();
        void Update(std::string address, std::string db_block_hash, std::string mining_condition_hex_string);

        uint64_t GetNumerOfHashesSinceLastCall() {
                uint64_t h = n_hashes_executed;
                n_hashes_executed = 0;
                return h;
        }

        uint32_t ValidNoncesAvailable() {
                return (uint32_t)valid_nonces.length();
        }

        std::string ValidNoncesGet() {
                mtx_nonce_valid.lock();
                std::string valid_nonces_cpy = valid_nonces;
                valid_nonces.clear();
                mtx_nonce_valid.unlock();
                return valid_nonces_cpy;
        }
};
#endif
