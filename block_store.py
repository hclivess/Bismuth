"""
The Bismuth ledger: ONE LMDB environment holding the whole chain and every index the node reads.

Each block — its transaction rows, its difficulty, and every derived index entry (signature, address history, balances, content txid, pubkey registry) — is committed in ONE LMDB
write transaction by ``commit_block``, and a reorg is ONE transaction by ``rollback``. There is no second
file to keep in step, no commit marker, no reconcile: a crash leaves the store at a clean block boundary.

Sub-databases (all keys big-endian so LMDB's ordered keys give O(1) tips and ordered range scans):

  * ``blocks``  : u64 height -> msgpack {"h": block_hash, "t": [tx, ...]} (pre-fork: 11-field lists;
                  post-fork: one ``txrec`` blob per tx + the per-block address dict "a", doc/40)
  * ``hashes``  : raw block-hash bytes -> u64 height
  * ``pk``/``pkr``: public-key dedup (blake2b-32(key) -> id, id -> key)
  * ``diff``    : u64 height -> difficulty text (the value the PoW check was satisfied against)
  * ``sig``     : signature[:56] || 0x00 || s64 height || u16 idx -> ""   (txid / signature lookups)
  * ``addr``    : role(0 sender, 1 recipient) || u8 len || address || s64 height || u16 idx -> ""
  * ``bal``     : address -> 4 x i128 LE (credited amount, rewards, debited amount, fees), integer units
  * ``txid``    : raw content txid -> u64 height (post-fork rows, doc/29)
  * ``pkref``   : address -> u64 first-seen height || pubkey (post-fork pubkey-by-reference, doc/40)
  * ``meta``    : b"schema", b"txcount", b"alloc" (fixed balance allocations, see ``set_allocations``)

``s64`` is the height offset by 2**63 (signed keys, so a height can never wrap the ordering).

Rows come back with the ledger's column types restored — block_height INTEGER, timestamp/amount/fee/reward
NUMERIC, the rest TEXT — whatever form they were written in (``typed_row``). The digester writes str
fields; the typed form is what every reader (consensus difficulty, sync serving, the APIs) consumes.

Deps: ``kvstore`` (lmdb + msgpack).
"""
import hashlib
import os
import re
import struct
import threading
from decimal import Decimal

import addrbytes
import amounts
import txrec
from kvstore import Codec, KVStore, open_store

_pack = Codec.pack
_unpack = Codec.unpack
_hk = Codec.hkey
_uh = Codec.unhkey

_GIB = KVStore.GIB

SCHEMA = 2
SUBDBS = ["blocks", "hashes", "pk", "pkr", "diff", "sig", "addr", "bal", "txid", "pkref", "meta"]

# 12-field ledger row indices
H, TS, ADDR, RECIP, AMOUNT, SIG, PUB, HASH, FEE, REWARD, OP, OF = range(12)

SIG_KEY_LEN = 56          # the legacy txid length: signature[:56]
ROLE_SENDER, ROLE_RECIPIENT = 0, 1


# --------------------------------------------------------------------------- column typing
_INT_RE = re.compile(r"^\s*[+-]?\d+\s*$")
_NUM_RE = re.compile(r"^\s*[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?\s*$")
_I64 = 1 << 63


def numeric(v):
    """NUMERIC column typing: a well-formed integer -> int, a well-formed real -> float (int when it has no
    fractional part), anything else unchanged."""
    if isinstance(v, str):
        if _INT_RE.match(v):
            i = int(v)
            return i if -_I64 <= i < _I64 else float(i)
        if _NUM_RE.match(v):
            f = float(v)
            if f.is_integer() and -_I64 <= f < _I64:
                return int(f)
            return f
        return v
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, Decimal):
        return numeric(str(v))
    if isinstance(v, float) and v.is_integer() and -_I64 <= v < _I64:
        return int(v)
    return v


def real_text(f):
    """A float rendered the way a TEXT column stores a REAL (15 significant digits, always a point)."""
    s = "%.15g" % f
    if not any(c in s for c in ".eEn"):
        s += ".0"
    return s


