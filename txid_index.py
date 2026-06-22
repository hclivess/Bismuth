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

Deps: ``lmdb`` (required). Mirrors balance_index.py conventions.
"""
import lmdb

import amounts
import bismuth_serialize

_GIB = 1024 ** 3

# 12-column ledger row indices
_H, _TS, _ADDR, _RECIP, _AMOUNT, _SIG, _PUB, _HASH, _FEE, _REWARD, _OP, _OF = range(12)


def txid_of(row, fork_height):
    """The canonical content txid of a stored/converted 12-field ledger row, fork-aware (== the id
    essentials.format_raw_tx surfaces): post-fork the binary-pre-image txid (tx_id_v2_s via tx_id_at),
    over the SIGNED '%.8f' amount string reconstructed storage-mode-agnostically by amounts.ledger_value."""
    amount_str = '%.8f' % amounts.ledger_value(row[_AMOUNT])
    return bismuth_serialize.tx_id_at(int(row[_H]), fork_height, str(row[_TS]), str(row[_ADDR]),
                                      str(row[_RECIP]), amount_str, str(row[_OP]), str(row[_OF]))


class TxidIndex:
    def __init__(self, path, map_size=4 * _GIB, readonly=False, sync=True):
        self.env = lmdb.open(path, subdir=True, max_dbs=1, map_size=map_size,
                             readonly=readonly, lock=True, sync=sync, metasync=sync)
        self.db = self.env.open_db(b"txid")

    @staticmethod
    def _key(txid):
        return txid.encode() if isinstance(txid, str) else txid

    # --- maintenance --------------------------------------------------------
    def apply_rows(self, rows, fork_height):
        """Index one block's POST-FORK txs (height >= fork_height, positive height — the negative-height
        reward mirror rows are not real txs and are skipped). Idempotent on the same row."""
        if fork_height is None:
            return 0
        n = 0
        with self.env.begin(write=True) as txn:
            for r in rows:
                h = int(r[_H])
                if h <= 0 or h < int(fork_height):
                    continue
                txn.put(self._key(txid_of(r, fork_height)), h.to_bytes(8, "big"), db=self.db)
                n += 1
        return n

    def rebuild_from_cursor(self, cursor, fork_height):
        """(Re)build from an open ledger cursor — startup + post-reorg path. Drops and re-indexes every
        post-fork positive-height tx. With fork_height None (pre-fork mainnet) the index is empty."""
        with self.env.begin(write=True) as txn:
            txn.drop(self.db, delete=False)
            if fork_height is None:
                return 0
            n = 0
            for r in cursor.execute("SELECT * FROM transactions WHERE block_height >= ?", (int(fork_height),)):
                txn.put(self._key(txid_of(r, fork_height)), int(r[_H]).to_bytes(8, "big"), db=self.db)
                n += 1
            return n

    def rollback(self, keep_height):
        """Reorg: drop every txid confirmed ABOVE keep_height (the new tip). A full re-index is the safe
        path (matches balance_index's rebuild-on-reorg); callers that have a cursor should prefer
        rebuild_from_cursor. This standalone form scans the value set."""
        keep = int(keep_height)
        with self.env.begin(write=True) as txn:
            cur = txn.cursor(db=self.db)
            stale = [k for k, v in cur if int.from_bytes(v, "big") > keep]
            for k in stale:
                txn.delete(k, db=self.db)
        return len(stale)

    # --- read ---------------------------------------------------------------
    def height_of(self, txid):
        """The confirmed height of `txid`, or None if not on the indexed (post-fork) chain."""
        with self.env.begin() as txn:
            v = txn.get(self._key(txid), db=self.db)
        return int.from_bytes(v, "big") if v is not None else None

    def contains(self, txid, max_height=None):
        """True iff `txid` is in the confirmed chain (optionally at/below max_height — used to bound the
        replay check to [0, confirmed_tip], mirroring _signature_exists_in_ledger)."""
        h = self.height_of(txid)
        if h is None:
            return False
        return True if max_height is None else h <= int(max_height)

    def count(self):
        with self.env.begin() as txn:
            return txn.stat(db=self.db)["entries"]

    def close(self):
        self.env.close()
