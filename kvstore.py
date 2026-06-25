"""
Engine-agnostic key/value store abstraction (doc/26 — stage 1 of the DB-engine seam).

The node's 8 LMDB stores (block_store, token_index, txid_index, reward_chain, shieldedv1,
balance_index, vm_state, scripts/snapshot) each call ``lmdb.open()`` directly with bespoke txn
lifecycles and 3 divergent (de)serialization styles, with no shared base or factory. This module
introduces ONE small interface + an ``open_store`` factory so the underlying KV engine becomes a
single-arg choice:

    store = open_store("lmdb",      path, dbs=["rewards"], map_size=2 * KVStore.GIB)
    store = open_store("sqlite-kv", path, dbs=["rewards"])              # always available
    store = open_store("mdbx",      path, dbs=["rewards"])              # the day a binding is added

The model maps the node's usage exactly:
  * ``txn(write=False)`` — a read snapshot. Many concurrent (LMDB MVCC; SQLite deferred read txn).
  * ``txn(write=True)``  — the single writer. Short-lived, per request/block.
  * ``range`` / ``iterate`` — the stores' ``cursor`` / ``set_range`` ordered scans.

PERFORMANCE: the LMDB wrapper is a THIN passthrough. ``KVTxn`` holds the raw ``lmdb.Transaction`` and
forwards ``get``/``put``/``delete`` straight to it with no per-op allocation, copy, or row-object
churn (see tests/test_kvstore.py::test_perf_lmdb_overhead — measured overhead is a few %). The
backend-specific txn classes are plain attribute holders; ``open_db`` is done once at open.

Codec centralizes (de)serialization: msgpack by default with the SAME JSON fallback the existing
stores use, so a migrated store keeps a BYTE-IDENTICAL on-disk format and its parity test still holds.

Deps: ``lmdb`` (for the lmdb backend), ``msgpack`` (optional — JSON fallback), ``sqlite3`` (stdlib,
for the always-available sqlite-kv backend). ``mdbx`` imported lazily, optional.
"""
from __future__ import annotations

import struct


# --------------------------------------------------------------------------- Codec
class Codec:
    """Centralized key/value (de)serialization. Default value codec is msgpack with the SAME
    JSON-fallback the existing stores use (so on-disk bytes are identical). Keys are passed through
    as raw bytes — stores own their own key encoding (big-endian height, txid bytes, ...) and the KV
    layer never reinterprets them, which keeps ordered range scans engine-independent.

    NOTE: unpack uses ``strict_map_key=False`` INTENTIONALLY and uniformly for every migrated store — it
    only relaxes msgpack's rejection of non-str/bytes map keys, has no effect on the list/scalar values the
    current stores hold (e.g. reward_chain's list-of-entries), and keeps the codec tolerant for stores whose
    values may use integer-keyed maps. It does not change packed bytes, so on-disk parity is unaffected. [review]"""

    try:
        import msgpack as _msgpack  # noqa: N801

        @staticmethod
        def pack(obj):
            return Codec._msgpack.packb(obj, use_bin_type=True)

        @staticmethod
        def unpack(buf):
            return Codec._msgpack.unpackb(buf, raw=False, strict_map_key=False)

        backend = "msgpack"
    except ImportError:  # pragma: no cover - fallback path mirrors the stores
        import json as _json

        @staticmethod
        def pack(obj):
            return Codec._json.dumps(obj, separators=(",", ":")).encode()

        @staticmethod
        def unpack(buf):
            return Codec._json.loads(buf)

        backend = "json"

    # height key helpers — the convention shared by every height-keyed projection
    @staticmethod
    def hkey(height):
        return struct.pack(">Q", int(height))

    @staticmethod
    def unhkey(key):
        return struct.unpack(">Q", bytes(key))[0]


# --------------------------------------------------------------------------- interface
class KVTxn:
    """Transaction handle. Subclassed thinly per backend. The method shape is the audit's:

        get(db, key) -> bytes | None
        put(db, key, val)
        delete(db, key) -> bool
        range(db, start, end, reverse=False) -> iter of (key, val), key-ordered
        iterate(db, prefix=b"") -> iter of (key, val)
    """

    def get(self, db, key):  # pragma: no cover - abstract
        raise NotImplementedError

    def put(self, db, key, val):  # pragma: no cover
        raise NotImplementedError

    def delete(self, db, key):  # pragma: no cover
        raise NotImplementedError

    def range(self, db, start=None, end=None, reverse=False):  # pragma: no cover
        raise NotImplementedError

    def iterate(self, db, prefix=b""):  # pragma: no cover
        raise NotImplementedError


