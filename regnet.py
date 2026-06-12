"""
Regnet specific functions and settings
"""

import base64
import math
import os
import sqlite3
import sys
import time
import functools
from Cryptodome.PublicKey import RSA
from Cryptodome.Hash import SHA
from Cryptodome.Signature import PKCS1_v1_5
from hashlib import sha224, blake2b
from random import getrandbits

import amounts
import connections
import mempool as mp
import mining_heavy3 as mining

# fixed diff for regnet
REGNET_DIFF = 16

REGNET_PORT = 3030

REGNET_DB = "static/regmode.db"

REGNET_INDEX = "static/index_reg.db"

REGNET_PEERS = "peers_reg.txt"
REGNET_SUGGESTED_PEERS = "peers_reg.txt"

SQL_INDEX = [ "CREATE TABLE aliases (block_height INTEGER, address, alias)",
              "CREATE TABLE tokens (block_height INTEGER, timestamp, token, address, recipient, txid, amount INTEGER)" ]

SQL_LEDGER = [ "CREATE TABLE misc (block_height INTEGER, difficulty TEXT)",

               "CREATE TABLE transactions (block_height INTEGER, timestamp NUMERIC, address TEXT, recipient TEXT, \
               amount NUMERIC, signature TEXT, public_key TEXT, block_hash TEXT, fee NUMERIC, reward NUMERIC, \
               operation TEXT, openfield TEXT)",

               "INSERT INTO transactions (openfield, operation, reward, fee, block_hash, public_key, signature, \
               amount, recipient, address, timestamp, block_height) \
               VALUES ('genesis', 1, 1, 0, '7a0f384876aca3871adbde8622a87f8b971ede0ed8ee10425e3958a1', \
               '-----BEGIN PUBLIC KEY-----\nMIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDKvLTbDx85a1ugb/6xMMhVOq6U\n2GeYT8+Iq2z9FwIMR40l2ttGqNK7varNccFLIu8Kn4ogDQs3WSWQCxNkhZh/FqzF\nYYa3/ItPPfzrXqgajwD8q4Zt4Ymjt8+2BkImPjjFNkuTQIz2Iu3yFqOIxLdjMw7n\nUVu9tFPiUkD0VnDPLQIDAQAB\n-----END PUBLIC KEY-----', \
               'DKiWVr+GQHrsEUlu3qEQnsB5rznU4Is7RFLnPmHM1grobiUFHup0kSWiN83gBkNS9LgE57RXUEJvxMKc+9hIAzYE8EwGtO3RsXxkqPTT1v19CguN0iqE4nIM8Bur53/Djs5a1bH/R8EMersemZY1bDJ4jTeeba6yqFxmevGk/gw=', \
               0, '4edadac9093d9326ee4b17f869b14f1a2534f96f9c5d7b48dc9acaed', 'genesis', 1493640955.47, 1);",

               "INSERT INTO misc (difficulty, block_height) VALUES ({},1)".format(REGNET_DIFF)]

# Integer-atomic-unit ledger schema (doc/16 phase 2 cutover): amount/fee/reward become INTEGER and the
# genesis reward (1 BIS) is stored as 100000000 units. Derived from SQL_LEDGER so the long genesis
# key/sig literals are not duplicated.
SQL_LEDGER_INTEGER = [
    s.replace("amount NUMERIC", "amount INTEGER")
     .replace("fee NUMERIC", "fee INTEGER")
     .replace("reward NUMERIC", "reward INTEGER")
     .replace("VALUES ('genesis', 1, 1, 0,", "VALUES ('genesis', 1, 100000000, 0,")
    for s in SQL_LEDGER
]


FILES_TO_REMOVE = [REGNET_DB, REGNET_INDEX]

HASHCOUNT = 10

# Max number of tx to embed per block.
TX_PER_BLOCK = 2


# Do not edit below, it's fed by node.py

ADDRESS = 'This is a fake address placeholder for regtest mode only'
KEY = None
PRIVATE_KEY_READABLE = 'matching priv key'
PUBLIC_KEY_B64ENCODED = 'matching pub key b64'

DIGEST_BLOCK = None

# because of compatibility - huge node refactor wanted.


def sql_trace_callback(log, id, statement):
    line = f"SQL[{id}] {statement}"
    log.warning(line)


