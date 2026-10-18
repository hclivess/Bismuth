"""Optipoolware's own persistence: ONE LMDB environment (``poolstore/``) via the node's ``kvstore``.

It holds the pool's local state only — never the ledger (chain data comes from the node REST):

  * ``shares``  : u64 seq -> JSON share record {address, shares, timestamp, paid, rate, name, workers, subname}
                  (the current round; paid rows move to ``archive`` after each payout run)
  * ``archive`` : u64 seq -> JSON share record (paid shares, kept for history)
  * ``nonces``  : nonce bytes -> b"" (share-reuse guard for the current round; cleared every payout run)
  * ``meta``    : b"seq" -> the monotonic record counter

Every mutation is one LMDB write transaction, so the pool server's HTTP threads, the payout thread and a
concurrent ``optiexplorer`` reader in another process all see consistent state with no retry loops.
The round is small (it is cleared hourly), so the aggregate queries are plain scans.
"""
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from kvstore import Codec, open_store  # noqa: E402

DEFAULT_PATH = "poolstore"          # relative to the working dir (the pool runs from the repo root)
_DBS = ["shares", "archive", "nonces", "meta"]
FIELDS = ("address", "shares", "timestamp", "paid", "rate", "name", "workers", "subname")


def _dump(rec):
    return json.dumps(rec, separators=(",", ":")).encode()


def _load(buf):
    return json.loads(bytes(buf))


def _paid(rec):
    return str(rec.get("paid")) == "1"


class PoolStore:
    def __init__(self, path=DEFAULT_PATH, map_size=1 << 30, readonly=False):
        if not readonly:
            os.makedirs(path, exist_ok=True)
        self.path = path
        self.store = open_store("lmdb", path, dbs=_DBS, map_size=map_size, readonly=readonly, lock=True)
        self.shares = self.store.open_db("shares")
        self.archive = self.store.open_db("archive")
        self.nonces = self.store.open_db("nonces")
        self.meta = self.store.open_db("meta")

    # ------------------------------------------------------------------ writes
    def _next_seq(self, txn):
        v = txn.get(self.meta, b"seq")
        n = (Codec.unhkey(v) if v is not None else 0) + 1
        txn.put(self.meta, b"seq", Codec.hkey(n))
        return Codec.hkey(n)

    def record_share(self, nonce, rec):
        """Atomically: refuse a reused ``nonce`` (returns False), else remember it and append the share
        record (returns True)."""
        nk = str(nonce).encode()
        with self.store.txn(write=True) as txn:
            if txn.get(self.nonces, nk) is not None:
                return False
            txn.put(self.nonces, nk, b"")
            txn.put(self.shares, self._next_seq(txn), _dump({f: rec.get(f) for f in FIELDS}))
            return True

    def nonce_seen(self, nonce):
        with self.store.txn() as txn:
            return txn.get(self.nonces, str(nonce).encode()) is not None

    def add_share(self, rec):
        """Append a share record without the nonce guard (operator tooling / tests)."""
        with self.store.txn(write=True) as txn:
            txn.put(self.shares, self._next_seq(txn), _dump({f: rec.get(f) for f in FIELDS}))

    def mark_paid(self, address):
        """Flag every share of ``address`` in the current round paid."""
        with self.store.txn(write=True) as txn:
            for k, v in list(txn.iterate(self.shares)):
                rec = _load(v)
                if rec.get("address") == address and not _paid(rec):
                    rec["paid"] = "1"
                    txn.put(self.shares, k, _dump(rec))

    def touch_timestamps(self, ts):
        """Stamp every share in the current round with ``ts`` (the post-payout round reset)."""
        with self.store.txn(write=True) as txn:
            for k, v in list(txn.iterate(self.shares)):
                rec = _load(v)
                rec["timestamp"] = ts
                txn.put(self.shares, k, _dump(rec))

    def close_round(self):
        """Move the paid shares to the archive and clear the nonce guard, in ONE transaction.
        Returns the number of archived records."""
        moved = 0
        with self.store.txn(write=True) as txn:
            for k, v in list(txn.iterate(self.shares)):
                if _paid(_load(v)):
                    txn.put(self.archive, k, v)
                    txn.delete(self.shares, k)
                    moved += 1
            txn.drop(self.nonces)
        return moved

    # ------------------------------------------------------------------ reads
    def all_shares(self):
        """Every share record of the current round, in insertion order."""
        with self.store.txn() as txn:
            return [_load(v) for _k, v in txn.iterate(self.shares)]

    def unpaid(self):
        return [r for r in self.all_shares() if not _paid(r)]

    def has_shares(self):
        with self.store.txn() as txn:
            return txn.count(self.shares) > 0

    def archived(self):
        with self.store.txn() as txn:
            return [_load(v) for _k, v in txn.iterate(self.archive)]

    def close(self):
        self.store.close()


def unpaid_sum_by_address(records):
    """``{address: sum(shares)}`` over the unpaid records (insertion order of first appearance)."""
    out = {}
    for r in records:
        if not _paid(r):
            out[r["address"]] = out.get(r["address"], 0) + float(r.get("shares") or 0)
    return {a: int(v) if float(v).is_integer() else v for a, v in out.items()}