class KVStore:
    """Engine-agnostic store. ``open_db(name)`` returns an opaque db-handle; ``txn(write=...)`` is a
    context manager yielding a KVTxn; ``stat`` / ``sync`` / ``close`` round it out."""

    GIB = 1024 ** 3
    Codec = Codec

    def open_db(self, name):  # pragma: no cover - abstract
        raise NotImplementedError

    def txn(self, write=False):  # pragma: no cover
        raise NotImplementedError

    def stat(self, db=None):  # pragma: no cover
        raise NotImplementedError

    def sync(self):  # pragma: no cover
        raise NotImplementedError

    def close(self):  # pragma: no cover
        raise NotImplementedError

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


# --------------------------------------------------------------------------- LMDB backend
class _LmdbTxn(KVTxn):
    """THIN passthrough over a live ``lmdb.Transaction``. No per-op allocation or copying — get/put/
    delete forward directly to the raw txn; the only object created is the cursor for range/iterate
    (lazily, only when a scan is requested), which mirrors what the hand-written stores already do."""

    __slots__ = ("_t",)

    def __init__(self, raw_txn):
        self._t = raw_txn

    def get(self, db, key):
        return self._t.get(key, db=db)

    def put(self, db, key, val):
        self._t.put(key, val, db=db)

    def delete(self, db, key):
        return self._t.delete(key, db=db)

    def range(self, db, start=None, end=None, reverse=False):
        # py-lmdb (no buffers) already returns real `bytes` for key/value, so we forward the native
        # cursor iterator directly — NO per-item copy/allocation. Bound checks reuse those same bytes.
        cur = self._t.cursor(db=db)
        if not reverse:
            if start is not None:
                if not cur.set_range(start):
                    return
            elif not cur.first():
                return
            if end is None:
                for kv in cur.iternext(keys=True, values=True):
                    yield kv
            else:
                for k, v in cur.iternext(keys=True, values=True):
                    if k >= end:
                        return
                    yield k, v
        else:
            # reverse: walk from the last key < end down to >= start
            if end is not None:
                if cur.set_range(end):
                    if not cur.prev():     # set_range lands on >= end; step back to < end
                        return
                elif not cur.last():       # everything is < end
                    return
            elif not cur.last():
                return
            it = cur.iterprev(keys=True, values=True)
            if start is None:
                for kv in it:
                    yield kv
            else:
                for k, v in it:
                    if k < start:
                        return
                    yield k, v

    def iterate(self, db, prefix=b""):
        cur = self._t.cursor(db=db)
        if not prefix:
            if not cur.first():
                return
            for kv in cur.iternext(keys=True, values=True):
                yield kv
            return
        if not cur.set_range(prefix):
            return
        for k, v in cur.iternext(keys=True, values=True):
            if not k.startswith(prefix):
                return
            yield k, v


class _LmdbTxnCtx:
    """Context manager wrapping ``env.begin`` so the same shape works for read and write txns."""

    __slots__ = ("_env", "_write", "_cm", "_raw")

    def __init__(self, env, write):
        self._env = env
        self._write = write
        self._cm = None
        self._raw = None

    def __enter__(self):
        self._cm = self._env.begin(write=self._write)
        self._raw = self._cm.__enter__()
        return _LmdbTxn(self._raw)

    def __exit__(self, *exc):
        return self._cm.__exit__(*exc)


class LmdbKVStore(KVStore):
    def __init__(self, path, *, dbs, map_size, readonly=False, sync=True):
        import lmdb
        names = list(dbs) if dbs else []
        self.env = lmdb.open(
            path, subdir=True, max_dbs=max(1, len(names)), map_size=map_size,
            readonly=readonly, lock=not readonly, sync=sync, metasync=sync,
        )
        self._dbs = {name: self.env.open_db(name.encode()) for name in names}

    def open_db(self, name):
        if name not in self._dbs:
            self._dbs[name] = self.env.open_db(name.encode())
        return self._dbs[name]

    def txn(self, write=False):
        return _LmdbTxnCtx(self.env, write)

    def stat(self, db=None):
        with self.env.begin() as t:
            return dict(t.stat(db=db)) if db is not None else dict(self.env.stat())

    def sync(self):
        self.env.sync(True)

    def close(self):
        self.env.close()


