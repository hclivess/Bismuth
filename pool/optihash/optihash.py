# optihash.py v 0.30 to be used with Python3.5 or better
# Optimized CPU-miner for Optipoolware based pool mining only
# Copyright Hclivess, Primedigger, Maccaspacca, SylvainDeaure 2017
# .

import time, sys, os, math, json, logging
import urllib.request
from multiprocessing import Process, freeze_support, Queue
from random import getrandbits
from hashlib import sha224, blake2b   # hf2: dual-algo Heavy3 inner hash (sha224 pre-fork, blake2b post-fork)

# The miner talks to the POOL over HTTP now (no socket / connections). It still needs the node's
# `mining_heavy3` (the Heavy3 PoW); resolve it from the repo root when run in-repo (pool/optihash/ ->
# repo root). Standalone miners keep mining_heavy3 + the Heavy3 binary alongside the executable.
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
import mining_heavy3 as mining

__version__ = '0.3.1'

# load config (miner.toml — stdlib tomllib; typed, resolved next to this script)
import tomllib
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "miner.toml"), "rb") as _f:
    _mc = tomllib.load(_f)
port = _mc["port"]
mining_ip_conf = _mc["mining_ip"]
mining_threads_conf = _mc["mining_threads"]
tor_conf = int(_mc.get("tor", 0))
self_address = _mc["miner_address"]
nonce_time = int(_mc["nonce_time"])
mname = _mc["miner_name"]
hashcount = int(_mc["hashcount"])
# load config

# Logger (no raw print anywhere). Console output for the miner; worker processes inherit this on fork,
# and re-run this on spawn (Windows), so every process logs consistently.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
app_log = logging.getLogger("optihash")

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
                    for nonce in possibles:
                        # add the seed back to get a full 128 bits nonce; hf2: prepend cb_prefix so the
                        # submitted nonce IS the coinbase openfield (cb_prefix+seed+nonce) the node expects.
                        nonce = cb_prefix + seed + nonce
                        # full-precision recheck — same dual-algo as the pre-filter via new_pow=
                        xdiffx = mining.diffme_heavy3(address, nonce, db_block_hash, new_pow=new_pow)
                        if xdiffx < diff:
                            pass
                        else:
                            wname = "{}{}".format(mname, str(q))
                            app_log.info("Thread {} solved work in {} cycles ({} kh/s)".format(q, tries, h1))
                            block_timestamp = '%.2f' % time.time()
                            share = {"miner_address": self_address, "block_timestamp": block_timestamp,
                                     "nonce": nonce, "blockhash": db_block_hash, "netdiff": netdiff,
                                     "sdiff": xdiffx, "rate": dh, "worker_base": mname,
                                     "workers": thr, "worker_num": str(q)}
                            app_log.info("Submitting solution from {}".format(wname))
                            tries = 0
                            # submit the share to the pool over HTTP (was the socket 'block' command)
                            try:
                                _req = urllib.request.Request(
                                    "http://%s:%s/share" % (mining_ip_conf, port),
                                    data=json.dumps(share).encode("utf-8"),
                                    headers={"Content-Type": "application/json"}, method="POST")
                                with urllib.request.urlopen(_req, timeout=10) as _r:
                                    _resp = json.load(_r)
                                app_log.info("Solution accepted by pool: {}".format(_resp))
                            except Exception as e:
                                app_log.error("Could not submit solution to pool: {}".format(e))
            except Exception as e:
                # DON'T re-raise: a stray iteration error must not kill the worker for the rest of
                # nonce_time and skip hq.put below — that previously deadlocked runit()'s hq.get().
                app_log.warning("Worker iteration error: {}".format(e))
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

            # GET work from the pool over HTTP (was the socket 'getwork' command)
            with urllib.request.urlopen("http://%s:%s/work" % (mining_ip_conf, port), timeout=10) as _r:
                wp = json.load(_r)
            db_block_hash = wp["blockhash"]
            diff = int(wp["diff"])
            paddress = wp["pool_address"]
            netdiff = int(wp["netdiff"])
            # hf2: the fork-signal coinbase prefix to fold into the openfield + whether blake2b PoW is active
            cb_prefix = wp.get("cb_prefix", "")
            new_pow = bool(wp.get("new_pow", False))

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
            app_log.info("{} miners searching for solutions at difficulty {} and condition {}".format(mining_threads_conf, str(diff), str(mining_condition)))

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
            app_log.info("Current total hash rate is {} kh/s".format(str(dh)))

        except Exception as e:
            app_log.error("Unable to reach pool ({}); check your connection or IP settings".format(e))
            time.sleep(1)


if __name__ == '__main__':
    freeze_support()  # must be this line, don't move ahead

    mining.mining_open()
    try:
        runit()
    finally:
        mining.mining_close()
