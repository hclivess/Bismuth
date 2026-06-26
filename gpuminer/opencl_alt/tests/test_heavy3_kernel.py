"""Correctness test for the OpenCL Heavy3 mining kernel (gpuminer/opencl_alt/kernels/heavy3.cl).

Proves the kernel reproduces the consensus PoW (mining_heavy3.diffme_heavy3) EXACTLY for both inner-hash
eras, and that the production miner emits exactly the consensus winners:

  * test_heavy3_dual : blake2b (post-fork) AND sha224 (pre-fork) inner hash + anneal3 + difficulty, vs
                       diffme_heavy3(new_pow=...), digest- and difficulty-exact.
  * mine_heavy3      : nonce iteration + emission — every emitted nonce really meets the threshold per
                       consensus (no false positives) and no consensus winner is missed (no false negatives).

Runs on ANY OpenCL device. There is no GPU in CI, so this is validated on pocl (CPU OpenCL):
    python3 -m venv --system-site-packages venv && venv/bin/pip install pyopencl pocl-binary-distribution
    venv/bin/python -m pytest gpuminer/opencl_alt/tests/test_heavy3_kernel.py -q -s
Skipped automatically when no pyopencl / OpenCL platform is present.
"""
import os
import random
import sys

import pytest

np = pytest.importorskip("numpy")
cl = pytest.importorskip("pyopencl")

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
KERNEL = os.path.join(ROOT, "gpuminer", "opencl_alt", "kernels", "heavy3.cl")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _ctx():
    try:
        plats = cl.get_platforms()
    except Exception:
        plats = []
    if not plats:
        pytest.skip("no OpenCL platform available")
    return cl.create_some_context(interactive=False)


def _setup(seed):
    """Small monkeypatched Heavy3 map so the test is fast but uses the REAL anneal3 (mainnet path)."""
    import mining_heavy3 as mh
    rnd = random.Random(seed)
    MAP_SIZE = 1 << 20                       # RND_LEN = 2^18 (multiple of 8, like the real 1GB/2^28)
    mapbytes = bytes(rnd.getrandbits(8) for _ in range(MAP_SIZE))
    mh.MMAP = mapbytes
    mh.RND_LEN = MAP_SIZE // 4
    mh.heavy = True
    mh.is_regnet = False
    return mh, mapbytes, rnd


def _build(ctx):
    return cl.Program(ctx, open(KERNEL).read()).build()


def test_kernel_matches_consensus_both_algos():
    ctx = _ctx()
    q = cl.CommandQueue(ctx)
    prg = _build(ctx)
    mh, mapbytes, rnd = _setup(7)
    mf = cl.mem_flags
    map_b = cl.Buffer(ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=np.frombuffer(mapbytes, dtype=np.uint8))
    kern = cl.Kernel(prg, "test_heavy3_dual")
    import hashlib

    for new_pow in (0, 1):
        dig_mismatch = diff_mismatch = total = 0
        for _ in range(3):
            bh = "%064x" % rnd.getrandbits(256)
            cond = mh.bin_convert(bh)
            cond_b = cl.Buffer(ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=np.frombuffer(cond.encode(), dtype=np.uint8))
            N = 800
            data = bytearray(); lens = []; ed = []; eg = []
            for _ in range(N):
                addr = "%056x" % rnd.getrandbits(224)
                nonce = "%0x" % rnd.getrandbits(rnd.randint(8, 180))
                pre = (addr + nonce + bh).encode("utf-8")
                data += bytes(pre).ljust(256, b"\x00"); lens.append(len(pre))
                ed.append(mh.diffme_heavy3(addr, nonce, bh, new_pow=bool(new_pow)))
                eg.append((hashlib.blake2b(pre, digest_size=28) if new_pow else hashlib.sha224(pre)).digest())
            ins_b = cl.Buffer(ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=np.frombuffer(bytes(data), dtype=np.uint8))
            lens_b = cl.Buffer(ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=np.array(lens, dtype=np.uint32))
            dig = np.zeros(N * 28, dtype=np.uint8); dff = np.zeros(N, dtype=np.uint32)
            dig_b = cl.Buffer(ctx, mf.WRITE_ONLY, dig.nbytes); dff_b = cl.Buffer(ctx, mf.WRITE_ONLY, dff.nbytes)
            kern(q, (N,), None, ins_b, lens_b, np.uint32(new_pow), map_b, np.uint32(mh.RND_LEN),
                 cond_b, np.uint32(len(cond)), dig_b, dff_b)
            cl.enqueue_copy(q, dig, dig_b); cl.enqueue_copy(q, dff, dff_b); q.finish()
            for i in range(N):
                total += 1
                if bytes(dig[i*28:(i+1)*28]) != eg[i]: dig_mismatch += 1
                if int(dff[i]) != ed[i]: diff_mismatch += 1
        assert dig_mismatch == 0, "%s digest mismatches (new_pow=%d)" % (dig_mismatch, new_pow)
        assert diff_mismatch == 0, "%s difficulty mismatches (new_pow=%d)" % (diff_mismatch, new_pow)


