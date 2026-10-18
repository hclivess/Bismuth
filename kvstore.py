"""
Key/value store abstraction over LMDB (doc/36).

ONE small interface + an ``open_store`` factory, used by the ledger (``block_store``) and the
tokens/aliases plugin store (``token_index``):

    store = open_store("lmdb", path, dbs=["blocks", "diff"], map_size=64 * KVStore.GIB)
    store = open_store("mdbx", path, dbs=[...])          # the day an MDBX binding is pinned

  * ``txn(write=False)`` — a read snapshot. Many concurrent (LMDB MVCC).
  * ``txn(write=True)``  — the single writer. Short-lived, per block.
  * ``range`` / ``iterate`` — ordered cursor scans (``set_range``).

The LMDB wrapper is a THIN passthrough: ``KVTxn`` holds the raw ``lmdb.Transaction`` and forwards
``get``/``put``/``delete`` straight to it with no per-op allocation or copy.

Codec centralizes (de)serialization: msgpack, with a JSON fallback.

Deps: ``lmdb``, ``msgpack`` (optional — JSON fallback). ``mdbx`` imported lazily, optional.
"""
from __future__ import annotations

import os
import struct


# --------------------------------------------------------------------------- store paths
def store_path(ledger_path, name):
    """Path of a derived LMDB store, NAMESPACED BY LEDGER FILENAME — ``<name>-<ledger>``.

    Every store is namespaced per ledger name, so mainnet (``static/ledger.db`` -> ``blockstore-ledger.db``),
    testnet and regnet (``static/regmode.db`` -> ``blockstore-regmode.db``) never share a directory — a
    regnet run can never hand its blocks to a mainnet node, nor wipe a mainnet store (the doc/18
    pollution class).
    """
    base = os.path.basename(str(ledger_path)) or "ledger.db"
    return os.path.join(os.path.dirname(str(ledger_path)) or ".", "%s-%s" % (name, base))