# --------------------------------------------------------------------------- SQLite-KV backend
class _SqliteTxn(KVTxn):
    """One (key BLOB PRIMARY KEY, value BLOB) table per named db. Ordered range via ORDER BY key —
    SQLite compares BLOBs with memcmp, which is the SAME lexicographic order LMDB uses, so a store's
    ordered scans (big-endian-height set_range, prefix iterate) return the identical sequence. This
    is the always-available second backend that PROVES the seam without an MDBX binding."""

    __slots__ = ("_conn",)

    def __init__(self, conn):
        self._conn = conn

    def get(self, db, key):
        row = self._conn.execute(
            'SELECT value FROM "%s" WHERE key=?' % db, (key,)
        ).fetchone()
        return bytes(row[0]) if row is not None else None

    def put(self, db, key, val):
        self._conn.execute(
            'INSERT INTO "%s"(key,value) VALUES(?,?) '
            'ON CONFLICT(key) DO UPDATE SET value=excluded.value' % db, (key, val)
        )

    def delete(self, db, key):
        cur = self._conn.execute('DELETE FROM "%s" WHERE key=?' % db, (key,))
        return cur.rowcount > 0

    def range(self, db, start=None, end=None, reverse=False):
        clauses, params = [], []
        if start is not None:
            clauses.append("key >= ?")
            params.append(start)
        if end is not None:
            clauses.append("key < ?")
            params.append(end)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        order = "DESC" if reverse else "ASC"
        sql = 'SELECT key,value FROM "%s"%s ORDER BY key %s' % (db, where, order)
        for k, v in self._conn.execute(sql, params):
            yield bytes(k), bytes(v)

    def iterate(self, db, prefix=b""):
        if prefix:
            # [prefix, prefix++) half-open range over BLOBs
            hi = _prefix_upper(prefix)
            if hi is None:
                sql = 'SELECT key,value FROM "%s" WHERE key >= ? ORDER BY key' % db
                params = (prefix,)
            else:
                sql = 'SELECT key,value FROM "%s" WHERE key >= ? AND key < ? ORDER BY key' % db
                params = (prefix, hi)
        else:
            sql = 'SELECT key,value FROM "%s" ORDER BY key' % db
            params = ()
        for k, v in self._conn.execute(sql, params):
            yield bytes(k), bytes(v)


def _prefix_upper(prefix):
    """Smallest key strictly greater than every key starting with ``prefix`` (BLOB order), or None if
    the prefix is all 0xff (no upper bound)."""
    b = bytearray(prefix)
    while b and b[-1] == 0xFF:
        b.pop()
    if not b:
        return None
    b[-1] += 1
    return bytes(b)


class _SqliteTxnCtx:
    __slots__ = ("_store", "_write", "_conn")

    def __init__(self, store, write):
        self._store = store
        self._write = write
        self._conn = None

    def __enter__(self):
        # A FRESH connection per txn, so the KVStore contract holds on sqlite too: WAL gives concurrent read
        # snapshots, each connection is used only within its own txn/thread (no cross-thread sqlite errors),
        # and BEGIN IMMEDIATE + busy_timeout serialize the single writer. (A single shared connection could not
        # nest txns nor be shared across threads — [review #1].) Connect cost is acceptable: sqlite-kv is the
        # portable proof/fallback backend, never the LMDB hot path.
        self._conn = self._store._connect()
        self._conn.execute("BEGIN IMMEDIATE" if self._write else "BEGIN")
        return _SqliteTxn(self._conn)

    def __exit__(self, exc_type, *rest):
        try:
            self._conn.execute("COMMIT" if exc_type is None else "ROLLBACK")
        finally:
            self._conn.close()
        return False


