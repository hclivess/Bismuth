"""
vm_state.py — the contract state store for the decentralized-apps v2 VM (doc/17).

A KV store holding, per deployed contract:
  * its **code** (immutable bytecode), and
  * its **storage** (a 256-bit-word key/value map), and
  * its **custody balance** (BIS units the contract holds).

Kept deliberately separate from the consensus ledger and rebuildable from it: the canonical record is the
chain's ``vm:`` transactions; this store is a materialised view that the digester maintains AFTER the
fork activates, and rebuilds by re-executing those transactions on a reorg. Three named sub-DBs (code,
storage, balances). Storage values of 0 are stored as deletions so an unset slot and a 0 slot are identical
(and the state stays compact).

Migrated onto the engine-agnostic KV abstraction (``kvstore.open_store``, doc/26 storage stage 1): the
underlying KV engine (LMDB / MDBX / sqlite-kv) is now a single factory arg instead of a direct
``lmdb.open()`` call. The public method surface AND the on-disk byte format are UNCHANGED — keys are still
raw bytes (``addr`` / ``addr:`` + big-endian 256-bit word) and values raw bytes (the bytecode / a
big-endian 256-bit word / a big-endian custody balance). NO Codec is used: every key and value was already
a raw byte string, so the on-disk format is byte-identical to the pre-migration store and the state_root /
determinism parity proof in the tests holds byte-for-byte on the lmdb backend; the SAME store now also runs
on sqlite-kv, proving the seam.

Deps: ``kvstore`` (which needs ``lmdb`` for the default backend).
"""
from kvstore import open_store

_GIB = 1024 ** 3
_KEY = 32  # storage keys/values are 256-bit words, big-endian


class VMState:
    def __init__(self, path, map_size=4 * _GIB, readonly=False, sync=True, backend="lmdb"):
        self.store = open_store(backend, path, dbs=["code", "storage", "balances"],
                                map_size=map_size, readonly=readonly, sync=sync)
        self.code_db = self.store.open_db("code")
        self.stor_db = self.store.open_db("storage")
        self.bal_db = self.store.open_db("balances")   # per-contract BIS custody (units), in the state root
        # kept for back-compat with callers/tests that introspect the env directly (lmdb backend only)
        self.env = getattr(self.store, "env", None)

    # --- code -------------------------------------------------------------
    def deploy(self, addr, code):
        with self.store.txn(write=True) as txn:
            txn.put(self.code_db, addr.encode(), bytes(code))

    def get_code(self, addr):
        with self.store.txn() as txn:
            return txn.get(self.code_db, addr.encode())

    def list_contracts(self):
        with self.store.txn() as txn:
            return [k.decode() for k, _ in txn.iterate(self.code_db)]

    # --- storage ----------------------------------------------------------
    def load_storage(self, addr):
        """Return the contract's full storage as {int: int} for the VM to operate on."""
        prefix = (addr + ":").encode()
        out = {}
        with self.store.txn() as txn:
            for k, v in txn.iterate(self.stor_db, prefix=prefix):
                out[int.from_bytes(k[len(prefix):], "big")] = int.from_bytes(v, "big")
        return out

    def commit_storage(self, addr, storage):
        prefix = (addr + ":").encode()
        with self.store.txn(write=True) as txn:
            for key, val in storage.items():
                k = prefix + int(key).to_bytes(_KEY, "big")
                if val:
                    txn.put(self.stor_db, k, int(val).to_bytes(_KEY, "big"))
                else:
                    txn.delete(self.stor_db, k)   # 0 == unset, keep compact

    def storage_items(self, addr):
        """(key, value) pairs of a contract's storage, for display."""
        return sorted(self.load_storage(addr).items())

    # --- custody balance (BIS units the contract holds; the ledger 'vm_custody' sink mirrors the total) ---
    def get_balance(self, addr):
        with self.store.txn() as txn:
            v = txn.get(self.bal_db, addr.encode())
        return int.from_bytes(v, "big") if v else 0

    def add_balance(self, addr, delta):
        """Adjust a contract's custody balance by `delta` (units); never below 0. Returns the new balance."""
        bal = self.get_balance(addr) + int(delta)
        if bal < 0:
            bal = 0
        with self.store.txn(write=True) as txn:
            if bal:
                txn.put(self.bal_db, addr.encode(), bal.to_bytes(32, "big"))
            else:
                txn.delete(self.bal_db, addr.encode())
        return bal

    def set_balance(self, addr, value):
        """Set a contract's custody balance to an ABSOLUTE `value` (units; clamped >= 0). The journaled
        Host (vm_engine) computes each touched contract's final balance across a whole vm:call subtree —
        deposits, internal CALL value-moves, TRANSFER debits, SELFDESTRUCT drains — and persists it once on
        success, so flush sets the absolute value rather than replaying deltas."""
        value = int(value)
        if value < 0:
            value = 0
        with self.store.txn(write=True) as txn:
            if value:
                txn.put(self.bal_db, addr.encode(), value.to_bytes(32, "big"))
            else:
                txn.delete(self.bal_db, addr.encode())

    # --- code/storage deletion (SELFDESTRUCT) -----------------------------
    # set_code is just deploy() (it overwrites the code key) — SETCODE flushes through deploy(). delete_code +
    # clear_storage are the SELFDESTRUCT primitives: they remove a contract so its state_root contribution
    # disappears (and a later call to it finds no code). Deterministic + rebuildable: replaying the same vm:
    # txs re-runs the same SELFDESTRUCT and reaches the identical (absent) state.
    def delete_code(self, addr):
        with self.store.txn(write=True) as txn:
            txn.delete(self.code_db, addr.encode())

    def clear_storage(self, addr):
        """Delete every storage slot of `addr` (collect-then-delete, so the scan isn't mutated mid-cursor)."""
        prefix = (addr + ":").encode()
        with self.store.txn(write=True) as txn:
            keys = [k for k, _ in txn.iterate(self.stor_db, prefix=prefix)]
            for k in keys:
                txn.delete(self.stor_db, k)

    def state_root(self):
        """Deterministic 32-byte root (hex) over the ENTIRE contract state — every contract's code and
        every storage slot, in sorted key order. Two nodes with identical state produce identical
        roots; any divergence (a non-determinism bug) is a mismatch the digester can REJECT. Empty state
        has a fixed root. (Not yet a Merkle trie — no inclusion proofs / incremental update; that's the
        optimisation. This is the consensus COMMITMENT.)"""
        import hashlib
        h = hashlib.blake2b(digest_size=32)
        with self.store.txn() as txn:
            for addr, code in txn.iterate(self.code_db):
                h.update(b"C")
                h.update(addr)
                h.update(len(code).to_bytes(4, "big"))
                h.update(code)
            for k, v in txn.iterate(self.stor_db):
                h.update(b"S")
                h.update(k)
                h.update(v)
            for addr, bal in txn.iterate(self.bal_db):
                h.update(b"B")
                h.update(addr)
                h.update(bal)
        return h.hexdigest()

    # --- maintenance ------------------------------------------------------
    def clear(self):
        with self.store.txn(write=True) as txn:
            txn.drop(self.code_db)
            txn.drop(self.stor_db)
            txn.drop(self.bal_db)

    def close(self):
        try:
            self.store.close()
        except Exception:
            pass