def test_mine_kernel_emits_exactly_consensus_winners():
    ctx = _ctx()
    q = cl.CommandQueue(ctx)
    prg = _build(ctx)
    mh, mapbytes, rnd = _setup(2024)
    mf = cl.mem_flags
    map_b = cl.Buffer(ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=np.frombuffer(mapbytes, dtype=np.uint8))
    kern = cl.Kernel(prg, "mine_heavy3")

    for new_pow in (1, 0):
        address = "%056x" % rnd.getrandbits(224)
        cb_prefix = "hf2" + ("%08x" % rnd.getrandbits(32))
        bh = "%064x" % rnd.getrandbits(256)
        cond = mh.bin_convert(bh)
        prefix = (address + cb_prefix).encode("utf-8")
        suffix = bh.encode("utf-8")
        MIN_DIFF = 18
        BASE = rnd.getrandbits(40)
        N = 1 << 17
        prefix_b = cl.Buffer(ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=np.frombuffer(prefix, dtype=np.uint8))
        suffix_b = cl.Buffer(ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=np.frombuffer(suffix, dtype=np.uint8))
        cond_b = cl.Buffer(ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=np.frombuffer(cond.encode(), dtype=np.uint8))
        MAXF = N
        fc = np.zeros(1, dtype=np.uint32); fn = np.zeros(MAXF, dtype=np.uint64); fd = np.zeros(MAXF, dtype=np.uint32)
        fc_b = cl.Buffer(ctx, mf.READ_WRITE | mf.COPY_HOST_PTR, hostbuf=fc)
        fn_b = cl.Buffer(ctx, mf.WRITE_ONLY, fn.nbytes); fd_b = cl.Buffer(ctx, mf.WRITE_ONLY, fd.nbytes)
        kern(q, (N,), None, prefix_b, np.uint32(len(prefix)), suffix_b, np.uint32(len(suffix)),
             np.uint64(BASE), np.uint32(new_pow), map_b, np.uint32(mh.RND_LEN),
             cond_b, np.uint32(len(cond)), np.uint32(MIN_DIFF), fc_b, fn_b, fd_b, np.uint32(MAXF))
        cl.enqueue_copy(q, fc, fc_b); q.finish()
        cnt = min(int(fc[0]), MAXF)
        cl.enqueue_copy(q, fn, fn_b); cl.enqueue_copy(q, fd, fd_b); q.finish()
        gpu = {int(fn[i]): int(fd[i]) for i in range(cnt)}

        # no false positives: every emitted nonce really meets the threshold per consensus
        for n, d in gpu.items():
            ns = cb_prefix + ("%016x" % n)
            real = mh.diffme_heavy3(address, ns, bh, new_pow=bool(new_pow))
            assert real == d and real >= MIN_DIFF, "false positive nonce=%d gpu=%d real=%d" % (n, d, real)
        # no false negatives: a CPU subrange scan finds nothing the GPU missed
        for i in range(20000):
            n = BASE + i
            ns = cb_prefix + ("%016x" % n)
            if mh.diffme_heavy3(address, ns, bh, new_pow=bool(new_pow)) >= MIN_DIFF:
                assert n in gpu, "false negative: consensus winner nonce=%d not emitted" % n
