"""
LMDB-backed append-only block-body store (roadmap doc/17 phase 7).

Immutable block bodies — the per-block transaction rows — live in an embedded, memory-mapped LMDB
key/value store instead of an ever-growing SQL table. Two sub-databases:

  * ``blocks``  : big-endian uint64 height  ->  msgpack {"h": block_hash, "t": [tx, ...]}
  * ``hashes``  : block_hash bytes          ->  big-endian uint64 height

Heights are big-endian so LMDB's ordered keys give an O(1) tip and ordered range scans. Each stored
tx drops the redundant ``block_height`` (it IS the key); ``get_block`` re-prepends it so callers get
the exact 12-field ledger rows back. This is **storage only**: the stored fields are byte-for-byte
what the SQLite ledger holds, so everything derived from them (signature buffers, block hashes) is
identical — it sits behind the frozen serialization boundary, proven by ``verify_against_sqlite`` and
the tests. Append-only, with height-based ``rollback`` for chain reorgs.

This is the foundation for phase 7; it does not yet replace the node's read/write path (that lands
behind a config flag, replay-validated, in a later step).

Migrated onto the engine-agnostic KV abstraction (``kvstore.open_store``, doc/26 storage stage 1): the
four sub-dbs (blocks/hashes/pk/pkr) are opened through ``open_store`` and every env.begin()/cursor goes
through ``KVStore.txn()``/``KVTxn``, so the underlying engine (LMDB<->MDBX<->sqlite-kv) is a one-arg
``backend=`` choice. The public method surface AND the on-disk byte format are UNCHANGED:
  * blocks  : big-endian uint64 height -> ``Codec.pack`` msgpack {"h":..,"t":[..]} (same packer as before)
  * hashes  : block_hash bytes         -> big-endian uint64 height (raw bytes)
  * pk      : blake2b-32(public_key)   -> id BE uint64 (raw bytes)
  * pkr     : id BE uint64             -> public_key bytes (raw bytes)
So a running node reads its existing LMDB files unchanged, and ``verify_against_sqlite`` /
storage_backend's cross_check still hold byte-for-byte. The pubkey-dedup id assignment (next id = current
``pk`` entry count, within the SAME write txn) uses ``KVTxn.count``; the concurrent readonly-reader
integration test relies on lock=True even when readonly — both preserved through the seam.

Deps: ``kvstore`` (which needs ``lmdb`` for the lmdb backend; ``msgpack`` optional — JSON fallback).
"""
import hashlib

from kvstore import Codec, KVStore, open_store

_pack = Codec.pack
_unpack = Codec.unpack
_hk = Codec.hkey
_uh = Codec.unhkey

_GIB = KVStore.GIB


