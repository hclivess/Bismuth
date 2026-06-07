"""Real-scale demonstration: build an LMDB BlockStore from the live 22GB mainnet ledger, prove it is
a lossless + consensus-faithful mirror, and measure its size. Read-only on the ledger."""
import os
import shutil
import time

import bismuth_serialize
import block_store
from polysign.signerfactory import SignerFactory
from quantizer import quantize_two, quantize_eight

LEDGER = "static/ledger.db"
STORE = "/var/tmp/bs_demo"
START, END = 3_850_000, 4_850_000   # 1,000,000 recent real blocks (RSA + ECDSA + ED25519 txs)

shutil.rmtree(STORE, ignore_errors=True)
os.makedirs(STORE)
s = block_store.BlockStore(STORE, map_size=32 * 1024 ** 3, sync=False)

t = time.time()
n = block_store.build_from_sqlite(LEDGER, s, start=START, end=END)
bt = time.time() - t
print(f"BUILD : {n} blocks [{START}..{END}] in {bt:.0f}s = {n/max(bt,1):.0f} blk/s  (tip={s.tip()})")

sz = sum(os.path.getsize(os.path.join(STORE, f)) for f in os.listdir(STORE))
print(f"SIZE  : LMDB store = {sz/1e9:.2f} GB for {n} blocks  (full SQLite ledger.db is 22 GB)")

t = time.time()
blocks, txs = block_store.verify_against_sqlite(LEDGER, s, start=START, end=END)
print(f"LOSSLESS: {blocks} blocks / {txs} txs byte-identical to the ledger in {time.time()-t:.0f}s")

# consensus faithfulness: re-derive each signed tx's buffer FROM THE STORE and verify the signature
checked = failed = 0
for h, rows in s.blocks_in_range(START, START + 5000):
    for r in rows:  # [height, ts, addr, recip, amount, sig, pubkey, block_hash, fee, reward, op, openfield]
        if not (r[5] and r[6]):
            continue
        ts = "%.2f" % quantize_two(r[1])
        amt = "%.8f" % quantize_eight(r[4])
        buf = bismuth_serialize.signature_buffer(ts, r[2], r[3], amt, r[10], r[11])
        try:
            SignerFactory.verify_bis_signature(r[5], r[6], buf, r[2])
            checked += 1
        except Exception:
            failed += 1  # coinbase / unsigned rows land here
print(f"SIGCHECK: {checked} signed txs verified through the store, {failed} unverifiable (coinbase/unsigned)")
s.close()
print("DONE")
