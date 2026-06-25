"""
Maintained O(1) per-address balance index in integer atomic units (roadmap doc/17 phase 7, step 2).

The authoritative balance (`essentials.ledger_balance3`) is a full-table aggregate — on the 22 GB
mainnet ledger it does not finish in 200 s. This index keeps the same quantity per address, but as a
running total updated on block apply / reversed on rollback, so a balance read is a single O(1) LMDB
lookup.

It tracks exactly what the authoritative query does:
    credit(addr) = SUM(amount + reward)  over rows where recipient == addr
    debit(addr)  = SUM(amount + fee)      over rows where address   == addr
    balance(addr) = credit - debit
in **integer atomic units**. Integer addition is exact and order-independent, so the running total is
byte-identical to the full aggregate — *provided amounts are stored as integer units* (the phase-2
`ledger_integer_amounts` mode; regnet runs in this mode, so the tests prove the bit-match). With
legacy float amounts the authoritative aggregate is float arithmetic and cannot be bit-matched, which
is why this step depends on the integer cutover.

Storage only / standalone here: it is rebuildable from the ledger and exposes apply/rollback, but is
NOT yet wired into the digester's commit path (that lands behind a config flag, replay-validated).

Migrated onto the engine-agnostic KV abstraction (``kvstore.open_store``, doc/26 storage stage 1): the
underlying KV engine (LMDB <-> MDBX <-> sqlite-kv) is now a single ``backend=`` factory arg instead of a
direct ``lmdb.open()`` call. The public method surface AND the on-disk byte format are UNCHANGED — the
key is still the raw address bytes and the value the same msgpack ``[credit_units, debit_units]`` list
(``Codec`` from kvstore is the exact same msgpack ``use_bin_type=True`` / JSON-fallback the store used
before) — so the bit-match-vs-``ledger_balance3`` parity proof in the tests holds byte-for-byte on the
lmdb backend, and the SAME store now also runs on sqlite-kv (proving the seam is engine-independent).

Deps: ``kvstore`` (which needs ``lmdb`` for the lmdb backend; ``msgpack`` optional — JSON fallback).
"""
from kvstore import Codec, KVStore, open_store

import amounts


# hf2 Stage-4 (doc/40 core-indexes §2): the balance value is two fixed-width u128 LITTLE-endian counters
# (credit_units, debit_units) — TRUE bytes, not a msgpack list (~19B -> 32B fixed, branch-free decode).
# Credit/debit are cumulative running sums (unbounded-growing), so u128 gives ceiling-free headroom. The
# non-negativity guard makes the running-total invariant explicit and converts the silent OverflowError
# (negative -> to_bytes) into a loud, named error if a future caller ever rolls back out of apply order.
def _pack(cd):
    c, d = cd
    if c < 0 or d < 0:
        raise ValueError("balance_index running totals must stay non-negative: c=%d d=%d" % (c, d))
    return c.to_bytes(16, "little") + d.to_bytes(16, "little")


def _unpack(v):
    v = bytes(v)
    return int.from_bytes(v[:16], "little"), int.from_bytes(v[16:], "little")

_GIB = KVStore.GIB

# field indices in a 12-column ledger row
_ADDR, _RECIP, _AMOUNT, _FEE, _REWARD = 2, 3, 4, 8, 9


def _key(addr):
    return addr.encode() if isinstance(addr, str) else addr


def _units(v):
    # amount/fee/reward in integer-storage mode are integers (or int-valued); be tolerant of int-strings
    return int(v)


def _fold(acc, addr, recip, amt, fee, reward):
    """Single source of truth for the credit/debit folding, shared by the per-block apply path (12-column
    rows, via _accumulate) and the column-narrowed full rebuild — so both produce identical totals."""
    acc.setdefault(recip, [0, 0])[0] += amt + reward   # recipient credited amount + reward
    acc.setdefault(addr, [0, 0])[1] += amt + fee       # sender debited amount + fee


