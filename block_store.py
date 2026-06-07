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

Deps: ``lmdb`` (required), ``msgpack`` (optional — JSON fallback, larger).
"""
import hashlib
import struct

import lmdb

try:
    import msgpack

    def _pack(o):
        return msgpack.packb(o, use_bin_type=True)

    def _unpack(b):
        return msgpack.unpackb(b, raw=False, strict_map_key=False)
except ImportError:  # pragma: no cover - fallback path
    import json

    def _pack(o):
        return json.dumps(o, separators=(",", ":")).encode()

    def _unpack(b):
        return json.loads(b)

_GIB = 1024 ** 3


def _hk(height):
    return struct.pack(">Q", int(height))   # ordered key: lexicographic == numeric


def _uh(key):
    return struct.unpack(">Q", key)[0]


class BlockStore:
    # stored-tx index of the public_key field (in the 11-field row after block_height is dropped:
    # timestamp,address,recipient,amount,signature,public_key,block_hash,fee,reward,operation,openfield)
    _PK = 5

    def __init__(self, path, map_size=64 * _GIB, readonly=False, sync=True):
        self.env = lmdb.open(path, subdir=True, max_dbs=4, map_size=map_size,
                             readonly=readonly, lock=not readonly, sync=sync,
                             metasync=sync)
        self.blocks = self.env.open_db(b"blocks")
        self.hashes = self.env.open_db(b"hashes")
        # Public-key dedup: the 1068-byte RSA public key is 1:1 with the sender address and repeats on
        # every tx, so store each distinct key ONCE and reference it by a small integer id. Transparent
        # + lossless: get_block re-expands the id to the original key string.
        self.pk = self.env.open_db(b"pk")     # public_key bytes -> id (BE uint64)
        self.pkr = self.env.open_db(b"pkr")   # id (BE uint64) -> public_key bytes

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
        v = txn.get(hkey, db=self.pk)
        if v is not None:
            nid = _uh(v)
        else:
            nid = txn.stat(db=self.pk)["entries"]
            txn.put(hkey, _hk(nid), db=self.pk)
            txn.put(_hk(nid), pkb, db=self.pkr)
        cache[pk] = nid
        return nid

    def _expand(self, txn, height, rec):
        """Rebuild the full 12-field rows for a block, re-expanding the public-key id to the key str."""
        out = []
        for t in rec["t"]:
            t = list(t)
            pkb = txn.get(_hk(t[self._PK]), db=self.pkr)
            if pkb is not None:
                t[self._PK] = pkb.decode()
            out.append([height] + t)
        return out

    # --- write -------------------------------------------------------------
    def put_blocks(self, items):
        """Store an iterable of ``(height, block_hash, full_rows)`` in one transaction.
        ``full_rows`` are 12-field ledger rows; block_height is dropped (the key) and the public key is
        replaced by its dedup id."""
        with self.env.begin(write=True) as txn:
            cache = {}
            for height, block_hash, rows in items:
                txs = []
                for r in rows:
                    t = list(r[1:])
                    t[self._PK] = self._pubkey_id(txn, t[self._PK], cache)
                    txs.append(t)
                txn.put(_hk(height), _pack({"h": block_hash, "t": txs}), db=self.blocks)
                txn.put(self._bh(block_hash), _hk(height), db=self.hashes)

    def put_block(self, height, block_hash, rows):
        self.put_blocks([(height, block_hash, rows)])

    def rollback(self, to_height):
        """Delete every block with height > ``to_height`` (a reorg). Returns the count removed."""
        with self.env.begin(write=True) as txn:
            cur = txn.cursor(db=self.blocks)
            to_delete = []
            if cur.set_range(_hk(int(to_height) + 1)):
                for k, v in cur:                       # collect first (don't mutate while iterating)
                    to_delete.append((bytes(k), self._bh(_unpack(v)["h"])))
            for block_key, hash_key in to_delete:
                txn.delete(block_key, db=self.blocks)
                txn.delete(hash_key, db=self.hashes)
        return len(to_delete)

    # --- read --------------------------------------------------------------
    def get_block(self, height):
        """Full 12-field ledger rows for ``height`` (block_height re-prepended, pubkey re-expanded)."""
        with self.env.begin() as txn:
            v = txn.get(_hk(height), db=self.blocks)
            if v is None:
                return None
            return self._expand(txn, height, _unpack(v))

    def block_hash(self, height):
        with self.env.begin() as txn:
            v = txn.get(_hk(height), db=self.blocks)
        return _unpack(v)["h"] if v is not None else None

    def height_by_hash(self, block_hash):
        with self.env.begin() as txn:
            v = txn.get(self._bh(block_hash), db=self.hashes)
        return _uh(v) if v is not None else None

    def blocks_in_range(self, start, end):
        """Yield ``(height, full_rows)`` for heights in [start, end], ascending."""
        with self.env.begin() as txn:
            cur = txn.cursor(db=self.blocks)
            if cur.set_range(_hk(start)):
                for k, v in cur:
                    h = _uh(k)
                    if h > end:
                        break
                    yield h, self._expand(txn, h, _unpack(v))

    def tip(self):
        with self.env.begin() as txn:
            cur = txn.cursor(db=self.blocks)
            return _uh(cur.key()) if cur.last() else None

    def count(self):
        with self.env.begin() as txn:
            return txn.stat(db=self.blocks)["entries"]

    def close(self):
        self.env.close()


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