class BlockStore:
    # stored-tx index of the public_key field (in the 11-field row after block_height is dropped:
    # timestamp,address,recipient,amount,signature,public_key,block_hash,fee,reward,operation,openfield)
    _PK = 5

    def __init__(self, path, map_size=64 * _GIB, readonly=False, sync=True, backend="lmdb"):
        # lock=True even when readonly: it registers in the reader table so a separate process can read
        # the store consistently WHILE the node writes it (the integration test does exactly this).
        self.store = open_store(backend, path, dbs=["blocks", "hashes", "pk", "pkr"],
                                map_size=map_size, readonly=readonly, sync=sync, lock=True)
        self.blocks = self.store.open_db("blocks")
        self.hashes = self.store.open_db("hashes")
        # Public-key dedup: the 1068-byte RSA public key is 1:1 with the sender address and repeats on
        # every tx, so store each distinct key ONCE and reference it by a small integer id. Transparent
        # + lossless: get_block re-expands the id to the original key string.
        self.pk = self.store.open_db("pk")     # public_key bytes -> id (BE uint64)
        self.pkr = self.store.open_db("pkr")   # id (BE uint64) -> public_key bytes
        # kept for back-compat with callers/tests that introspect the raw env directly (lmdb backend only)
        self.env = getattr(self.store, "env", None)

    @staticmethod
    def _bh(block_hash):
        return block_hash.encode() if isinstance(block_hash, str) else block_hash

    def _pubkey_id(self, txn, pk, cache):
        """Map a public key to its dedup id, assigning a new one (next = count) if unseen."""
        if pk in cache:
            return cache[pk]
        pkb = pk.encode() if isinstance(pk, str) else pk
        # key the dedup table by a fixed-size content hash: a public key (1068 B for RSA) exceeds LMDB's
        # 511-byte max key size, so it can't be the key directly. blake2b-256 collisions are negligible,
        # and verify_against_sqlite would catch any anyway. The full key is stored as the value in pkr.
        hkey = hashlib.blake2b(pkb, digest_size=32).digest()
        v = txn.get(self.pk, hkey)
        if v is not None:
            nid = _uh(v)
        else:
            nid = txn.count(self.pk)
            txn.put(self.pk, hkey, _hk(nid))
            txn.put(self.pkr, _hk(nid), pkb)
        cache[pk] = nid
        return nid

    def _expand(self, txn, height, rec):
        """Rebuild the full 12-field rows for a block, re-expanding the public-key id to the key str."""
        out = []
        for t in rec["t"]:
            t = list(t)
            pkb = txn.get(self.pkr, _hk(t[self._PK]))
            if pkb is not None:
                t[self._PK] = pkb.decode()
            out.append([height] + t)
        return out

    # --- write -------------------------------------------------------------
    def put_blocks(self, items):
        """Store an iterable of ``(height, block_hash, full_rows)`` in one transaction.
        ``full_rows`` are 12-field ledger rows; block_height is dropped (the key) and the public key is
        replaced by its dedup id."""
        with self.store.txn(write=True) as txn:
            cache = {}
            for height, block_hash, rows in items:
                txs = []
                for r in rows:
                    t = list(r[1:])
                    t[self._PK] = self._pubkey_id(txn, t[self._PK], cache)
                    txs.append(t)
                txn.put(self.blocks, _hk(height), _pack({"h": block_hash, "t": txs}))
                txn.put(self.hashes, self._bh(block_hash), _hk(height))

    def put_block(self, height, block_hash, rows):
        self.put_blocks([(height, block_hash, rows)])

    def rollback(self, to_height):
        """Delete every block with height > ``to_height`` (a reorg). Returns the count removed."""
        with self.store.txn(write=True) as txn:
            to_delete = []
            # collect first (don't mutate while iterating); range from to_height+1 to the end
            for k, v in txn.range(self.blocks, start=_hk(int(to_height) + 1)):
                to_delete.append((bytes(k), self._bh(_unpack(v)["h"])))
            for block_key, hash_key in to_delete:
                txn.delete(self.blocks, block_key)
                txn.delete(self.hashes, hash_key)
        return len(to_delete)

    # --- read --------------------------------------------------------------
    def get_block(self, height):
        """Full 12-field ledger rows for ``height`` (block_height re-prepended, pubkey re-expanded)."""
        with self.store.txn() as txn:
            v = txn.get(self.blocks, _hk(height))
            if v is None:
                return None
            return self._expand(txn, height, _unpack(v))

    def recent_block_weights(self, tip_height, window, w_unit=1000):
        """Per-block WEIGHT (tx count + openfield bytes // w_unit) for the last ``window`` blocks up to
        ``tip_height`` — the post-fork dynamic-fee congestion signal, read from THIS store (LMDB), NEVER
        SQLite. Deterministic: a pure function of the stored canonical blocks. openfield is the last stored
        field (block_height is dropped as the key, so no pubkey re-expansion is needed for the length)."""
        weights = []
        lo = max(1, int(tip_height) - int(window) + 1)
        unit = max(1, int(w_unit))
        with self.store.txn() as txn:
            for h in range(lo, int(tip_height) + 1):
                v = txn.get(self.blocks, _hk(h))
                if v is None:
                    continue
                rows = _unpack(v)["t"]
                ofbytes = sum(len(str(r[-1])) for r in rows)
                weights.append(len(rows) + ofbytes // unit)
        return weights

    def block_hash(self, height):
        with self.store.txn() as txn:
            v = txn.get(self.blocks, _hk(height))
        return _unpack(v)["h"] if v is not None else None

    def height_by_hash(self, block_hash):
        with self.store.txn() as txn:
            v = txn.get(self.hashes, self._bh(block_hash))
        return _uh(v) if v is not None else None

    def blocks_in_range(self, start, end):
        """Yield ``(height, full_rows)`` for heights in [start, end], ascending."""
        with self.store.txn() as txn:
            # end-inclusive: range() is end-exclusive, so bound to end+1 (matches the old h>end break)
            for k, v in txn.range(self.blocks, start=_hk(start), end=_hk(int(end) + 1)):
                h = _uh(k)
                yield h, self._expand(txn, h, _unpack(v))

    def tip(self):
        with self.store.txn() as txn:
            for k, _v in txn.range(self.blocks, reverse=True):
                return _uh(k)
            return None

    def count(self):
        return self.store.stat(self.blocks)["entries"]

    def close(self):
        self.store.close()


# --- build / validate against the legacy SQLite ledger ---------------------------------------------

def _open_ro(ledger_path):
    import sqlite3
    conn = sqlite3.connect("file:%s?mode=ro" % ledger_path, uri=True, timeout=60)
    conn.text_factory = str
    return conn


def _grouped_blocks(cursor, start, end, batch_blocks):
    """Stream the ledger in height order, yielding (height, block_hash, rows) per block."""
    h = start
    while h <= end:
        hi = min(h + batch_blocks - 1, end)
        rows = cursor.execute(
            "SELECT * FROM transactions WHERE block_height >= ? AND block_height <= ? "
            "ORDER BY block_height, rowid", (h, hi)).fetchall()
        cur_h, group = None, None
        for r in rows:
            if r[0] != cur_h:
                if group:
                    yield cur_h, group[0][7], group
                cur_h, group = r[0], []
            group.append(list(r))
        if group:
            yield cur_h, group[0][7], group
        h = hi + 1


def build_from_sqlite(ledger_path, store, start=1, end=None, batch_blocks=500):
    """Load blocks [start, end] from a SQLite ledger's ``transactions`` table into ``store``."""
    conn = _open_ro(ledger_path)
    try:
        cur = conn.cursor()
        if end is None:
            end = cur.execute("SELECT max(block_height) FROM transactions").fetchone()[0] or 0
        batch, n = [], 0
        for height, bh, rows in _grouped_blocks(cur, start, end, batch_blocks):
            batch.append((height, bh, rows))
            if len(batch) >= batch_blocks:
                store.put_blocks(batch); n += len(batch); batch = []
        if batch:
            store.put_blocks(batch); n += len(batch)
        return n
    finally:
        conn.close()


def verify_against_sqlite(ledger_path, store, start=1, end=None):
    """Assert every block in [start, end] read back from ``store`` equals the SQLite rows exactly.
    Returns (blocks_checked, txs_checked); raises AssertionError on the first mismatch."""
    conn = _open_ro(ledger_path)
    try:
        cur = conn.cursor()
        if end is None:
            end = cur.execute("SELECT max(block_height) FROM transactions").fetchone()[0] or 0
        blocks = txs = 0
        for height, _bh, rows in _grouped_blocks(cur, start, end, 500):
            got = store.get_block(height)
            assert got is not None, "block %d missing from store" % height
            assert got == rows, "block %d differs between store and ledger" % height
            blocks += 1
            txs += len(rows)
        return blocks, txs
    finally:
        conn.close()