def generate_one_block(blockhash, mempool_txs, node, db_handler):
    try:
        if not blockhash:
            node.logger.app_log.warning("Bad blockhash")
            return
        diff_hex = math.floor((REGNET_DIFF / 8) - 1)
        mining_condition = blockhash[0:diff_hex]
        while True:
            try_arr = [('%0x' % getrandbits(32)) for i in range(HASHCOUNT)]
            i = 0
            for i in range(100):
                i += 1
                # hf2: a signalling node mines a coinbase nonce that carries the readiness marker
                # (fork.FORK2_SIGNAL). It rides in the PoW nonce itself, so PoW still validates.
                # POST-FORK: instead, commit the VM pre-state root in the coinbase (doc/19) so peers can
                # reject a divergent block. Either way it's part of the PoW-hashed seed.
                _signal = "hf2" if getattr(node, "fork_signal", False) else ''   # keep signalling so syncing nodes still detect the fork
                # Dual PoW (doc/18-D, bundled into hf2): post-fork the inner hash modernises sha224 ->
                # blake2b, exactly like miner.py and the verifier (mining_heavy3.diffme_heavy3). The
                # generator MUST mirror it or the block it mines fails its own PoW check. Switch on
                # new_pow = next height >= the (single) hf2 fork_height.
                _pfh = getattr(node, "fork_height", None)
                new_pow = _pfh is not None and (getattr(node, "hdd_block", 0) + 1) >= _pfh

                def _inner(s):     # the 224-bit inner digest fed to the Heavy3 anneal (blake2b post-fork)
                    b = s.encode("utf-8")
                    return int.from_bytes(blake2b(b, digest_size=28).digest() if new_pow
                                          else sha224(b).digest(), 'big')

                _sr_fh = getattr(node, "fork_height", None)
                _sr = getattr(node, "vm_state_root", None)
                if _sr_fh is not None and getattr(node, "hdd_block", 0) + 1 >= _sr_fh and _sr:
                    import vm_engine
                    seed = _signal + vm_engine.embed_state_root(_sr, '%0x' % getrandbits(48))
                else:
                    seed = _signal + ('%0x' % getrandbits(128 - 32))
                prefix = ADDRESS + seed
                # print("node heavy", node.heavy)
                if node.heavy:
                    possibles = [nonce for nonce in try_arr if
                                 mining_condition in (
                                     mining.anneal3(mining.MMAP, _inner(prefix + nonce + blockhash)))]
                else:
                    possibles = [nonce for nonce in try_arr if
                                 mining_condition in (
                                     mining.anneal3_regnet(mining.MMAP, _inner(prefix + nonce + blockhash)))]
                if possibles:
                    nonce = seed + possibles[0]
                    node.logger.app_log.warning("Generate got a block in {} tries len {}".format(i, len(possibles)))
                    # assemble block with mp data
                    txs = []
                    for i in range(TX_PER_BLOCK):
                        if not len(mempool_txs):
                            break
                        txs.append(mempool_txs.pop(0))
                    block_send = []
                    removal_signature = []
                    for mpdata in txs:
                        transaction = (
                            str(mpdata[0]), str(mpdata[1][:56]), str(mpdata[2][:56]), '%.8f' % float(mpdata[3]),
                            str(mpdata[4]), str(mpdata[5]), str(mpdata[6]),
                            str(mpdata[7]))  # create tuple
                        # node.logger.app_log.warning transaction
                        block_send.append(transaction)  # append tuple to list for each run
                        removal_signature.append(str(mpdata[4]))  # for removal after successful mining
                    # claim reward
                    block_timestamp = '%.2f' % time.time()
                    transaction_reward = (str(block_timestamp), str(ADDRESS[:56]), str(ADDRESS[:56]),
                                          '%.8f' % float(0), "0", str(nonce))  # only this part is signed!
                    # node.logger.app_log.warning transaction_reward

                    hash = SHA.new(str(transaction_reward).encode("utf-8"))
                    signer = PKCS1_v1_5.new(KEY)
                    signature = signer.sign(hash)
                    signature_enc = base64.b64encode(signature)

                    if signer.verify(hash, signature):
                        node.logger.app_log.warning("Signature valid")
                        block_send.append((str(block_timestamp), str(ADDRESS[:56]), str(ADDRESS[:56]), '%.8f' % float(0),
                                           str(signature_enc.decode("utf-8")), str(PUBLIC_KEY_B64ENCODED.decode("utf-8")),
                                           "0", str(nonce)))  # mining reward tx
                        node.logger.app_log.warning("Block to send: {}".format(block_send))
                    # calc hash

                    new_hash = DIGEST_BLOCK(node, [block_send], None, 'regtest',  db_handler)
                    # post block to self or better, send to db to make sure it is. when we add the next one?
                    # use a link to the block digest function
                    # embed at mot TX_PER_BLOCK txs from the mp
                    return new_hash

    except Exception as e:
        node.logger.app_log.warning(e)
        exc_type, exc_obj, exc_tb = sys.exc_info()
        fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
        node.logger.app_log.warning("{} {} {}".format(exc_type, fname, exc_tb.tb_lineno))


