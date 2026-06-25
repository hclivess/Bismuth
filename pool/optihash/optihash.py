# optihash.py v 0.30 to be used with Python3.5 or better
# Optimized CPU-miner for Optipoolware based pool mining only
# Copyright Hclivess, Primedigger, Maccaspacca, SylvainDeaure 2017
# .

import time, socks, sys, os, math
from multiprocessing import Process, freeze_support, Queue
from random import getrandbits
from hashlib import sha224, blake2b   # hf2: dual-algo Heavy3 inner hash (sha224 pre-fork, blake2b post-fork)

# Resolve the node's modernized `connections` + `mining_heavy3` from the repo root when the miner runs
# in-repo (pool/optihash/ -> repo root). Standalone miners keep these modules alongside the binary.
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
import connections
import mining_heavy3 as mining

__version__ = '0.3.1'

# load config
lines = [line.rstrip('\n') for line in open('miner.txt')]
for line in lines:
    if "port=" in line:
        port = line.split('=')[1]
    if "mining_ip=" in line:
        mining_ip_conf = line.split('=')[1]
    if "mining_threads=" in line:
        mining_threads_conf = line.split('=', 1)[1].strip()   # was str.strip(charset) — a char-set bug
    if "tor=" in line:
        tor_conf = int(line.split('=', 1)[1].strip())          # was str.strip(charset) — a char-set bug
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


# NOTE: the old standalone diffme() + bin_convert_orig() were removed — they were dead (their only call
# sites were commented out) and hardcoded sha224, a post-hf2 wrong-algo footgun. The authoritative diff
# check is mining_heavy3.diffme_heavy3(..., new_pow=) (the SAME function the node consensus uses).


def miner(q, pool_address, db_block_hash, diff, mining_condition, netdiff, hq, thr, dh, cb_prefix="", new_pow=False):
    process_mmap = False
    if not mining.RND_LEN:
        mining.mining_open()
        process_mmap = True
    try:
        tries = 0
        try_arr = [('%0x' % getrandbits(32)) for i in range(nonce_time*hashcount)]
        address = pool_address
        # hf2 (doc/18-D): the Heavy3 inner hash modernises sha224 -> blake2b (28-byte digest) at the fork.
        # Computed once here so the hot pre-filter below stays branch-free. Mirrors mining_heavy3.diffme_heavy3.
        _pow_digest = (lambda b: blake2b(b, digest_size=28).digest()) if new_pow else (lambda b: sha224(b).digest())
        timeout = time.time() + nonce_time
        h1 = 0   # ensure hq.put(h1) below is always defined, even if the loop never produces a rate
        # print(pool_address)
        while time.time() < timeout:
            try:
                t1 = time.time()
                tries = tries + 1
                # generate the "address" of a random backyard that we will sample in this try
                seed = ('%0x' % getrandbits(128-32))
                # this part won't change, so concat once only. hf2: fold the fork-signal prefix (cb_prefix)
                # into the PoW input so it matches the node's openfield = cb_prefix+seed+nonce (miner.py:86-88).
                prefix = pool_address + cb_prefix + seed
                # This is where the actual hashing takes place (dual-algo via _pow_digest: sha224 / blake2b)
                possibles = [nonce for nonce in try_arr if
                             mining_condition in (mining.anneal3(mining.MMAP, int.from_bytes(
                                 _pow_digest((prefix + nonce + db_block_hash).encode("utf-8")), 'big')))]
                # hash rate calculation
                try:
                    t2 = time.time()
                    h1 = int(((nonce_time*hashcount) / (t2 - t1))/1000)
                except Exception as e:
                    h1 = 1
                if possibles:
                    # print(possibles)
                    for nonce in possibles:
                        # add the seed back to get a full 128 bits nonce; hf2: prepend cb_prefix so the
                        # submitted nonce IS the coinbase openfield (cb_prefix+seed+nonce) the node expects.
                        nonce = cb_prefix + seed + nonce
                        # full-precision recheck — same dual-algo as the pre-filter via new_pow=
                        xdiffx = mining.diffme_heavy3(address, nonce, db_block_hash, new_pow=new_pow)
                        if xdiffx < diff:
                            pass
                        else:
                            print("Thread {} solved work in {} cycles - YAY!".format(q, tries))
                            wname = "{}{}".format(mname, str(q))
                            print("{} running at {} kh/s".format(wname,str(h1)))
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
                # DON'T re-raise: a stray iteration error must not kill the worker for the rest of
                # nonce_time and skip hq.put below — that previously deadlocked runit()'s hq.get().
                print("Miner: worker iteration error: {}".format(e))
                time.sleep(0.1)
        hq.put(str(h1))
    finally:
        if process_mmap:
            mining.mining_close()


def runit():
    connected = 0
    dh = 0
    hq = Queue()

    while True:
        try:

            s = socks.socksocket()
            if tor_conf == 1:
                s.setproxy(socks.PROXY_TYPE_SOCKS5, "127.0.0.1", 9050)
            s.connect((mining_ip_conf, int(port)))  # connect to pool
            connections.send(s, "getwork", 10)
            work_pack = connections.receive(s, 10)
            wp = work_pack[-1]
            db_block_hash = (wp[0])
            diff = int((wp[1]))
            paddress = (wp[2])
            netdiff = int((wp[3]))
            # hf2: APPEND-ONLY fields (a pre-hf2 4-tuple pool still parses) — the fork-signal coinbase
            # prefix to mine into the openfield, and whether the blake2b PoW is active for this block.
            cb_prefix = wp[4] if len(wp) > 4 else ""
            new_pow = bool(wp[5]) if len(wp) > 5 else False
            s.close()

            diff_hex = math.floor((diff / 8) - 1)
            mining_condition = db_block_hash[0:diff_hex]

            instances = range(int(mining_threads_conf))
            thr = int(mining_threads_conf)

            procs = []
            for q in instances:
                p = Process(target=miner, args=(str(q + 1), paddress, db_block_hash, diff, mining_condition,  netdiff, hq, thr, dh, cb_prefix, new_pow))
                p.daemon = True
                p.start()
                procs.append(p)   # was: only the LAST p was kept, so join/terminate ran on it N times
            print("{} miners searching for solutions at difficulty {} and condition {}".format(mining_threads_conf,str(diff),str(mining_condition)))

            time.sleep(nonce_time)

            for p in procs:
                p.join(timeout=5)
                p.terminate()

            # timeout-bounded so a dead/exited worker that never put a rate can't block runit() forever
            results = []
            for _ in procs:
                try:
                    results.append(int(hq.get(timeout=5)))
                except Exception:
                    results.append(0)
            dh = sum(results)
            print("Current total hash rate is {} kh/s".format(str(dh)))

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
