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

Deps: ``lmdb`` (required), ``msgpack`` (optional — JSON fallback).
"""
import lmdb

try:
    import msgpack

    def _pack(o):
        return msgpack.packb(o, use_bin_type=True)

    def _unpack(b):
        return msgpack.unpackb(b, raw=False)
except ImportError:  # pragma: no cover
    import json

    def _pack(o):
        return json.dumps(o).encode()

    def _unpack(b):
        return json.loads(b)

import amounts

_GIB = 1024 ** 3

# field indices in a 12-column ledger row
_ADDR, _RECIP, _AMOUNT, _FEE, _REWARD = 2, 3, 4, 8, 9


def _key(addr):
    return addr.encode() if isinstance(addr, str) else addr


def _units(v):
    # amount/fee/reward in integer-storage mode are integers (or int-valued); be tolerant of int-strings
    return int(v)


def _accumulate(rows, acc):
    """Fold a block's rows into ``acc`` (address -> [credit_units, debit_units])."""
    for r in rows:
        amt, fee, reward = _units(r[_AMOUNT]), _units(r[_FEE]), _units(r[_REWARD])
        acc.setdefault(r[_RECIP], [0, 0])[0] += amt + reward   # recipient credited amount + reward
        acc.setdefault(r[_ADDR], [0, 0])[1] += amt + fee       # sender debited amount + fee
    return acc


class BalanceIndex:
    def __init__(self, path, map_size=4 * _GIB, readonly=False, sync=True):
        self.env = lmdb.open(path, subdir=True, max_dbs=1, map_size=map_size,
                             readonly=readonly, lock=not readonly, sync=sync, metasync=sync)
        self.db = self.env.open_db(b"bal")

    def _get(self, txn, addr):
        v = txn.get(_key(addr), db=self.db)
        if v is None:
            return 0, 0
        c, d = _unpack(v)
        return int(c), int(d)

    def _apply(self, acc, sign):
        with self.env.begin(write=True) as txn:
            for addr, (dc, dd) in acc.items():
                c, d = self._get(txn, addr)
                txn.put(_key(addr), _pack([c + sign * dc, d + sign * dd]), db=self.db)

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
            acc = {}
            for r in conn.execute("SELECT * FROM transactions"):
                _accumulate([r], acc)
        finally:
            conn.close()
        with self.env.begin(write=True) as txn:
            txn.drop(self.db, delete=False)            # fresh rebuild
            for addr, (c, d) in acc.items():
                txn.put(_key(addr), _pack([c, d]), db=self.db)
        return len(acc)

    # --- read ---------------------------------------------------------------
    def get_balance_units(self, address):
        with self.env.begin() as txn:
            c, d = self._get(txn, address)
        return c - d

    def get_balance(self, address):
        """Balance as a Decimal (atomic units -> decimal), matching the display edge."""
        return amounts.to_decimal(self.get_balance_units(address))

    def count(self):
        with self.env.begin() as txn:
            return txn.stat(db=self.db)["entries"]

    def close(self):
        self.env.close()