def command(sdef, data, blockhash, node, db_handler):
    try:
        node.logger.app_log.warning("Regnet got command {}".format(data))
        if data == 'regtest_generate':
            how_many = int(connections.receive(sdef))
            node.logger.app_log.warning("regtest_generate {} {}".format(how_many, blockhash))
            mempool_txs = mp.MEMPOOL.fetchall(mp.SQL_SELECT_TX_TO_SEND)
            for i in range(how_many):
                blockhash = generate_one_block(blockhash, mempool_txs, node, db_handler)
            connections.send(sdef, 'OK')
        elif data == 'regtest_mine':
            # drive the REAL solo miner (miner.py): it builds a block with the pending mempool txs + the
            # hf2 coinbase (+ the VM state root post-fork) and mines the dual-algo Heavy3 — the exact
            # code path used on mainnet. This is how we regnet-test the actual miner, not the regnet-only
            # generator above.
            how_many = int(connections.receive(sdef))
            import miner
            for _ in range(how_many):
                miner.generate_block(node, db_handler)
            connections.send(sdef, 'OK')
        elif data == 'regtest_powcheck':
            # verify the DUAL-algo PoW runs both ways inside the node (which has the Heavy3 mmap): the same
            # input under sha224 vs blake2b yields different difficulties across a few samples.
            old_vals, new_vals = [], []
            for n in ("a1", "b2", "c3", "d4"):
                old_vals.append(mining.diffme_heavy3(node.keys.address, n, blockhash, new_pow=False))
                new_vals.append(mining.diffme_heavy3(node.keys.address, n, blockhash, new_pow=True))
            connections.send(sdef, "{}|{}".format(",".join(map(str, old_vals)), ",".join(map(str, new_vals))))
        elif data == 'regtest_rollback':
            # REGNET-ONLY test helper: drive the REAL chain rollback so the reorg path (ledger + all
            # auxiliary stores) is exercised end-to-end. Destructive — hence this dispatcher is regnet-
            # gated and we re-check is_regnet here (defence-in-depth: a rollback command must never be
            # reachable on a live chain).
            below = int(connections.receive(sdef))
            if getattr(node, "is_regnet", False):
                from decimal import Decimal
                import chain_ops
                chain_ops.rollback(node, db_handler, below)
                # reflect the new tip in the node's height pointers (a resync does this in production)
                node.hdd_block = below - 1
                node.last_block = below - 1
                db_handler.execute_param(db_handler.c,
                    "SELECT timestamp, block_hash FROM transactions WHERE block_height = ? AND reward != 0 LIMIT 1",
                    (below - 1,))
                row = db_handler.c.fetchone()
                if row:
                    node.last_block_timestamp = Decimal(row[0])
                    node.last_block_hash = row[1]
            connections.send(sdef, 'OK')
    except Exception as e:
        node.logger.app_log.warning(e)
        exc_type, exc_obj, exc_tb = sys.exc_info()
        fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
        node.logger.app_log.warning("{} {} {}".format(exc_type, fname, exc_tb.tb_lineno))


def init(app_log, trace_db_calls=False):
    # Empty peers
    with open(REGNET_PEERS, 'w') as f:
        f.write("{}")
    with open(REGNET_SUGGESTED_PEERS, 'w') as f:
        f.write("{}")
    # Regnet starts a FRESH chain at every boot by design. BISMUTH_REGNET_KEEP=1 is a test-only
    # escape (tests/fork_transition_smoke.py) that keeps an existing regnet chain across a restart,
    # so live restart/replay continuity — e.g. the hf2 transition and its lock-in replay — can be
    # exercised against a real node. Default behaviour (no env var / no existing DB) is unchanged.
    if os.environ.get("BISMUTH_REGNET_KEEP") and os.path.exists(REGNET_DB):
        app_log.warning("Regnet: BISMUTH_REGNET_KEEP set — keeping the existing regnet chain")
        return
    # empty files
    for remove_me in FILES_TO_REMOVE:
        if os.path.exists(remove_me):
            os.remove(remove_me)
    # the fork lock-in sidecar MUST die with the chain it was derived from: a stale lock-in against a
    # freshly-wiped regnet chain is the same chain/lock-in inconsistency as the 2026-06-09 regnet->
    # mainnet bleed, just intra-regnet (the node would boot a brand-new chain with hf2 already
    # "active"). Namespacing keeps this strictly regnet-scoped (fork_lockin-regmode.db.json).
    try:
        import fork as _fork
        _lock = _fork.lockin_path(REGNET_DB)
        if os.path.exists(_lock):
            os.remove(_lock)
    except Exception:
        pass
    # create empty index db
    with sqlite3.connect(REGNET_DB) as source_db:
        if trace_db_calls:
            source_db.set_trace_callback(functools.partial(sql_trace_callback,app_log,"REGNET-INIT"))
        for request in (SQL_LEDGER_INTEGER if amounts.LEDGER_INTEGER else SQL_LEDGER):
            source_db.execute(request)
        source_db.commit()
    # create empty reg db
    with sqlite3.connect(REGNET_INDEX) as source_db:
        if trace_db_calls:
            source_db.set_trace_callback(functools.partial(sql_trace_callback,app_log,"REGNET-INIT-INDEX"))
        for request in SQL_INDEX:
            source_db.execute(request)
        source_db.commit()

    # Here, we do not have the keys info yet.
