"""
Reward sidechain (roadmap doc/17 phase 5).

The dev-fund and hypernode-payout rewards are minted LOCALLY at block commit (not synced): the node
writes them into the main ledger as **negative-height "mirror" rows** (``dbhandler_write.dev_reward`` /
``hn_reward`` at ``-block_height``). That negative-row hack pollutes the chain table and complicates
every height-based query.

This moves them into a separate store keyed by the **positive** block height they belong to. Each
entry preserves a mirror row's exact balance effect — the synthetic minting source (``"Development
Reward"`` / ``"Hypernode Payouts"``) is debited and the recipient credited — so

    (main ledger, positive heights only)  +  (reward sidechain)   ==   today's full ledger balances

byte-for-byte. It is therefore **balance-preserving and replay-identical**: synced block bodies and
their hashes are unaffected (the mirrors were never part of them), so this is not a consensus change.

Standalone here: store + ``extract_from_ledger`` (lift the existing negative rows out) + the
balance-equivalence proof in the tests. Wiring the digester to write here instead of negative rows
(and the balance path to read here) lands behind a config flag, replay-validated.

Deps: ``lmdb`` (required), ``msgpack`` (optional — JSON fallback).
"""
import struct

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

_GIB = 1024 ** 3


def _hk(height):
    return struct.pack(">Q", int(height))


def _uh(key):
    return struct.unpack(">Q", key)[0]


class RewardChain:
    def __init__(self, path, map_size=2 * _GIB, readonly=False, sync=True):
        self.env = lmdb.open(path, subdir=True, max_dbs=1, map_size=map_size,
                             readonly=readonly, lock=not readonly, sync=sync, metasync=sync)
        self.db = self.env.open_db(b"rewards")

    def add(self, height, sender, recipient, amount_units, mirror_hash=""):
        """Append a reward entry for (positive) block ``height``."""
        with self.env.begin(write=True) as txn:
            v = txn.get(_hk(height), db=self.db)
            entries = _unpack(v) if v is not None else []
            entries.append([sender, recipient, int(amount_units), mirror_hash])
            txn.put(_hk(height), _pack(entries), db=self.db)

    def entries_for(self, height):
        with self.env.begin() as txn:
            v = txn.get(_hk(height), db=self.db)
        return _unpack(v) if v is not None else []

    def all_entries(self):
        """Yield (height, sender, recipient, amount_units, mirror_hash) over the whole sidechain."""
        with self.env.begin() as txn:
            for k, v in txn.cursor(db=self.db):
                h = _uh(k)
                for sender, recipient, amount, mh in _unpack(v):
                    yield h, sender, recipient, int(amount), mh

    def rollback(self, to_height):
        """Drop reward entries for blocks above ``to_height`` (reorg)."""
        with self.env.begin(write=True) as txn:
            cur = txn.cursor(db=self.db)
            keys = []
            if cur.set_range(_hk(int(to_height) + 1)):
                keys = [bytes(k) for k, _ in cur]
            for k in keys:
                txn.delete(k, db=self.db)
        return len(keys)

    def balance_delta_units(self, address):
        """Net effect of the whole sidechain on ``address`` (recipient credited, sender debited)."""
        delta = 0
        for _h, sender, recipient, amount, _mh in self.all_entries():
            if recipient == address:
                delta += amount
            if sender == address:
                delta -= amount
        return delta

    def extract_from_ledger(self, ledger_path):
        """Lift the existing negative-height mirror rows out of a SQLite ledger into the sidechain."""
        import sqlite3
        conn = sqlite3.connect("file:%s?mode=ro" % ledger_path, uri=True, timeout=120)
        conn.text_factory = str
        n = 0
        try:
            # row: [block_height(neg), ts, address(sender), recipient, amount, sig, pubkey, mirror_hash, ...]
            for r in conn.execute("SELECT * FROM transactions WHERE block_height < 0 ORDER BY block_height DESC"):
                self.add(-int(r[0]), r[2], r[3], int(r[4]), r[7])
                n += 1
        finally:
            conn.close()
        return n

    def count_blocks(self):
        with self.env.begin() as txn:
            return txn.stat(db=self.db)["entries"]

    def close(self):
        self.env.close()
