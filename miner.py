"""
miner.py — a real solo miner for Bismuth (mainnet + regnet).

It assembles a full block — the pending mempool transactions plus a signed coinbase whose openfield
carries the hf2 fork-readiness signal and, post-fork, the committed VM state root — then mines the
Heavy3 proof-of-work and digests it. The PoW is DUAL-ALGO: today's sha224-anneal, or the modernised
blake2b-anneal once hf2 activates (the PoW swap is BUNDLED into the single hf2 fork, doc/18-D); the
switch is `block_height >= node.fork_height` (see fork.py / mining_heavy3.diffme_heavy3).

The GPU nonce-finders in gpuminer/ (CUDA bis.cu, OpenCL bismuth.cl) solve the same PoW; this module is
the block-builder / orchestrator they (or this CPU loop) feed into. Enable solo mining with `mine=True`
and a funded wallet; opt into signalling with `fork_signal=True` (which asserts blake2b readiness too).
"""
import base64
import math
import time
from hashlib import blake2b, sha224
from random import getrandbits

from Cryptodome.Hash import SHA
from Cryptodome.Signature import PKCS1_v1_5

import fork
import mempool as mp
import mining_heavy3 as mining
import regnet

HASHCOUNT = 256          # nonce candidates tried per mining attempt before re-reading the tip
MAX_TX_PER_BLOCK = 1000  # cap on mempool txs embedded in one block


def _coinbase_prefix(node):
    """The FIXED part of the coinbase openfield: the readiness signal + (post-fork) the VM state root.
    The mined nonce is appended to it. The node's detections SEARCH for their markers (hf2/vmsr), so
    the concatenation order is not fragile."""
    sig = ""
    if getattr(node, "fork_signal", False):
        sig += fork.FORK2_SIGNAL                      # "hf2": whole-bundle readiness (incl. blake2b PoW)
    fh = getattr(node, "fork_height", None)
    sr = getattr(node, "vm_state_root", None)
    if fh is not None and (node.last_block + 1) >= fh and sr:
        import vm_engine
        sig += vm_engine.embed_state_root(sr, "")     # "vmsr"+root: enforced post-fork (doc/19)
    return sig


def _new_pow(node):
    """Has the modernised (blake2b) Heavy3 activated for the block we're about to mine?
    The PoW swap rides the single hf2 fork, so this gates on the same fork_height as everything else."""
    fh = getattr(node, "fork_height", None)
    return fh is not None and (node.last_block + 1) >= fh


def _required_diff(node, db_handler):
    """The difficulty the mined nonce must satisfy — matched to what check_block validates."""
    if node.is_regnet:
        return regnet.REGNET_DIFF - 8                 # mirrors mining_heavy3.check_block's regnet branch
    from difficulty import difficulty
    return int(difficulty(node, db_handler)[0])


def mine_nonce(node, db_handler):
    """Search for a winning coinbase openfield on the current tip. NO db_lock held (pure computation).
    Returns (blockhash, nonce) or None."""
    blockhash = node.last_block_hash
    if not blockhash:
        return None
    address = node.keys.address
    new_pow = _new_pow(node)
    # Same winning condition the in-tree generator + the chain use: the difficulty's hex-prefix of the
    # block hash must appear as a substring of the annealed hash. (check_block re-derives difficulty from
    # diffme_heavy3, but mining to this proven condition produces blocks it accepts.)
    if node.is_regnet:
        diff = regnet.REGNET_DIFF
    else:
        from difficulty import difficulty
        diff = difficulty(node, db_handler)[0]
    diff_hex = max(1, int(math.floor((diff / 8) - 1)))
    target = blockhash[0:diff_hex]
    anneal = mining.anneal3 if node.heavy else mining.anneal3_regnet
    prefix = _coinbase_prefix(node)
    attempts = 0
    while not node.IS_STOPPING:
        for _ in range(HASHCOUNT):
            attempts += 1
            openfield = prefix + ('%0x' % getrandbits(64))           # the coinbase openfield = prefix + nonce
            data = (address + openfield + blockhash).encode("utf-8")
            raw = blake2b(data, digest_size=28).digest() if new_pow else sha224(data).digest()
            if target in str(anneal(mining.MMAP, int.from_bytes(raw, 'big'))):
                return blockhash, openfield
        if node.last_block_hash != blockhash:     # tip advanced while mining -> abandon, caller re-mines
            return None
        if attempts > 400000:                     # safety cap so a command never hangs
            node.logger.app_log.warning(f"Miner: NO nonce in {attempts} (diff_hex={diff_hex} target={target!r} heavy={node.heavy})")
            return None
    return None