def text(v):
    if isinstance(v, str):
        return v
    if isinstance(v, float):
        return real_text(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v).decode("utf-8", "replace")
    return str(v)


_COLS = (numeric, numeric, text, text, numeric, text, text, text, numeric, numeric, text, text)


def typed_row(row):
    """A 12-field row with the ledger column types restored (a tuple, like every reader expects)."""
    return tuple(f(v) for f, v in zip(_COLS, row))


def units(v):
    """A stored amount/fee/reward -> integer atomic units (exact; mode-aware)."""
    if v is None or v == "":
        return 0
    if amounts.LEDGER_INTEGER:
        return int(Decimal(str(v)))
    return amounts.to_units(v)


def is_coinbase_row(r):
    try:
        return bool(r[REWARD]) and float(r[REWARD]) != 0
    except (TypeError, ValueError):
        return False


def txid_of(row, fork_height):
    """The canonical content txid of a 12-field ledger row (== essentials.format_raw_tx's id): post-fork the
    binary-pre-image txid over the SIGNED '%.8f' amount string, reconstructed storage-mode-agnostically."""
    import bismuth_serialize
    amount_str = '%.8f' % amounts.ledger_value(row[AMOUNT])
    return bismuth_serialize.tx_id_at(int(row[H]), fork_height, str(row[TS]), str(row[ADDR]),
                                      str(row[RECIP]), amount_str, str(row[OP]), str(row[OF]))


# --------------------------------------------------------------------------- key helpers
def _sh(h):
    return struct.pack(">Q", int(h) + _I64)


def _unsh(b):
    return struct.unpack(">Q", bytes(b))[0] - _I64


def _abytes(a):
    return str(a).encode("utf-8")[:255]


def _addr_prefix(role, a):
    b = _abytes(a)
    return bytes((role, len(b))) + b


def _addr_key(role, a, h, idx):
    return _addr_prefix(role, a) + _sh(h) + struct.pack(">H", idx)


def _sig_key(sig, h, idx):
    return str(sig).encode("utf-8")[:SIG_KEY_LEN] + b"\x00" + _sh(h) + struct.pack(">H", idx)


def _split_pos(key):
    """(height, idx) from the tail of an index key."""
    return _unsh(key[-10:-2]), struct.unpack(">H", key[-2:])[0]


def _indexable_sig(sig):
    s = str(sig)
    return s not in ("", "0", "None")


def _bal_pack(v):
    return b"".join(int(x).to_bytes(16, "little", signed=True) for x in v)


def _bal_unpack(b):
    b = bytes(b)
    return [int.from_bytes(b[i:i + 16], "little", signed=True) for i in range(0, 64, 16)]


def _fold(acc, r, sign=1):
    """Fold one row into acc: address -> [credited amount, rewards, debited amount, fees] (units)."""
    amt = units(r[AMOUNT])
    rc = acc.setdefault(str(r[RECIP]), [0, 0, 0, 0])
    rc[0] += sign * amt
    rc[1] += sign * units(r[REWARD])
    sd = acc.setdefault(str(r[ADDR]), [0, 0, 0, 0])
    sd[2] += sign * amt
    sd[3] += sign * units(r[FEE])


