"""
vm_state.py — the contract state store for the decentralized-apps v2 VM (doc/17).

An LMDB store holding, per deployed contract:
  * its **code** (immutable bytecode), and
  * its **storage** (a 256-bit-word key/value map).

Kept deliberately separate from the consensus ledger and rebuildable from it: the canonical record is the
chain's ``vm:`` transactions; this store is a materialised view that the digester maintains AFTER the
fork activates, and rebuilds by re-executing those transactions on a reorg. Two named sub-DBs (code,
storage). Storage values of 0 are stored as deletions so an unset slot and a 0 slot are identical (and
the state stays compact).
"""
import lmdb

_GIB = 1024 ** 3
_KEY = 32  # storage keys/values are 256-bit words, big-endian


class VMState:
    def __init__(self, path, map_size=4 * _GIB, readonly=False):
        # lock=True even when readonly so a reader (a test / the explorer) is consistent while the node writes
        self.env = lmdb.open(path, subdir=True, max_dbs=2, map_size=map_size,
                             readonly=readonly, lock=True)
        self.code_db = self.env.open_db(b"code")
        self.stor_db = self.env.open_db(b"storage")

    # --- code -------------------------------------------------------------
    def deploy(self, addr, code):
        with self.env.begin(write=True) as txn:
            txn.put(addr.encode(), bytes(code), db=self.code_db)

    def get_code(self, addr):
        with self.env.begin() as txn:
            return txn.get(addr.encode(), db=self.code_db)

    def list_contracts(self):
        with self.env.begin() as txn:
            cur = txn.cursor(db=self.code_db)
            return [k.decode() for k, _ in cur]

    # --- storage ----------------------------------------------------------
    def load_storage(self, addr):
        """Return the contract's full storage as {int: int} for the VM to operate on."""
        prefix = (addr + ":").encode()
        out = {}
        with self.env.begin() as txn:
            cur = txn.cursor(db=self.stor_db)
            if cur.set_range(prefix):
                for k, v in cur:
                    if not k.startswith(prefix):
                        break
                    out[int.from_bytes(k[len(prefix):], "big")] = int.from_bytes(v, "big")
        return out

    def commit_storage(self, addr, storage):
        prefix = (addr + ":").encode()
        with self.env.begin(write=True) as txn:
            for key, val in storage.items():
                k = prefix + int(key).to_bytes(_KEY, "big")
                if val:
                    txn.put(k, int(val).to_bytes(_KEY, "big"), db=self.stor_db)
                else:
                    txn.delete(k, db=self.stor_db)   # 0 == unset, keep compact

    def storage_items(self, addr):
        """(key, value) pairs of a contract's storage, for display."""
        return sorted(self.load_storage(addr).items())

    def state_root(self):
        """Deterministic 32-byte root (hex) over the ENTIRE contract state — every contract's code and
        every storage slot, in LMDB's sorted key order. Two nodes with identical state produce identical
        roots; any divergence (a non-determinism bug) is a mismatch the digester can REJECT. Empty state
        has a fixed root. (Not yet a Merkle trie — no inclusion proofs / incremental update; that's the
        optimisation. This is the consensus COMMITMENT.)"""
        import hashlib
        h = hashlib.blake2b(digest_size=32)
        with self.env.begin() as txn:
            for addr, code in txn.cursor(db=self.code_db):
                h.update(b"C")
                h.update(addr)
                h.update(len(code).to_bytes(4, "big"))
                h.update(code)
            for k, v in txn.cursor(db=self.stor_db):
                h.update(b"S")
                h.update(k)
                h.update(v)
        return h.hexdigest()

    # --- maintenance ------------------------------------------------------
    def clear(self):
        with self.env.begin(write=True) as txn:
            txn.drop(self.code_db, delete=False)
            txn.drop(self.stor_db, delete=False)

    def close(self):
        try:
            self.env.close()
        except Exception:
            pass
