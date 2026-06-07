# optihash.py v 0.31-cuda to be used with Python3.5 or better
# CUDA GPU-miner for Optipoolware based pool mining only
# Copyright Hclivess, Primedigger, Maccaspacca, SylvainDeaure 2017
# kbk and geho2, 2021
# .

import time, socks, connections, sys, os, math
from multiprocessing import Process, freeze_support, Queue
from random import getrandbits
from hashlib import sha224

import mining_heavy3 as mining

#Added by kbk
import bis # CUDA code
from datetime import datetime

def cuda_to_nonce_arr(string):
        elements = string.splitlines()
        newEl = []
        for el in elements:
                if len(el) > 30:
                        newEl.append(el)
        return newEl
#eo added by kbk

__version__ = '0.3.1-cuda'

# load config
lines = [line.rstrip('\n') for line in open('miner.txt')]
for line in lines:
    if "port=" in line:
        port = line.split('=')[1]
    if "mining_ip=" in line:
        mining_ip_conf = line.split('=')[1]
    if "mining_threads=" in line:
        mining_threads_conf = line.strip('mining_threads=')
    if "tor=" in line:
        tor_conf = int(line.strip('tor='))
    if "miner_address=" in line:
        self_address = line.split('=')[1]
    if "nonce_time=" in line:
        nonce_time = int(line.split('=')[1])
    if "miner_name=" in line:
        mname = line.split('=')[1]
    if "hashcount=" in line:
        hashcount = int(line.split('=')[1])

# load config

bin_format_dict = dict((x, format(ord(x), '8b').replace(' ', '0')) for x in '0123456789abcdef')


def bin_convert(string):
    return ''.join(bin_format_dict[x] for x in string)


def bin_convert_orig(string):
    return ''.join(format(ord(x), '8b').replace(' ', '0') for x in string)


def diffme(pool_address, nonce, db_block_hash):
    # minimum possible diff
    diff = 60
    # will return 0 for diff < 60
    diff_result = 0
    mining_hash = bin_convert(sha224((pool_address + nonce + db_block_hash).encode("utf-8")).hexdigest())
    mining_condition = bin_convert(db_block_hash)
    while mining_condition[:diff] in mining_hash:
        diff_result = diff
        diff += 1
    return diff_result


def miner(q, pool_address, db_block_hash, diff, mining_condition, netdiff, hq, thr, dh, bisCuda): # Added by kbk
    process_mmap = False
    if not mining.RND_LEN:
        mining.mining_open()
        process_mmap = True
    try:
        tries = 0
        try_arr = [('%0x' % getrandbits(32)) for i in range(nonce_time*hashcount)]
        address = pool_address
        timeout = time.time() + nonce_time
        while time.time() < timeout:
            try:
                t1 = time.time()
                tries = tries + 1

                # Added by kbk, wait for nonces
                possibles=[]
                time.sleep(0.25)

                nonces_cu = bisCuda.ValidNoncesGet()
                possibles = cuda_to_nonce_arr(nonces_cu)
                #eo added by kbk

                if possibles:
                    for nonce in possibles:
                        xdiffx = mining.diffme_heavy3(address, nonce, db_block_hash)
                        if xdiffx < diff:
                            print("---> Diff too low in nonce from GPU <----")
                            pass
                        else:
                            print("Thread {} solved work in {} cycles - YAY!".format(q, tries))
                            wname = "{}{}".format(mname, str(q))
                            block_send = []
                            del block_send[:]  # empty
                            block_timestamp = '%.2f' % time.time()
                            block_send.append((block_timestamp, nonce, db_block_hash, netdiff, xdiffx, dh, mname, thr, str(q)))
                            print("Sending solution: {}".format(block_send))
                            tries = 0
                            # submit mined nonce to pool
                            try:
                                s1 = socks.socksocket()
                                if tor_conf == 1:
                                    s1.setproxy(socks.PROXY_TYPE_SOCKS5, "127.0.0.1", 9050)
                                s1.connect((mining_ip_conf, int(port)))  # connect to pool
                                print("Miner: connected to pool, proceeding to submit solution")
                                connections.send(s1, "block", 10)
                                connections.send(s1, self_address, 10)
                                connections.send(s1, block_send, 10)
                                print("Miner: solution submitted to pool")
                                time.sleep(0.2)
                                s1.close()

                            except Exception as e:
                                print("Miner: Could not submit solution to pool")
                                pass
            except Exception as e:
                print(e)
                time.sleep(0.1)
                raise
    finally:
        if process_mmap:
            mining.mining_close()


def runit():
    connected = 0
    dh = 0
    hq = Queue()

    #Added by kbk
    bisCuda = bis.BisCuda()
    db_block_hash_previous = " "
    thr = 1
    #Eo added by kbk

    while True:
        try:
            # Added by kbk
            start_time = datetime.now() #Timing hashrate
            # Eo added by kbk

            s = socks.socksocket()
            if tor_conf == 1:
                s.setproxy(socks.PROXY_TYPE_SOCKS5, "127.0.0.1", 9050)
            s.connect((mining_ip_conf, int(port)))  # connect to pool
            connections.send(s, "getwork", 10)
            work_pack = connections.receive(s, 10)
            db_block_hash = (work_pack[-1][0])
            diff = int((work_pack[-1][1]))
            paddress = (work_pack[-1][2])
            netdiff = int((work_pack[-1][3]))
            s.close()

            diff_hex = math.floor((diff / 8) - 1)
            mining_condition = db_block_hash[0:diff_hex]

            print("{} miners searching for solutions at difficulty {} and condition {}".format(mining_threads_conf,str(diff),str(mining_condition)))

            #Added by kbk
            if db_block_hash != db_block_hash_previous:
                    bisCuda.Update(paddress, db_block_hash, mining_condition)
                    bisCuda.ValidNoncesGet() #Clear buffer
                    db_block_hash_previous = db_block_hash
                    print("Updated CUDA block hash")

            miner(1, paddress, db_block_hash, diff, mining_condition, netdiff, hq, thr, dh, bisCuda)
            stop_time = datetime.now()
            elapsed_time = stop_time - start_time
            nHashes = bisCuda.GetNumerOfHashesSinceLastCall()
            hPerSec = nHashes/elapsed_time.total_seconds()
            print("\n************************\n")
            print(hPerSec/1e6, "Mh/s")
            print("\n************************\n")
            #Eo added by kbk

        except Exception as e:
            print(e)
            print("Miner: Unable to connect to pool check your connection or IP settings.")
            time.sleep(1)


if __name__ == '__main__':
    freeze_support()  # must be this line, don't move ahead

    mining.mining_open()
    try:
        runit()
    finally:
        mining.mining_close()