class BlockStore:
    # stored-tx index of the public_key field (in the 11-field row after block_height is dropped)
    _PK = 5

    def __init__(self, path, map_size=64 * _GIB, readonly=False, sync=True, backend="lmdb"):
        # lock=True even when readonly: it registers in the reader table so a separate process can read
        # the store consistently WHILE the node writes it.
        self.path = path
        self.store = open_store(backend, path, dbs=SUBDBS, map_size=map_size, readonly=readonly,
                                sync=sync, lock=True)
        for name in SUBDBS:
            setattr(self, name, self.store.open_db(name))
        self.env = getattr(self.store, "env", None)
        self.readonly = readonly
        if not readonly:
            with self.store.txn(write=True) as txn:
                if txn.get(self.meta, b"schema") is None and txn.count(self.blocks) == 0:
                    txn.put(self.meta, b"schema", str(SCHEMA).encode())

    # ------------------------------------------------------------------ schema
    def schema(self):
        with self.store.txn() as txn:
            v = txn.get(self.meta, b"schema")
        return int(v) if v is not None else None

    def set_schema(self, value=SCHEMA):
        with self.store.txn(write=True) as txn:
            txn.put(self.meta, b"schema", str(int(value)).encode())

    # ------------------------------------------------------------------ body codec
    @staticmethod
    def _addr_ref(a, block_addrs, addr_index):
        a = str(a)
        j = addr_index.get(a)
        if j is None:
            j = len(block_addrs)
            addr_index[a] = j
            block_addrs.append(addrbytes.pack_addr(a))
        return j

    @staticmethod
    def _fromhex_or(s):
        if isinstance(s, str):
            try:
                return bytes.fromhex(s)
            except ValueError:
                return s
        return s

    @staticmethod
    def _bh(block_hash):
        # the `hashes` key is the RAW digest; a non-hex string (test fixture) falls back to utf-8
        if isinstance(block_hash, str):
            try:
                return bytes.fromhex(block_hash)
            except ValueError:
                return block_hash.encode()
        return block_hash

    def _pubkey_id(self, txn, pk, cache):
        if pk in cache:
            return cache[pk]
        pkb = pk.encode() if isinstance(pk, str) else pk
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
        """The stored record -> full 12-field rows as written (pubkey re-expanded, true-bytes fields rebuilt)."""
        env_h = rec["h"]
        block_hash_hex = bytes(env_h).hex() if isinstance(env_h, (bytes, bytearray)) else env_h
        addr_dict = [addrbytes.unpack_addr(ab) for ab in rec.get("a", [])]
        out = []
        for elem in rec["t"]:
            if isinstance(elem, (bytes, bytearray, memoryview)):
                t = txrec.unpack_row(bytes(elem))
                t[1] = addr_dict[t[1]]
                t[2] = addr_dict[t[2]]
                t[6] = block_hash_hex
            else:
                t = list(elem)
            pkb = txn.get(self.pkr, _hk(t[self._PK]))
            if pkb is not None:
                t[self._PK] = pkb.decode()
            out.append([height] + t)
        return out

    def _raw_block(self, txn, height):
        v = txn.get(self.blocks, _hk(height))
        return None if v is None else self._expand(txn, height, _unpack(v))

    def _put_body(self, txn, height, block_hash, rows, fork_height, cache):
        post_fork = fork_height is not None and int(height) >= int(fork_height)
        if post_fork and Codec.backend != "msgpack":
            raise RuntimeError("post-fork true-bytes block storage requires msgpack")
        txs, block_addrs, addr_index = [], [], {}
        for r in rows:
            t = list(r[1:])
            t[self._PK] = self._pubkey_id(txn, t[self._PK], cache)
            if post_fork:
                ai = self._addr_ref(t[1], block_addrs, addr_index)
                ri = self._addr_ref(t[2], block_addrs, addr_index)
                txs.append(txrec.pack_row(t, ai, ri))
            else:
                txs.append(t)
        if post_fork:
            txn.put(self.blocks, _hk(height),
                    _pack({"h": self._fromhex_or(block_hash), "a": block_addrs, "t": txs}))
        else:
            txn.put(self.blocks, _hk(height), _pack({"h": block_hash, "t": txs}))
        txn.put(self.hashes, self._bh(block_hash), _hk(height))

    # ------------------------------------------------------------------ index maintenance
    def _index_rows(self, txn, height, rows, acc, fork_height, delete=False):
        """Write (or delete) the sig/addr/txid/pkref entries of one block + fold its balance deltas."""
        put = (lambda db, k, v=b"": txn.delete(db, k)) if delete else (lambda db, k, v=b"": txn.put(db, k, v))
        sign = -1 if delete else 1
        post_fork = fork_height is not None and int(height) >= int(fork_height)
        for idx, r in enumerate(rows):
            if _indexable_sig(r[SIG]):
                put(self.sig, _sig_key(r[SIG], height, idx))
            put(self.addr, _addr_key(ROLE_SENDER, r[ADDR], height, idx))
            put(self.addr, _addr_key(ROLE_RECIPIENT, r[RECIP], height, idx))
            _fold(acc, r, sign)
            if post_fork:
                put(self.txid, self._txid_key(txid_of(r, fork_height)), _hk(height))
        if post_fork and not delete:
            for r in rows:
                if is_coinbase_row(r):
                    continue
                pub = r[PUB]
                if not pub or str(pub).strip() == "":
                    continue
                key = _abytes(r[ADDR])
                if txn.get(self.pkref, key) is None:
                    txn.put(self.pkref, key, _hk(height) + (pub.encode() if isinstance(pub, str) else bytes(pub)))

    def _apply_balances(self, txn, acc):
        for a, d in acc.items():
            if not any(d):
                continue
            k = _abytes(a)
            v = txn.get(self.bal, k)
            cur = _bal_unpack(v) if v is not None else [0, 0, 0, 0]
            new = [x + y for x, y in zip(cur, d)]
            if any(new):
                txn.put(self.bal, k, _bal_pack(new))
            else:
                txn.delete(self.bal, k)

    @staticmethod
    def _txid_key(txid):
        if isinstance(txid, str):
            try:
                return bytes.fromhex(txid)
            except ValueError:
                return txid.encode()
        return txid

    def _bump(self, txn, key, delta):
        v = txn.get(self.meta, key)
        n = (_uh(v) if v is not None else 0) + int(delta)
        txn.put(self.meta, key, _hk(max(0, n)))

    @staticmethod
    def _tip_in(txn, db):
        for k, _v in txn.range(db, reverse=True):
            return _uh(k)
        return None

    # ------------------------------------------------------------------ write
    def commit_block(self, height, block_hash, rows, difficulty=None, fork_height=None):
        """Append one block atomically: body, hash index, difficulty, and every derived
        index (signature, address, balances, post-fork txid + pubkey registry). ``height`` must be tip+1."""
        height = int(height)
        with self.store.txn(write=True) as txn:
            tip = self._tip_in(txn, self.blocks)
            if tip is not None and height != tip + 1:
                raise ValueError(f"commit_block: height {height} does not extend the tip {tip}")
            self._put_body(txn, height, block_hash, rows, fork_height, {})
            if difficulty is not None:
                txn.put(self.diff, _hk(height), text(difficulty).encode())
            acc = {}
            self._index_rows(txn, height, [list(r) for r in rows], acc, fork_height)
            self._apply_balances(txn, acc)
            self._bump(txn, b"txcount", len(rows))

    def rollback(self, to_height, fork_height=None):
        """Drop every block above ``to_height`` with all its index entries, in ONE transaction. Returns the
        removed blocks' rows (ascending, typed) — the txs a reorg hands back to the mempool."""
        to_height = int(to_height)
        removed = []
        with self.store.txn(write=True) as txn:
            heights = [_uh(k) for k, _v in txn.range(self.blocks, start=_hk(to_height + 1))]
            acc = {}
            for h in reversed(heights):
                rows = self._raw_block(txn, h)
                self._index_rows(txn, h, rows, acc, fork_height, delete=True)
                v = txn.get(self.blocks, _hk(h))
                txn.delete(self.hashes, self._bh(_unpack(v)["h"]))
                txn.delete(self.blocks, _hk(h))
                txn.delete(self.diff, _hk(h))
                self._bump(txn, b"txcount", -len(rows))
                removed[:0] = [typed_row(r) for r in rows]
            self._apply_balances(txn, acc)
            if heights:
                stale = [k for k, v in txn.iterate(self.pkref) if _uh(bytes(v)[:8]) > to_height]
                for k in stale:
                    txn.delete(self.pkref, k)
                stale = [k for k, v in txn.iterate(self.txid) if _uh(v) > to_height]
                for k in stale:
                    txn.delete(self.txid, k)
        return removed

    def put_difficulties(self, items):
        """Backfill ``(height, difficulty)`` pairs in one transaction (store upgrade)."""
        with self.store.txn(write=True) as txn:
            for height, difficulty in items:
                txn.put(self.diff, _hk(height), text(difficulty).encode())

    def allocations(self):
        """{address: units} — fixed balance credits that no block row carries (``set_allocations``)."""
        with self.store.txn() as txn:
            v = txn.get(self.meta, b"alloc")
        return {} if v is None else {str(k): int(u) for k, u in _unpack(v).items()}

    def set_allocations(self, alloc):
        """Set the fixed balance allocations ({address: units}), crediting the difference to the balances in
        the same transaction. Mainnet's are the concluded dev / hypernode reward totals (ledger_genesis)."""
        alloc = {str(k): int(u) for k, u in (alloc or {}).items()}
        with self.store.txn(write=True) as txn:
            v = txn.get(self.meta, b"alloc")
            old = {} if v is None else {str(k): int(u) for k, u in _unpack(v).items()}
            acc = {}
            for a in set(old) | set(alloc):
                d = alloc.get(a, 0) - old.get(a, 0)
                if d:
                    acc[a] = [0, d, 0, 0]
            self._apply_balances(txn, acc)
            txn.put(self.meta, b"alloc", _pack(alloc))

    def reindex(self, fork_height=None, batch=2000, progress=None):
        """Rebuild every derived index (sig, addr, bal, txid, pkref, txcount) from the blocks.
        Recovery / upgrade tool; the live node maintains them incrementally in commit_block/rollback."""
        with self.store.txn(write=True) as txn:
            for db in (self.sig, self.addr, self.bal, self.txid, self.pkref):
                txn.drop(db)
            txn.delete(self.meta, b"txcount")
        tip = self.tip() or 0
        acc, ntx = {}, 0
        h = 1
        while h <= tip:
            hi = min(tip, h + batch - 1)
            with self.store.txn(write=True) as txn:
                for k, v in txn.range(self.blocks, start=_hk(h), end=_hk(hi + 1)):
                    bh = _uh(k)
                    rows = self._expand(txn, bh, _unpack(v))
                    self._index_rows(txn, bh, rows, acc, fork_height)
                    ntx += len(rows)
            if progress:
                progress(hi, tip)
            h = hi + 1
        with self.store.txn(write=True) as txn:
            v = txn.get(self.meta, b"alloc")
            for a, u in ({} if v is None else _unpack(v)).items():
                acc.setdefault(str(a), [0, 0, 0, 0])[1] += int(u)
            self._apply_balances(txn, acc)
            txn.put(self.meta, b"txcount", _hk(ntx))
        return ntx

    # ------------------------------------------------------------------ block reads
    def get_block(self, height):
        """The typed 12-field rows of block ``height``, or None."""
        with self.store.txn() as txn:
            rows = self._raw_block(txn, int(height))
        return None if rows is None else [typed_row(r) for r in rows]

    def get_block_raw(self, height):
        with self.store.txn() as txn:
            return self._raw_block(txn, int(height))

    def last_openfield(self, height):
        """The openfield of block ``height``'s last row (the coinbase), read straight from the stored record
        — no pubkey or address expansion. The fork-signal scan reads one per block of history."""
        with self.store.txn() as txn:
            v = txn.get(self.blocks, _hk(int(height)))
        if v is None:
            return None
        t = _unpack(v)["t"]
        if not t:
            return None
        last = t[-1]
        if isinstance(last, (bytes, bytearray, memoryview)):
            return txrec.unpack_row(bytes(last))[10]
        return last[10]

    def blocks_in_range(self, start, end):
        """Yield ``(height, typed_rows)`` for heights in [start, end], ascending."""
        with self.store.txn() as txn:
            for k, v in txn.range(self.blocks, start=_hk(max(0, int(start))), end=_hk(int(end) + 1)):
                h = _uh(k)
                yield h, [typed_row(r) for r in self._expand(txn, h, _unpack(v))]

    def blocks_desc(self, start=None):
        """Yield ``(height, typed_rows)`` from ``start`` (default: the tip) downwards."""
        with self.store.txn() as txn:
            end = None if start is None else _hk(int(start) + 1)
            for k, v in txn.range(self.blocks, end=end, reverse=True):
                h = _uh(k)
                yield h, [typed_row(r) for r in self._expand(txn, h, _unpack(v))]

    def block_hash(self, height):
        with self.store.txn() as txn:
            v = txn.get(self.blocks, _hk(int(height)))
        if v is None:
            return None
        h = _unpack(v)["h"]
        return bytes(h).hex() if isinstance(h, (bytes, bytearray)) else h

    def height_by_hash(self, block_hash):
        with self.store.txn() as txn:
            v = txn.get(self.hashes, self._bh(block_hash))
        return _uh(v) if v is not None else None

    def tip(self):
        with self.store.txn() as txn:
            return self._tip_in(txn, self.blocks)

    def first(self):
        with self.store.txn() as txn:
            for k, _v in txn.range(self.blocks):
                return _uh(k)
        return None

    def count(self):
        return self.store.stat(self.blocks)["entries"]

    def tx_count(self):
        with self.store.txn() as txn:
            v = txn.get(self.meta, b"txcount")
        return _uh(v) if v is not None else 0

    def coinbase(self, height):
        """The block's mining tx (the row with a non-zero reward), typed, or None."""
        rows = self.get_block(height)
        if not rows:
            return None
        for r in reversed(rows):
            if r[REWARD] != 0:
                return r
        return None

    def recent_block_weights(self, tip_height, window, w_unit=1000, strict=False):
        """Per-block WEIGHT (tx count + openfield bytes // w_unit) for the last ``window`` blocks up to
        ``tip_height`` — the post-fork dynamic-fee congestion signal. ``strict`` (the consensus read) raises
        on a missing height instead of averaging over a short window. The window floors at block 2 (genesis
        is never digested) — pure height arithmetic, identical on every node."""
        weights = []
        lo = max(2, int(tip_height) - int(window) + 1)
        unit = max(1, int(w_unit))
        with self.store.txn() as txn:
            for h in range(lo, int(tip_height) + 1):
                v = txn.get(self.blocks, _hk(h))
                if v is None:
                    if strict:
                        raise ValueError(
                            f"block store missing height {h} in the dynamic-fee window [{lo}, {tip_height}] "
                            f"— cannot compute a consensus-consistent base_fee")
                    continue
                rows = _unpack(v)["t"]
                ofbytes = sum(len(str(txrec.openfield_of(r))) if isinstance(r, (bytes, bytearray, memoryview))
                              else len(str(r[-1])) for r in rows)
                weights.append(len(rows) + ofbytes // unit)
        return weights

    # ------------------------------------------------------------------ difficulty
    def difficulty(self, height):
        with self.store.txn() as txn:
            v = txn.get(self.diff, _hk(int(height)))
        return None if v is None else bytes(v).decode()

    def last_difficulty(self):
        """(height, difficulty text) of the highest recorded difficulty, or (None, None)."""
        with self.store.txn() as txn:
            for k, v in txn.range(self.diff, reverse=True):
                return _uh(k), bytes(v).decode()
        return None, None

    def difficulties(self, heights):
        out = {}
        with self.store.txn() as txn:
            for h in heights:
                v = txn.get(self.diff, _hk(int(h)))
                if v is not None:
                    out[int(h)] = bytes(v).decode()
        return out

    def difficulty_range(self, start, end):
        """[(height, text)] for heights in [start, end)."""
        with self.store.txn() as txn:
            return [(_uh(k), bytes(v).decode())
                    for k, v in txn.range(self.diff, start=_hk(max(0, int(start))), end=_hk(max(0, int(end))))]

    # ------------------------------------------------------------------ index reads
    def _row_at(self, txn, h, idx, cache):
        if h not in cache:
            cache[h] = self._raw_block(txn, h) or []
        rows = cache[h]
        return typed_row(rows[idx]) if idx < len(rows) else None

    def signature_rows(self, sig, max_height=None, limit=None):
        """Typed rows whose signature equals ``sig`` exactly (heights <= max_height)."""
        s = str(sig)
        prefix = s.encode("utf-8")[:SIG_KEY_LEN] + b"\x00"
        out, cache = [], {}
        with self.store.txn() as txn:
            for k, _v in txn.iterate(self.sig, prefix):
                h, idx = _split_pos(k)
                if max_height is not None and h > int(max_height):
                    continue
                r = self._row_at(txn, h, idx, cache)
                if r is not None and r[SIG] == s:
                    out.append(r)
                    if limit and len(out) >= limit:
                        break
        return out

    def signature_exists(self, sig, max_height=None):
        return bool(self.signature_rows(sig, max_height=max_height, limit=1))

    def rows_by_sig_prefix(self, prefix, limit=None):
        """Typed rows whose signature starts with ``prefix`` (the legacy txid lookup), ascending height."""
        p = str(prefix)
        if not p:
            return []
        pb = p.encode("utf-8")
        scan = pb if len(pb) <= SIG_KEY_LEN else pb[:SIG_KEY_LEN] + b"\x00"
        out, cache = [], {}
        with self.store.txn() as txn:
            for k, _v in txn.iterate(self.sig, scan):
                h, idx = _split_pos(k)
                r = self._row_at(txn, h, idx, cache)
                if r is not None and str(r[SIG]).startswith(p):
                    out.append(r)
                    if limit and len(out) >= limit:
                        break
        return out

    def _addr_iter(self, txn, role, a, reverse, lo=None, hi=None):
        p = _addr_prefix(role, a)
        start = p + (_sh(lo) if lo is not None else b"")
        end = p + _sh(hi + 1) if hi is not None else p + b"\xff" * 10
        for k, _v in txn.range(self.addr, start=start, end=end, reverse=reverse):
            yield _split_pos(k)

    def address_positions(self, txn, a, role="any", reverse=True, lo=None, hi=None):
        if role == "sender":
            yield from self._addr_iter(txn, ROLE_SENDER, a, reverse, lo, hi)
            return
        if role == "recipient":
            yield from self._addr_iter(txn, ROLE_RECIPIENT, a, reverse, lo, hi)
            return
        # merge the two ordered streams, de-duplicating self-sends (same (height, idx) in both)
        its = [self._addr_iter(txn, ROLE_SENDER, a, reverse, lo, hi),
               self._addr_iter(txn, ROLE_RECIPIENT, a, reverse, lo, hi)]
        heads = [next(it, None) for it in its]
        last = None
        while heads[0] is not None or heads[1] is not None:
            if heads[1] is None or (heads[0] is not None and
                                    ((heads[0] > heads[1]) if reverse else (heads[0] < heads[1]))):
                i = 0
            else:
                i = 1
            pos = heads[i]
            heads[i] = next(its[i], None)
            if pos != last:
                last = pos
                yield pos

    def address_rows(self, address, role="any", reverse=True, limit=None, lo=None, hi=None, skip=0):
        """Typed rows touching ``address`` (as sender, recipient, or either), in height order (descending by
        default; rows within a block keep their order). ``lo``/``hi`` bound the height."""
        out, cache = [], {}
        with self.store.txn() as txn:
            def emit(group):
                # within one block a reverse scan yields idx descending: restore insertion order per height
                # BEFORE skip/limit, so a limit that cuts a block keeps its FIRST rows (not its coinbase)
                nonlocal skip
                for h, idx in (reversed(group) if reverse else group):
                    if skip:
                        skip -= 1
                        continue
                    r = self._row_at(txn, h, idx, cache)
                    if r is None:
                        continue
                    out.append(r)
                    if limit and len(out) >= limit:
                        return True
                return False

            group = []
            for pos in self.address_positions(txn, address, role, reverse, lo, hi):
                if group and pos[0] != group[0][0]:
                    if emit(group):
                        return out
                    group = []
                group.append(pos)
            if group:
                emit(group)
        return out

    def address_tx_count(self, address, role="any"):
        """Number of rows touching ``address`` (``role``: "any" | "sender" | "recipient"), from the index only."""
        with self.store.txn() as txn:
            return sum(1 for _ in self.address_positions(txn, address, role, False))

    def first_pubkey(self, address):
        """The public key of the first non-coinbase tx sent by ``address``, or None."""
        cache = {}
        with self.store.txn() as txn:
            for h, idx in self._addr_iter(txn, ROLE_SENDER, address, False):
                r = self._row_at(txn, h, idx, cache)
                if r is not None and r[REWARD] == 0:
                    return r[PUB]
        return None

    def reward_units_since(self, address, above_height):
        """Sum of coinbase rewards credited to ``address`` in blocks with height > above_height (units)."""
        total, cache = 0, {}
        with self.store.txn() as txn:
            for h, idx in self._addr_iter(txn, ROLE_RECIPIENT, address, True, lo=int(above_height) + 1):
                r = self._row_at(txn, h, idx, cache)
                if r is not None and r[REWARD] != 0:
                    total += units(r[REWARD])
        return total

    # ------------------------------------------------------------------ balances
    def balance_parts(self, address):
        """(credited amount, rewards, debited amount, fees) for ``address`` in integer units."""
        with self.store.txn() as txn:
            v = txn.get(self.bal, _abytes(address))
        return tuple(_bal_unpack(v)) if v is not None else (0, 0, 0, 0)

    def balance_units(self, address):
        c, r, d, f = self.balance_parts(address)
        return c + r - d - f

    def balance(self, address):
        """The confirmed ledger balance as a Decimal (8 dp)."""
        return amounts.to_decimal(self.balance_units(address))

    def address_count(self):
        return self.store.stat(self.bal)["entries"]

    def iter_balances(self):
        """Yield (address, (credit, reward, debit, fee)) for every address."""
        with self.store.txn() as txn:
            for k, v in txn.iterate(self.bal):
                yield bytes(k).decode("utf-8", "replace"), tuple(_bal_unpack(v))

    # ------------------------------------------------------------------ txid / pubkey registry
    def txid_height(self, txid):
        with self.store.txn() as txn:
            v = txn.get(self.txid, self._txid_key(txid))
        return _uh(v) if v is not None else None

    def pkref_get(self, address):
        with self.store.txn() as txn:
            v = txn.get(self.pkref, _abytes(address))
        return bytes(v)[8:] if v is not None else None

    def pkref_first_seen(self, address):
        with self.store.txn() as txn:
            v = txn.get(self.pkref, _abytes(address))
        return _uh(bytes(v)[:8]) if v is not None else None

    def pkref_count(self):
        return self.store.stat(self.pkref)["entries"]

    def txid_count(self):
        return self.store.stat(self.txid)["entries"]

    # ------------------------------------------------------------------ snapshot / lifecycle
    def stream_snapshot(self, fileobj):
        """doc/43 DB-direct snapshot serving: stream a consistent, compacted MVCC image of the whole ledger
        straight to a writable binary file object, generated on demand from the live store."""
        self.store.copy_to_fileobj(fileobj, compact=True)

    def sync(self):
        self.store.sync()

    def close(self):
        self.store.close()


class TxidView:
    """``node.txid_index`` — the post-fork content-txid index inside the ledger."""

    def __init__(self, store):
        self.store = store

    def height_of(self, txid):
        return self.store.txid_height(txid)

    def contains(self, txid, max_height=None):
        h = self.store.txid_height(txid)
        return h is not None and (max_height is None or h <= int(max_height))

    def count(self):
        return self.store.txid_count()


class PubkeyView:
    """``node.pk_registry`` — the post-fork address -> pubkey registry inside the ledger (doc/40)."""

    def __init__(self, store):
        self.store = store

    def get(self, address):
        return self.store.pkref_get(address)

    def first_seen(self, address):
        return self.store.pkref_first_seen(address)

    def count(self):
        return self.store.pkref_count()


def as_ledger(obj):
    """The BlockStore behind ``obj`` — a BlockStore itself, or anything holding one as ``.store`` (DbHandler)."""
    return obj if isinstance(obj, BlockStore) else getattr(obj, "store", obj)


# --------------------------------------------------------------------------- process-wide handle
_OPEN = {}
_OPEN_LOCK = threading.Lock()


def open_ledger(path, **kw):
    """The ONE BlockStore for ``path`` in this process (an LMDB env must not be opened twice per process)."""
    key = os.path.realpath(path)
    with _OPEN_LOCK:
        st = _OPEN.get(key)
        if st is None:
            st = BlockStore(path, **kw)
            _OPEN[key] = st
        return st


def close_ledger(path):
    key = os.path.realpath(path)
    with _OPEN_LOCK:
        st = _OPEN.pop(key, None)
    if st is not None:
        st.close()