def _build_block(node, nonce):
    """Assemble [pending mempool txs..., signed coinbase] in the exact shape the digester expects."""
    block_send = []
    for m in mp.MEMPOOL.fetchall(mp.SQL_SELECT_TX_TO_SEND)[:MAX_TX_PER_BLOCK]:
        block_send.append((str(m[0]), str(m[1][:56]), str(m[2][:56]), '%.8f' % float(m[3]),
                           str(m[4]), str(m[5]), str(m[6]), str(m[7])))
    ts = '%.2f' % time.time()
    addr = node.keys.address
    # hf2 §2.C coinbase compaction: post-fork the coinbase carries NO signature + NO public key (authorized
    # by PoW + the reward formula; the node enforces empty). Pre-fork it is RSA-signed as before.
    _fh = getattr(node, "fork_height", None)
    _post_fork = _fh is not None and (node.last_block + 1) >= _fh
    if _post_fork:
        sig_field, pub = "", ""
    else:
        reward_tx = (str(ts), str(addr[:56]), str(addr[:56]), '%.8f' % 0.0, "0", str(nonce))   # only this is signed
        h = SHA.new(str(reward_tx).encode("utf-8"))
        sig_field = base64.b64encode(PKCS1_v1_5.new(node.keys.key).sign(h)).decode("utf-8")
        pub = node.keys.public_key_b64encoded
        pub = pub.decode("utf-8") if isinstance(pub, (bytes, bytearray)) else str(pub)
    block_send.append((str(ts), str(addr[:56]), str(addr[:56]), '%.8f' % 0.0,
                       str(sig_field), str(pub), "0", str(nonce)))   # the coinbase
    return block_send


def generate_block(node, db_handler):
    """Mine ONE block on the tip and digest it (serialised with sync via db_lock). Returns the new block
    hash, or None if no nonce was found or the tip moved while mining."""
    from digest import digest_block
    found = mine_nonce(node, db_handler)              # slow part, no lock
    if found is None:
        return None
    blockhash, nonce = found
    if node.last_block_hash != blockhash:             # tip advanced while we mined -> discard, re-mine
        return None
    block_send = _build_block(node, nonce)
    # digest_block acquires db_lock itself if free, or SKIPS if a sync digest already holds it (in which
    # case the mining_loop just re-mines). So we must NOT hold the lock here.
    return digest_block(node, [block_send], None, "miner", db_handler)


def mining_loop(node, db_handler):
    """Solo-mining loop: mine block after block until the node stops."""
    node.logger.app_log.warning("Status: solo miner started (mine=True)")
    while not node.IS_STOPPING:
        try:
            # doc/35: skip a round while the peer-difficulty divergence detector has paused mining (opt-in).
            # A wrong LOCAL difficulty — in EITHER direction — makes our mined blocks orphan-bound, so we
            # mustn't burn work at the configured difficulty() until a CLEAN peer-quorum reading clears it.
            if getattr(node, "mining_paused", False):
                time.sleep(1)
                continue
            if node.db_lock.locked():                 # sync in progress; don't fight it
                time.sleep(0.2)
                continue
            if generate_block(node, db_handler):
                node.logger.app_log.warning(f"Miner: block accepted, tip now {node.last_block}")
            else:
                time.sleep(0.05)
        except Exception as e:
            node.logger.app_log.warning(f"Miner loop error: {type(e).__name__}: {e}")
            time.sleep(1)
