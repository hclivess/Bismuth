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

import addrbytes
import sigbytes
import txfields
import txrec
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
    def _addr_ref(a, block_addrs, addr_index):
        """Intra-block address dedup: return ``a``'s index in this block's address dict (the envelope "a"
        list), appending its packed addrbytes blob on first sight. A repeated address (self-spend / change /
        a sender with several txs in the block) then costs a ~1-byte varint index instead of a ~30-byte blob."""
        a = str(a)
        j = addr_index.get(a)
        if j is None:
            j = len(block_addrs)
            addr_index[a] = j
            block_addrs.append(addrbytes.pack_addr(a))
        return j

    @staticmethod
    def _fromhex_or(s):
        # raw digest bytes for a valid hex hash; the original value otherwise (non-hex test fixture / bytes)
        if isinstance(s, str):
            try:
                return bytes.fromhex(s)
            except ValueError:
                return s
        return s

    @staticmethod
    def _bh(block_hash):
        # hf2 Stage-4 (doc/40 core-indexes §3): the `hashes` reverse-index key is the RAW digest (28B sha224
        # pre-fork / 32B blake2b post-fork), not its 56/64-hex .encode() (2x). Deterministic + tolerant: a
        # valid hex hash -> raw bytes; a non-hex string (synthetic test fixture) -> utf-8 fallback; bytes pass
        # through. The same _bh is used on write AND lookup, so the key is consistent and height_by_hash on
        # malformed API input never raises (it falls back, then misses).
        if isinstance(block_hash, str):
            try:
                return bytes.fromhex(block_hash)
            except ValueError:
                return block_hash.encode()
        return block_hash

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
        """Rebuild the full 12-field rows for a block: re-expand the public-key id to the key str, and
        rebuild the hf2 Stage-4 TRUE-BYTES fields (signature, address, recipient) to their exact wire
        strings. Dispatch is by VALUE TYPE — a packed field is ``bytes`` (post-fork), a legacy field is
        ``str`` and passes through untouched (so pre-fork rows are byte-identical by construction; doc/40)."""
        # the block hash is stored ONCE in the envelope (raw post-fork / hex pre-fork); reconstruct the hex
        # once and stamp it into each tx's hoisted block_hash slot (doc/40: per-tx block_hash not stored).
        env_h = rec["h"]
        block_hash_hex = bytes(env_h).hex() if isinstance(env_h, (bytes, bytearray)) else env_h
        # per-block address dict (the "a" list): decode each packed address ONCE, then index into it (the
        # intra-block address dedup). Absent on legacy/pre-fork records.
        addr_dict = [addrbytes.unpack_addr(ab) for ab in rec.get("a", [])]
        out = []
        for elem in rec["t"]:
            if isinstance(elem, (bytes, bytearray, memoryview)):
                t = txrec.unpack_row(bytes(elem))       # post-fork: one txrec blob -> 11-field row (pk=id)
                t[1] = addr_dict[t[1]]                  # address:   resolve the "a"-dict index
                t[2] = addr_dict[t[2]]                  # recipient: resolve the "a"-dict index
                t[6] = block_hash_hex                   # fill the hoisted per-tx block_hash from the envelope
            else:
                t = list(elem)                          # pre-fork: legacy 11-field list (addresses inline)
            pkb = txn.get(self.pkr, _hk(t[self._PK]))   # re-expand the public-key dedup id (both forms)
            if pkb is not None:
                t[self._PK] = pkb.decode()
            out.append([height] + t)
        return out

    # --- write -------------------------------------------------------------
    def put_blocks(self, items, fork_height=None):
        """Store an iterable of ``(height, block_hash, full_rows)`` in one transaction.
        ``full_rows`` are 12-field ledger rows; block_height is dropped (the key) and the public key is
        replaced by its dedup id.

        hf2 Stage-4 (doc/40): a block whose DESTINATION height is ``>= fork_height`` (and fork_height is
        not None) stores the signature/address/recipient fields as TRUE BYTES (sigbytes/addrbytes) instead
        of base64/text; ``_expand`` rebuilds the exact wire strings on read. ``fork_height=None`` (the
        default, used by build_from_sqlite/verify_against_sqlite and any pre-fork write) keeps every field
        as the legacy ``str`` — byte-identical to the current store."""
        with self.store.txn(write=True) as txn:
            cache = {}
            for height, block_hash, rows in items:
                post_fork = fork_height is not None and int(height) >= int(fork_height)
                if post_fork and Codec.backend != "msgpack":
                    # the JSON-fallback codec cannot serialize the raw bytes blobs (doc/40 C6)
                    raise RuntimeError(
                        "post-fork true-bytes block storage requires kvstore backend=msgpack (or mdbx); "
                        "the JSON fallback cannot serialize the raw txrec/signature/address byte blobs — "
                        "install msgpack or set the kvstore backend accordingly before the fork height")
                txs = []
                block_addrs, addr_index = [], {}                        # per-block address dict (the "a" list)
                for r in rows:
                    t = list(r[1:])
                    t[self._PK] = self._pubkey_id(txn, t[self._PK], cache)
                    if post_fork:
                        # hf2 Stage-4 (doc/40 tx-fields model B): the ENTIRE row collapses to one txrec
                        # blob (a single msgpack `bin` element) instead of an 11-field list — kills the
                        # per-element msgpack framing (~20B/tx). Address + recipient are stored as varint
                        # indices into the per-block "a" dict (intra-block address dedup).
                        ai = self._addr_ref(t[1], block_addrs, addr_index)
                        ri = self._addr_ref(t[2], block_addrs, addr_index)
                        txs.append(txrec.pack_row(t, ai, ri))
                    else:
                        txs.append(t)                                    # pre-fork: legacy 11-field list
                if post_fork:
                    # envelope: raw block hash (block_hash() reconstructs hex) + the per-block address dict
                    txn.put(self.blocks, _hk(height),
                            _pack({"h": self._fromhex_or(block_hash), "a": block_addrs, "t": txs}))
                else:
                    txn.put(self.blocks, _hk(height), _pack({"h": block_hash, "t": txs}))
                txn.put(self.hashes, self._bh(block_hash), _hk(height))

    def put_block(self, height, block_hash, rows, fork_height=None):
        self.put_blocks([(height, block_hash, rows)], fork_height=fork_height)

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
                # openfield length per tx: legacy list -> last field; txrec blob -> decode the openfield.
                # Char-count of the openfield STRING either way (same semantics as the legacy list form, so
                # the consensus base_fee weight is unchanged by the storage consolidation).
                ofbytes = sum(len(str(txrec.openfield_of(r))) if isinstance(r, (bytes, bytearray, memoryview))
                              else len(str(r[-1])) for r in rows)
                weights.append(len(rows) + ofbytes // unit)
        return weights

    def block_hash(self, height):
        with self.store.txn() as txn:
            v = txn.get(self.blocks, _hk(height))
        if v is None:
            return None
        h = _unpack(v)["h"]
        return bytes(h).hex() if isinstance(h, (bytes, bytearray)) else h   # raw envelope -> hex on read

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