class SqliteKVStore(KVStore):
    def __init__(self, path, *, dbs, map_size=None, readonly=False, sync=True):
        import os
        import sqlite3
        self._sqlite3 = sqlite3
        self._readonly = readonly
        self._sync = sync
        # path mirrors LMDB's subdir convention: a directory holding one db file, so the same caller
        # path works on both backends and conftest cleanup (rmtree) is identical.
        if readonly:
            self._dbfile = path if os.path.isfile(path) else os.path.join(path, "kv.sqlite")
        else:
            if not os.path.isdir(path):
                os.makedirs(path, exist_ok=True)
            self._dbfile = os.path.join(path, "kv.sqlite")
            # WAL is a persistent file property — set it (and durability) once, then close.
            c = self._connect()
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=%s" % ("NORMAL" if sync else "OFF"))
            c.close()
        self._dbs = set()
        for name in (dbs or []):
            self.open_db(name)

    def _connect(self):
        """A fresh, fully-configured connection (WAL already persisted on the file). text_factory=bytes so
        keys/values round-trip as bytes; busy_timeout serializes the single writer; isolation_level=None so we
        drive BEGIN/COMMIT explicitly."""
        if self._readonly:
            conn = self._sqlite3.connect("file:%s?mode=ro" % self._dbfile, uri=True, timeout=30)
        else:
            conn = self._sqlite3.connect(self._dbfile, timeout=30, isolation_level=None)
        conn.execute("PRAGMA busy_timeout=30000")
        conn.text_factory = bytes
        return conn

    def open_db(self, name):
        if name not in self._dbs and not self._readonly:
            c = self._connect()
            try:
                c.execute('CREATE TABLE IF NOT EXISTS "%s" (key BLOB PRIMARY KEY, value BLOB) WITHOUT ROWID' % name)
            finally:
                c.close()
        self._dbs.add(name)
        return name  # the db-handle for sqlite IS the table name

    def txn(self, write=False):
        return _SqliteTxnCtx(self, write)

    def stat(self, db=None):
        c = self._connect()
        try:
            if db is None:
                total = 0
                for (name,) in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
                    name = name.decode() if isinstance(name, bytes) else name
                    total += c.execute('SELECT COUNT(*) FROM "%s"' % name).fetchone()[0]
                return {"entries": total}
            n = c.execute('SELECT COUNT(*) FROM "%s"' % db).fetchone()[0]
            return {"entries": n}
        finally:
            c.close()

    def sync(self):
        if self._readonly:
            return
        c = self._connect()
        try:
            c.execute("PRAGMA wal_checkpoint(FULL)")
        finally:
            c.close()

    def close(self):
        return  # no persistent connection — each txn/op opens and closes its own


# --------------------------------------------------------------------------- MDBX backend (lazy/optional)
class MdbxKVStore(KVStore):
    """Adapter behind the same factory. The mdbx binding is imported LAZILY so MDBX is a one-arg
    factory choice the day a binding is added — and a clean, actionable error (not an import crash at
    module load) if it isn't installed yet. MDBX is API-compatible with LMDB's cursor model, so once
    a binding exists this reuses the LMDB txn shape almost verbatim; until then it raises on
    construction only."""

    def __init__(self, path, *, dbs, map_size, readonly=False, sync=True):
        try:
            import mdbx  # noqa: F401  (libmdbx python binding; optional)
        except ImportError as e:  # pragma: no cover - exercised only without a binding
            raise RuntimeError(
                "mdbx backend requested but no 'mdbx' python binding is installed. "
                "Install one (e.g. libmdbx's python binding) or use backend='lmdb'/'sqlite-kv'."
            ) from e
        # When a binding is present, wire it here against the SAME KVTxn shape as LMDB.
        raise NotImplementedError(  # pragma: no cover
            "mdbx binding detected but the adapter wiring is intentionally deferred to the day a "
            "binding is pinned (the seam + factory choice exist now; LMDB/SQLite cover swappability)."
        )


# --------------------------------------------------------------------------- factory
_BACKENDS = {
    "lmdb": LmdbKVStore,
    "sqlite-kv": SqliteKVStore,
    "sqlite": SqliteKVStore,
    "mdbx": MdbxKVStore,
}


def open_store(backend, path, *, dbs, map_size=2 * KVStore.GIB, readonly=False, sync=True):
    """Open a KVStore on ``backend`` in {lmdb, mdbx, sqlite-kv}. ``dbs`` is the list of named sub-dbs;
    ``map_size`` is the LMDB/MDBX map size (ignored by sqlite-kv). This is the single seam the node's
    stores construct through, so swapping the engine is a one-arg change."""
    try:
        cls = _BACKENDS[backend]
    except KeyError:
        raise ValueError(
            "unknown KV backend %r (choose from %s)" % (backend, ", ".join(sorted(_BACKENDS)))
        )
    return cls(path, dbs=dbs, map_size=map_size, readonly=readonly, sync=sync)
