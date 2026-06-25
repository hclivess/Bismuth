"""
Maintained txid -> height index (doc/26 stage 4, store-migration concern b: the duplicate-signature
replay read off SQLite).

Post-hf2 every tx has a canonical content txid (blake2b of the frozen pre-image, doc/29). This LMDB
projection maps that txid -> the (positive) block height it was confirmed at, so the consensus
replay/dedup check ("has this tx already been seen on the confirmed chain?") becomes an O(1) lookup
instead of the SQLite signature scan (digest._signature_exists_in_ledger). It also makes the txid a
unique key (audit M-3/M-4: under signature malleability two ledger rows can share a txid; keying dedup
on the txid closes that), and can back the content-txid /api/transaction lookup.

POST-FORK ONLY: pre-fork txs keep the legacy signature-based dedup (their canonical id is the signature
slice, not a content hash), so only rows at height >= fork_height are indexed. Like balance_index it is
a rebuildable projection — maintained on apply, rebuilt from the ledger on a reorg — never authoritative
on its own until a flag (txid_index_consensus) flips the dedup read onto it after a clean shadow period.

Migrated onto the engine-agnostic KV abstraction (``kvstore.open_store``, doc/26 storage stage 1): the
txid->height map is stored through KVStore, so swapping the underlying engine (LMDB<->MDBX<->sqlite-kv) is
a one-arg ``backend=`` change. Keys are the raw txid bytes and values the 8-byte big-endian height — raw
bytes, no Codec — so the on-disk format is byte-identical to the pre-migration store.

Deps: ``kvstore`` (which needs ``lmdb`` for the default backend).
"""
from kvstore import open_store

import amounts
import bismuth_serialize

_GIB = 1024 ** 3

# 12-column ledger row indices
_H, _TS, _ADDR, _RECIP, _AMOUNT, _SIG, _PUB, _HASH, _FEE, _REWARD, _OP, _OF = range(12)


def _txid_fields(h, ts, addr, recip, amount_field, op, of, fork_height):
    """The canonical content txid from individual fields — the single source of truth shared by
    ``txid_of`` (12-column rows, the apply path) and the column-narrowed rebuild, so both derive
    byte-identical ids. ``amount_field`` is the raw stored amount; ledger_value reconstructs it
    storage-mode-agnostically and it is signed as '%.8f' into the binary pre-image (tx_id_at)."""
    amount_str = '%.8f' % amounts.ledger_value(amount_field)
    return bismuth_serialize.tx_id_at(int(h), fork_height, str(ts), str(addr),
                                      str(recip), amount_str, str(op), str(of))


def txid_of(row, fork_height):
    """The canonical content txid of a stored/converted 12-field ledger row, fork-aware (== the id
    essentials.format_raw_tx surfaces): post-fork the binary-pre-image txid (tx_id_v2_s via tx_id_at),
    over the SIGNED '%.8f' amount string reconstructed storage-mode-agnostically by amounts.ledger_value."""
    return _txid_fields(row[_H], row[_TS], row[_ADDR], row[_RECIP], row[_AMOUNT], row[_OP], row[_OF],
                        fork_height)


class TxidIndex:
    def __init__(self, path, map_size=4 * _GIB, readonly=False, sync=True, backend="lmdb"):
        self.store = open_store(backend, path, dbs=["txid"], map_size=map_size,
                                readonly=readonly, sync=sync)
        self.db = self.store.open_db("txid")
        # kept for back-compat with callers/tests that introspect the env directly (lmdb backend only)
        self.env = getattr(self.store, "env", None)

    @staticmethod
    def _key(txid):
        # hf2 Stage-4 (doc/40 C3): store the raw 32-byte blake2b content txid, not its 64-hex .encode()
        # (2x). Deterministic + tolerant: a valid hex string -> raw bytes; a non-hex string (test fixture /
        # sentinel) -> utf-8 fallback; raw bytes pass through. Lookup uses the same _key, so it is consistent.
        if isinstance(txid, str):
            try:
                return bytes.fromhex(txid)
            except ValueError:
                return txid.encode()
        return txid

    # --- maintenance --------------------------------------------------------
    def apply_rows(self, rows, fork_height):
        """Index one block's POST-FORK txs (height >= fork_height, positive height — the negative-height
        reward mirror rows are not real txs and are skipped). Idempotent on the same row."""
        if fork_height is None:
            return 0
        n = 0
        with self.store.txn(write=True) as txn:
            for r in rows:
                h = int(r[_H])
                if h <= 0 or h < int(fork_height):
                    continue
                txn.put(self.db, self._key(txid_of(r, fork_height)), h.to_bytes(8, "big"))
                n += 1
        return n

    def rebuild_from_cursor(self, cursor, fork_height):
        """(Re)build from an open ledger cursor — startup + post-reorg path. Drops and re-indexes every
        post-fork positive-height tx. With fork_height None (pre-fork mainnet) the index is empty."""
        with self.store.txn(write=True) as txn:
            txn.drop(self.db)
            if fork_height is None:
                return 0
            n = 0
            # Column-narrowed: pull only the 7 fields the content-txid needs instead of SELECT * — avoids
            # hauling every post-fork row's public_key + signature blobs through Python while re-hashing.
            for r in cursor.execute(
                    "SELECT block_height, timestamp, address, recipient, amount, operation, openfield "
                    "FROM transactions WHERE block_height >= ?", (int(fork_height),)):
                txn.put(self.db,
                        self._key(_txid_fields(r[0], r[1], r[2], r[3], r[4], r[5], r[6], fork_height)),
                        int(r[0]).to_bytes(8, "big"))
                n += 1
            return n

    def rollback(self, keep_height):
        """Reorg: drop every txid confirmed ABOVE keep_height (the new tip). A full re-index is the safe
        path (matches balance_index's rebuild-on-reorg); callers that have a cursor should prefer
        rebuild_from_cursor. This standalone form scans the value set."""
        keep = int(keep_height)
        with self.store.txn(write=True) as txn:
            stale = [k for k, v in txn.iterate(self.db) if int.from_bytes(v, "big") > keep]
            for k in stale:
                txn.delete(self.db, k)
        return len(stale)

    # --- read ---------------------------------------------------------------
    def height_of(self, txid):
        """The confirmed height of `txid`, or None if not on the indexed (post-fork) chain."""
        with self.store.txn() as txn:
            v = txn.get(self.db, self._key(txid))
        return int.from_bytes(v, "big") if v is not None else None

    def contains(self, txid, max_height=None):
        """True iff `txid` is in the confirmed chain (optionally at/below max_height — used to bound the
        replay check to [0, confirmed_tip], mirroring _signature_exists_in_ledger)."""
        h = self.height_of(txid)
        if h is None:
            return False
        return True if max_height is None else h <= int(max_height)

    def count(self):
        return self.store.stat(self.db)["entries"]

    def close(self):
        self.store.close()
