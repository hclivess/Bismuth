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

This store has been migrated onto the engine-agnostic KV abstraction (``kvstore.open_store``, doc/26
stage 1): the underlying KV engine (LMDB / MDBX / sqlite-kv) is now a single factory arg instead of a
direct ``lmdb.open()`` call. The public method surface AND the on-disk byte format are UNCHANGED — the
key is still the big-endian uint64 height and the value the same msgpack list-of-[sender, recipient,
amount, mirror_hash] entries — so the balance-equivalence parity proof in the tests holds byte-for-byte
on the lmdb backend, and the SAME store now also runs on sqlite-kv (proving the seam). ``Codec`` from
kvstore centralizes the (de)serialization (same msgpack / JSON-fallback the store used before).

Deps: ``kvstore`` (which needs ``lmdb`` for the lmdb backend; ``msgpack`` optional — JSON fallback).
"""
from kvstore import Codec, KVStore, open_store

_GIB = KVStore.GIB

_pack = Codec.pack
_unpack = Codec.unpack
_hk = Codec.hkey
_uh = Codec.unhkey


class RewardChain:
    def __init__(self, path, map_size=2 * _GIB, readonly=False, sync=True, backend="lmdb"):
        self.store = open_store(backend, path, dbs=["rewards"], map_size=map_size,
                                readonly=readonly, sync=sync)
        self.db = self.store.open_db("rewards")
        # kept for back-compat with callers/tests that introspect the env directly (lmdb backend only)
        self.env = getattr(self.store, "env", None)

    def add(self, height, sender, recipient, amount_units, mirror_hash=""):
        """Append a reward entry for (positive) block ``height``."""
        with self.store.txn(write=True) as txn:
            v = txn.get(self.db, _hk(height))
            entries = _unpack(v) if v is not None else []
            entries.append([sender, recipient, int(amount_units), mirror_hash])
            txn.put(self.db, _hk(height), _pack(entries))

    def entries_for(self, height):
        with self.store.txn() as txn:
            v = txn.get(self.db, _hk(height))
        return _unpack(v) if v is not None else []

    def all_entries(self):
        """Yield (height, sender, recipient, amount_units, mirror_hash) over the whole sidechain."""
        with self.store.txn() as txn:
            for k, v in txn.iterate(self.db):
                h = _uh(k)
                for sender, recipient, amount, mh in _unpack(v):
                    yield h, sender, recipient, int(amount), mh

    def rollback(self, to_height):
        """Drop reward entries for blocks above ``to_height`` (reorg)."""
        with self.store.txn(write=True) as txn:
            keys = [k for k, _ in txn.range(self.db, start=_hk(int(to_height) + 1))]
            for k in keys:
                txn.delete(self.db, k)
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
        return self.store.stat(self.db)["entries"]

    def close(self):
        self.store.close()
