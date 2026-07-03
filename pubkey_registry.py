"""hf2 pubkey-by-reference registry (doc/40 — the pubkey domain consensus stage).

A consensus address -> public-key map built ONLY from confirmed prior blocks. Post-fork, an RSA / ML-DSA /
native-multisig sender may OMIT its public key on the wire (the `public_key` field = "") and have it
resolved by address from this registry — dropping the 0.5–2.6 KB key (RSA PEM / ML-DSA key / multisig
redeem script) from the wire and the block-hash pre-image for every repeat send. (Single-sig
secp256k1/ED25519 already carry no pubkey — ecrecover / public_key_from_address — and are untouched.)

WHY IT IS SAFE (the security anchor): the address is a one-way commitment to the key material for every
scheme (RSA addr = sha224(PEM); ML-DSA = hash(pubkey); multisig = hash160(redeem)), and each signer's
`verify_bis_signature` REBUILDS the address from the (resolved) key and rejects on mismatch. So a wrong key
can never be registered (first-use is fully verified before commit) and the registry can never hand back a
key the address does not commit to. The registry is a *cache of a value the address already commits to*; it
adds no new trust. The one new consensus rule is the MISS reject (referencing an unregistered address).

PRIOR-BLOCK-ONLY (no intra-block ordering): `register_from_block` runs at block COMMIT, after the block is
fully validated. A tx at height H resolves against the committed tip only (<= H-1), so a same-block
first-use+reference pair MISSES (rejected) — keeping block-hash computation a pure function of wire bytes
(the empty field is hashed, never the resolved key). First-appearance gate => the map is a pure function of
[fork_height, H] in block order. Reorg-safe: rebuilt deterministically from the FULL ledger (never a pruned
cursor) on every rollback path, and value-scan rollback by height. Modeled on txid_index.py.
"""
from kvstore import open_store

_GIB = 1024 ** 3

# 12-column converted ledger-row indices (== processor.block_transactions shape, as in txid_index)
_H, _TS, _ADDR, _RECIP, _AMOUNT, _SIG, _PUB, _HASH, _FEE, _REWARD, _OP, _OF = range(12)


class PubkeyRegistry:
    def __init__(self, path, map_size=4 * _GIB, readonly=False, sync=True, backend="lmdb"):
        self.store = open_store(backend, path, dbs=["pkref"], map_size=map_size,
                                readonly=readonly, sync=sync)
        self.db = self.store.open_db("pkref")
        self.env = getattr(self.store, "env", None)

    @staticmethod
    def _key(addr):
        return addr.encode() if isinstance(addr, str) else addr

    @staticmethod
    def _val(height, pub):
        # first_seen_height (8B BE) || raw pubkey bytes (the wire field VERBATIM — base64 PEM/key/redeem)
        pub_b = pub.encode() if isinstance(pub, str) else bytes(pub)
        return int(height).to_bytes(8, "big") + pub_b

    # --- maintenance --------------------------------------------------------
    def register_from_block(self, rows, fork_height):
        """Register the FIRST-appearance public key of each post-fork address in this block. Runs at commit
        (after validation), so it only ever stores keys that already passed the address-binding check. Skips
        coinbase / negative-height mirror rows and empty (by-reference) pubkeys. First-appearance gate:
        an address already present is left untouched (idempotent)."""
        if fork_height is None:
            return 0
        n = 0
        with self.store.txn(write=True) as txn:
            for r in rows:
                h = int(r[_H])
                if h <= 0 or h < int(fork_height):
                    continue
                # Skip the coinbase reward row. Post-fork its _PUB slot holds the mining-header state-root
                # commitment ('vmsr'<root>), NOT a public key, and the coinbase carries no signable key. The
                # coinbase is the unique positive-height row with a non-zero reward (digest.py identifies it
                # the same way, via _t[9]). Registering its header would permanently shadow the miner
                # address's REAL key behind the first-appearance gate — breaking pubkey-by-reference for that
                # address — and violate the invariant that the registry only holds keys the address commits to.
                try:
                    if r[_REWARD] and float(r[_REWARD]) != 0:
                        continue
                except (ValueError, TypeError):
                    pass
                pub = r[_PUB]
                if not pub or str(pub).strip() == "":        # by-reference tx — nothing to register
                    continue
                key = self._key(r[_ADDR])
                if txn.get(self.db, key) is not None:        # first-appearance gate
                    continue
                txn.put(self.db, key, self._val(h, pub))
                n += 1
        return n

    def rebuild_from_cursor(self, cursor, fork_height):
        """(Re)build deterministically from an open ledger cursor — startup + EVERY reorg path. Keeps the
        FIRST non-empty pubkey per address (ORDER BY block_height, rowid). MUST be given the FULL ledger
        cursor (db_handler.h), never a pruned one, so first-registration data for ancient addresses
        survives pruning. Column-narrowed (address/public_key/block_height) — not a full-row scan."""
        with self.store.txn(write=True) as txn:
            txn.drop(self.db)
            if fork_height is None:
                return 0
            n = 0
            for r in cursor.execute(
                    "SELECT block_height, address, public_key, reward FROM transactions "
                    "WHERE block_height >= ? ORDER BY block_height ASC, rowid ASC", (int(fork_height),)):
                h, addr, pub, rew = int(r[0]), r[1], r[2], r[3]
                if h <= 0 or not pub or str(pub).strip() == "":
                    continue
                # Skip the coinbase reward row (its _PUB slot is the mining-header state-root commitment, not a
                # key) — must match register_from_block exactly so apply and rebuild register the same set.
                try:
                    if rew and float(rew) != 0:
                        continue
                except (ValueError, TypeError):
                    pass
                key = self._key(addr)
                if txn.get(self.db, key) is not None:        # first-appearance gate
                    continue
                txn.put(self.db, key, self._val(h, pub))
                n += 1
            return n

    def rebuild_from_ledger(self, ledger_path, fork_height):
        """Cold-start / snapshot rebuild: open the FULL ledger read-only and rebuild_from_cursor."""
        import sqlite3
        conn = sqlite3.connect("file:%s?mode=ro" % ledger_path, uri=True, timeout=60)
        conn.text_factory = str
        try:
            return self.rebuild_from_cursor(conn.cursor(), fork_height)
        finally:
            conn.close()

    def rollback(self, keep_height):
        """Reorg: drop every address whose first registration is ABOVE keep_height. Value-scan by the
        8-byte BE first_seen_height (like txid_index.rollback). Callers with a cursor should prefer
        rebuild_from_cursor (the canonical, fully-deterministic reorg path)."""
        keep = int(keep_height)
        with self.store.txn(write=True) as txn:
            stale = [k for k, v in txn.iterate(self.db) if int.from_bytes(bytes(v)[:8], "big") > keep]
            for k in stale:
                txn.delete(self.db, k)
        return len(stale)

    # --- read ---------------------------------------------------------------
    def get(self, addr):
        """The registered public-key wire bytes for `addr` (verbatim), or None if not registered in a prior
        confirmed block. The caller hands these to the per-scheme verifier, whose address-rebuild equality
        check is the binding anchor."""
        with self.store.txn() as txn:
            v = txn.get(self.db, self._key(addr))
        return bytes(v)[8:] if v is not None else None

    def first_seen(self, addr):
        with self.store.txn() as txn:
            v = txn.get(self.db, self._key(addr))
        return int.from_bytes(bytes(v)[:8], "big") if v is not None else None

    def count(self):
        return self.store.stat(self.db)["entries"]

    def close(self):
        self.store.close()