# --------------------------------------------------------------------------- Codec
class Codec:
    """Centralized key/value (de)serialization. Default value codec is msgpack with the SAME
    JSON-fallback the existing stores use (so on-disk bytes are identical). Keys are passed through
    as raw bytes — stores own their own key encoding (big-endian height, txid bytes, ...) and the KV
    layer never reinterprets them, which keeps ordered range scans engine-independent.

    NOTE: unpack uses ``strict_map_key=False`` INTENTIONALLY and uniformly for every migrated store — it
    only relaxes msgpack's rejection of non-str/bytes map keys, has no effect on the list/scalar values the
    current stores hold, and keeps the codec tolerant for stores whose
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

    def count(self, db):  # pragma: no cover
        """Entry count of `db` AS SEEN INSIDE THIS txn (reflects uncommitted puts in the current write
        txn — unlike KVStore.stat which opens a separate read snapshot). Used by stores that assign a
        monotonic id = current entry count within the same write txn (block_store's pubkey dedup)."""
        raise NotImplementedError

    def drop(self, db):  # pragma: no cover
        """Clear ALL entries in `db` (keep the empty sub-db). The drop+re-index rebuild primitive."""
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

    def copy_to(self, dst_path, compact=True):  # pragma: no cover
        """Consistent ONLINE copy of the whole store into ``dst_path`` (created/cleared by the backend),
        usable WHILE a writer is active (LMDB ``env.copy`` — MVCC-consistent). ``compact`` drops free
        pages. The copy is an LMDB subdir with data.mdb, readable by reopening at ``dst_path``."""
        raise NotImplementedError

    def copy_to_fileobj(self, fileobj, compact=True):  # pragma: no cover
        """Stream a consistent ONLINE copy of the whole store straight to a writable binary file object
        (an HTTP socket, a pipe) WITHOUT materializing an intermediate file on disk — the on-demand,
        no-doubled-file variant of ``copy_to`` (doc/43 DB-direct snapshot serving). MVCC-consistent while
        a writer is active, exactly like ``copy_to``. The streamed bytes are the backend's native env
        image: re-openable by writing them to the backend's on-disk file (LMDB: ``<dir>/data.mdb``) and
        reopening the same backend there. The caller MUST flush any buffered writes on ``fileobj`` first
        (the copy bypasses Python-level buffering by writing to the raw fd). Only the LMDB backend
        implements it (``env.copyfd``); other backends raise NotImplementedError so the caller falls back."""
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

    def count(self, db):
        return self._t.stat(db)["entries"]   # reflects uncommitted puts in this write txn

    def drop(self, db):
        self._t.drop(db, delete=False)   # clear all entries, keep the (empty) sub-db


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
    def __init__(self, path, *, dbs, map_size, readonly=False, sync=True, lock=None):
        import lmdb
        names = list(dbs) if dbs else []
        # lock defaults to (not readonly). A store may force lock=True even when readonly so a separate
        # READER process registers in LMDB's reader table and can read consistently WHILE the node
        # writes (block_store's concurrent-reader integration test relies on exactly this).
        use_lock = (not readonly) if lock is None else bool(lock)
        self.env = lmdb.open(
            path, subdir=True, max_dbs=max(1, len(names)), map_size=map_size,
            readonly=readonly, lock=use_lock, sync=sync, metasync=sync, max_readers=2048,
        )
        self._dbs = {}
        for name in names:
            try:
                self._dbs[name] = self.env.open_db(name.encode())
            except lmdb.ReadonlyError:
                # a read-only open of a store written before this sub-db existed: it is simply absent
                self._dbs[name] = None

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

    def copy_to(self, dst_path, compact=True):
        # LMDB's env.copy takes an MVCC snapshot, so it is consistent even while the node writes; a raw cp
        # would capture a mid-write mmap. compact=True drops free pages so the bootstrap is small. The dst
        # must be a fresh empty directory (LMDB subdir convention) — same precondition the snapshot tool had.
        import os
        if os.path.exists(dst_path):
            import shutil
            shutil.rmtree(dst_path)
        os.makedirs(dst_path)
        self.env.copy(dst_path, compact=compact)

    def copy_to_fileobj(self, fileobj, compact=True):
        # LMDB's env.copyfd writes an MVCC-consistent image straight to a file descriptor, so the snapshot
        # streams directly to an HTTP socket with NO intermediate file on disk and NO doubled ledger copy
        # (doc/43 DB-direct serve). It is consistent even while the node keeps writing. The caller must have
        # flushed `fileobj` first — copyfd bypasses Python buffering by writing to the raw fd, so any
        # still-buffered bytes (e.g. HTTP headers) would otherwise land AFTER the env image. compact=True
        # drops free pages so the streamed snapshot is as small as a freshly built one.
        self.env.copyfd(fileobj.fileno(), compact=compact)

    def close(self):
        self.env.close()


# --------------------------------------------------------------------------- MDBX backend (lazy/optional)
class MdbxKVStore(KVStore):
    """Adapter behind the same factory. The mdbx binding is imported LAZILY so MDBX is a one-arg
    factory choice the day a binding is added — and a clean, actionable error (not an import crash at
    module load) if it isn't installed yet. MDBX is API-compatible with LMDB's cursor model, so once
    a binding exists this reuses the LMDB txn shape almost verbatim; until then it raises on
    construction only."""

    def __init__(self, path, *, dbs, map_size, readonly=False, sync=True, lock=None):
        try:
            import mdbx  # noqa: F401  (libmdbx python binding; optional)
        except ImportError as e:  # pragma: no cover - exercised only without a binding
            raise RuntimeError(
                "mdbx backend requested but no 'mdbx' python binding is installed. "
                "Install one (e.g. libmdbx's python binding) or use backend='lmdb'."
            ) from e
        # When a binding is present, wire it here against the SAME KVTxn shape as LMDB.
        raise NotImplementedError(  # pragma: no cover
            "mdbx binding detected but the adapter wiring is intentionally deferred to the day a "
            "binding is pinned (the seam + factory choice exist now)."
        )


# --------------------------------------------------------------------------- factory
_BACKENDS = {
    "lmdb": LmdbKVStore,
    "mdbx": MdbxKVStore,
}


def open_store(backend, path, *, dbs, map_size=2 * KVStore.GIB, readonly=False, sync=True, lock=None):
    """Open a KVStore on ``backend`` in {lmdb, mdbx}. ``dbs`` is the list of named sub-dbs; ``map_size``
    is the map size. ``lock`` overrides the default LMDB locking (None => not readonly); set lock=True to
    keep a readonly reader in the reader table while a writer is active. This is the single seam the node's stores construct through,
    so swapping the engine is a one-arg change."""
    try:
        cls = _BACKENDS[backend]
    except KeyError:
        raise ValueError(
            "unknown KV backend %r (choose from %s)" % (backend, ", ".join(sorted(_BACKENDS)))
        )
    return cls(path, dbs=dbs, map_size=map_size, readonly=readonly, sync=sync, lock=lock)