def _accumulate(rows, acc):
    """Fold a block's 12-column rows into ``acc`` (address -> [credit_units, debit_units])."""
    for r in rows:
        _fold(acc, r[_ADDR], r[_RECIP], _units(r[_AMOUNT]), _units(r[_FEE]), _units(r[_REWARD]))
    return acc


class BalanceIndex:
    def __init__(self, path, map_size=4 * _GIB, readonly=False, sync=True, backend="lmdb"):
        self.store = open_store(backend, path, dbs=["bal"], map_size=map_size,
                                readonly=readonly, sync=sync)
        self.db = self.store.open_db("bal")
        # kept for back-compat with callers/tests that introspect the env directly (lmdb backend only)
        self.env = getattr(self.store, "env", None)

    def _get(self, txn, addr):
        v = txn.get(self.db, _key(addr))
        if v is None:
            return 0, 0
        c, d = _unpack(v)
        return int(c), int(d)

    def _apply(self, acc, sign):
        with self.store.txn(write=True) as txn:
            for addr, (dc, dd) in acc.items():
                c, d = self._get(txn, addr)
                txn.put(self.db, _key(addr), _pack([c + sign * dc, d + sign * dd]))

    # --- maintenance --------------------------------------------------------
    def apply_rows(self, rows):
        """Apply one block's ledger rows (amount/fee/reward are integer units)."""
        self._apply(_accumulate(rows, {}), +1)

    def rollback_rows(self, rows):
        """Reverse one block's ledger rows (a reorg undoing the apply)."""
        self._apply(_accumulate(rows, {}), -1)

    def rebuild_from_ledger(self, ledger_path):
        """(Re)build the whole index from a SQLite ledger. Accumulates in memory, then bulk-writes.

        Scans the ENTIRE transactions table with NO height filter — including the negative-height
        "mirror" reward rows (Development Reward / Hypernode Payouts) — because the authoritative
        ledger_balance3 has no height filter either, so the index must see every row to bit-match.
        """
        import sqlite3
        conn = sqlite3.connect("file:%s?mode=ro" % ledger_path, uri=True, timeout=120)
        conn.text_factory = str
        try:
            return self.rebuild_from_cursor(conn.cursor())
        finally:
            conn.close()

    def rebuild_from_cursor(self, cursor):
        """(Re)build from an already-open DB cursor — the live maintenance / post-rollback path, where
        the node's own connection IS the ledger (so we needn't know regnet-vs-mainnet file paths). Scans
        the ENTIRE transactions table (positive txs + the negative-height reward mirrors) so the index
        bit-matches ledger_balance3 — i.e. the concluded dev/HN rewards are already baked into balances,
        exactly what the hard-fork snapshot will persist when the mirror blocks are dropped."""
        acc = {}
        # Column-narrowed: pull only the 5 fields _fold needs instead of SELECT * — avoids dragging every
        # row's ~1KB public_key + signature blobs across the sqlite->Python boundary, which was the
        # dominant cost of this full-ledger rebuild on the 23GB chain (it runs at boot AND on every reorg).
        # Still scans EVERY row with no height filter, so the result stays byte-identical to ledger_balance3.
        for addr, recip, amt, fee, reward in cursor.execute(
                "SELECT address, recipient, amount, fee, reward FROM transactions"):
            _fold(acc, addr, recip, _units(amt), _units(fee), _units(reward))
        with self.store.txn(write=True) as txn:
            txn.drop(self.db)                          # fresh rebuild
            for addr, (c, d) in acc.items():
                txn.put(self.db, _key(addr), _pack([c, d]))
        return len(acc)

    # --- read ---------------------------------------------------------------
    def get_balance_units(self, address):
        with self.store.txn() as txn:
            c, d = self._get(txn, address)
        return c - d

    def get_balance(self, address):
        """Balance as a Decimal (atomic units -> decimal), matching the display edge."""
        return amounts.to_decimal(self.get_balance_units(address))

    def count(self):
        return self.store.stat(self.db)["entries"]

    def close(self):
        self.store.close()
