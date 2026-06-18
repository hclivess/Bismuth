"""Token (v2) + alias derived index on **LMDB** — the post-fork successor to the SQLite ``index.db``
(doc/26 storage rearchitecture, stage 2; NO SQLite post-fork).

``tokens`` and ``aliases`` were always the most cleanly *separable* part of the SQLite trio: their own
file (``index.db``), a derived projection of the ledger, never consensus. This module re-homes that
projection in an embedded LMDB env, exactly mirroring stage 1's ``shieldedv1.ShieldedState``: same
public *behaviour* (the balances/holders/alias maps callers see are byte-identical), height-ordered
secondary keys so a reorg rollback is a range delete, and atomic per-op write txns. The SQLite ``tokens``
/ ``aliases`` row tables and their SUM/GROUP-BY queries are replaced by *materialized* indexes:

  meta       : b"tok_anchor"/b"alias_anchor" -> last-indexed height; b"seq" -> monotonic op counter
  tokreg     : token                         -> {h,ts,issuer,txid,supply}   (issuance; first name wins)
  seen       : txid                          -> b""                          (processed-tx dedup)
  cred       : token \0 recipient \0 H Q      -> amount   (received rows; SUM by recipient, height-bounded)
  deb        : token \0 address   \0 H Q      -> amount   (sent rows;     SUM by address,   height-bounded)
  addrtok    : address \0 token              -> refcount (reverse index: tokens an address has touched)
  tokset     : token                         -> count    (rows carrying this token: issue + valid xfers)
  journal    : H Q                           -> {k,tok,rcp,adr,amt,txid}    (token op undo log, by height)
  alias_fwd  : alias                         -> {a:address,h}               (first claimant wins)
  alias_rev  : address \0 H \0 alias         -> b""                          (an address's aliases, ordered)
  ajournal   : H Q                           -> {al:alias,a:address}        (alias op undo log, by height)

(``H`` = big-endian uint64 height, ``Q`` = big-endian uint64 per-op sequence — together they make the
journal/cred/deb keys reconstructable from one another, and let rollback range-scan by height.)

The overspend rule that decides whether a transfer is recorded is preserved EXACTLY, including the legacy
``credit: block_height < h`` / ``debit: block_height <= h`` asymmetry (you cannot re-spend tokens received
in the same block) — so the same transfers are accepted/rejected and the same balances result.

This is a derived, rebuildable index: it never touches the canonical ledger and has no effect on
consensus. It is built incrementally from ``token_anchor()`` / ``alias_anchor()`` by the scanners in
``tokensv2`` / ``aliases`` (a fresh store has anchor 0 → first pass backfills the whole chain).

Deps: ``lmdb`` (required). Values are JSON (small, readable; mirrors shieldedv1).
"""
import json
import os
import struct

__version__ = "0.1.0"

_GIB = 1024 ** 3
_SEP = b"\x00"


def _hbe(height) -> bytes:
    return struct.pack(">Q", int(height))          # ordered height key: lexicographic == numeric


def _hq(height, seq) -> bytes:
    return struct.pack(">QQ", int(height), int(seq))   # 16-byte (height, seq) suffix


def _party_key(token: str, party: str, height, seq) -> bytes:
    """cred/deb key: token \0 party \0 <8B height><8B seq>. The \0 separators stop a token/party that is a
    prefix of another from colliding; the fixed 16-byte tail carries (height, seq) for rollback + bounds."""
    return token.encode() + _SEP + party.encode() + _SEP + _hq(height, seq)


def _party_prefix(token: str, party: str = None) -> bytes:
    if party is None:
        return token.encode() + _SEP
    return token.encode() + _SEP + party.encode() + _SEP


class TokenIndex:
    """LMDB token + alias projection for ONE ledger. Single writer, many readers (LMDB MVCC); each mutating
    method is its own atomic write txn, so a crash mid-pass just re-scans from the anchor (idempotent via
    the registry / txid dedup). Reorg rollback is a height range-delete + counter/refcount adjustment."""

    def __init__(self, path: str, map_size: int = 4 * _GIB, readonly: bool = False):
        import lmdb
        self.path = path
        self.env = lmdb.open(path, subdir=True, max_dbs=12, map_size=map_size, readonly=readonly, lock=True)
        self.meta = self.env.open_db(b"meta")
        self.tokreg = self.env.open_db(b"tokreg")
        self.seen = self.env.open_db(b"seen")
        self.cred = self.env.open_db(b"cred")
        self.deb = self.env.open_db(b"deb")
        self.addrtok = self.env.open_db(b"addrtok")
        self.tokset = self.env.open_db(b"tokset")
        self.journal = self.env.open_db(b"journal")
        self.alias_fwd = self.env.open_db(b"alias_fwd")
        self.alias_rev = self.env.open_db(b"alias_rev")
        self.ajournal = self.env.open_db(b"ajournal")

    # ---- meta helpers ----------------------------------------------------
    def _get_int(self, txn, db, key, default=0):
        v = txn.get(key, db=db)
        return int(v) if v is not None else default

    def _next_seq(self, txn) -> int:
        seq = self._get_int(txn, self.meta, b"seq", 0) + 1
        txn.put(b"seq", str(seq).encode(), db=self.meta)
        return seq

    def _bump_anchor(self, txn, key, height):
        if int(height) > self._get_int(txn, self.meta, key, 0):
            txn.put(key, str(int(height)).encode(), db=self.meta)

    def token_anchor(self) -> int:
        with self.env.begin() as txn:
            return self._get_int(txn, self.meta, b"tok_anchor", 0)

    def alias_anchor(self) -> int:
        with self.env.begin() as txn:
            return self._get_int(txn, self.meta, b"alias_anchor", 0)

    # ---- token registry / dedup reads (used by the scanner) --------------
    def has_token(self, token: str) -> bool:
        with self.env.begin() as txn:
            return txn.get(token.encode(), db=self.tokreg) is not None

    def has_txid(self, txid: str) -> bool:
        with self.env.begin() as txn:
            return txn.get(txid.encode(), db=self.seen) is not None

    # ---- bounded credit/debit (the EXACT overspend-check sums) ------------
    def _sum_party(self, db, token: str, party: str, height, inclusive: bool) -> int:
        """Σ amount over a party's rows for ``token`` with height < h (inclusive=False) or <= h (True).
        Mirrors ``SELECT sum(amount) ... WHERE token=? AND <recipient|address>=? AND block_height <|<= ?``."""
        prefix = _party_prefix(token, party)
        total = 0
        with self.env.begin() as txn:
            cur = txn.cursor(db=db)
            if cur.set_range(prefix):
                for k, v in cur:
                    if not k.startswith(prefix):
                        break
                    h = struct.unpack(">QQ", k[-16:])[0]
                    if (h <= height) if inclusive else (h < height):
                        total += int(v)
        return total

    def token_credit(self, token: str, address: str, below_height) -> int:
        """received-so-far for the overspend check: recipient credits at block_height < below_height."""
        return self._sum_party(self.cred, token, address, int(below_height), inclusive=False)

    def token_debit(self, token: str, address: str, upto_height) -> int:
        """sent-so-far for the overspend check: address debits at block_height <= upto_height."""
        return self._sum_party(self.deb, token, address, int(upto_height), inclusive=True)

    # ---- token writes (atomic per op) ------------------------------------
    def register_issue(self, height, ts, token: str, issuer: str, txid: str, supply) -> bool:
        """Register a new token (issuance). First name wins; idempotent (already-registered or
        already-seen-txid → no-op). The issuance row credits ``issuer`` with the full supply (legacy stores
        recipient=issuer, address='issued'), and counts as one ``tokset`` row (matches GROUP BY token)."""
        try:
            supply = int(supply)
        except (TypeError, ValueError):
            supply = 0
        tb, xb = token.encode(), txid.encode()
        with self.env.begin(write=True) as txn:
            if txn.get(tb, db=self.tokreg) is not None or txn.get(xb, db=self.seen) is not None:
                return False
            seq = self._next_seq(txn)
            txn.put(tb, json.dumps({"h": int(height), "ts": ts, "issuer": issuer,
                                    "txid": txid, "supply": supply}).encode(), db=self.tokreg)
            txn.put(xb, b"", db=self.seen)
            # issuance: recipient=issuer credited the supply, address='issued' debited it (legacy mirror)
            txn.put(_party_key(token, issuer, height, seq), str(supply).encode(), db=self.cred)
            txn.put(_party_key(token, "issued", height, seq), str(supply).encode(), db=self.deb)
            self._incr_addrtok(txn, issuer, token, +1)
            self._incr_addrtok(txn, "issued", token, +1)
            self._incr_count(txn, self.tokset, tb, +1)
            txn.put(_hq(height, seq), json.dumps({"k": "issue", "tok": token, "rcp": issuer,
                    "adr": "issued", "amt": supply, "txid": txid}).encode(), db=self.journal)
            self._bump_anchor(txn, b"tok_anchor", height)
            return True

    def apply_transfer(self, height, ts, token: str, sender: str, recipient: str, txid: str, amount) -> bool:
        """Record a VALID transfer (caller already checked the balance). Debits sender, credits recipient,
        links both addresses to the token, counts one ``tokset`` row. Idempotent on txid."""
        amount = int(amount)
        xb = txid.encode()
        with self.env.begin(write=True) as txn:
            if txn.get(xb, db=self.seen) is not None:
                return False
            seq = self._next_seq(txn)
            txn.put(xb, b"", db=self.seen)
            txn.put(_party_key(token, recipient, height, seq), str(amount).encode(), db=self.cred)
            txn.put(_party_key(token, sender, height, seq), str(amount).encode(), db=self.deb)
            self._incr_addrtok(txn, sender, token, +1)
            self._incr_addrtok(txn, recipient, token, +1)
            self._incr_count(txn, self.tokset, token.encode(), +1)
            txn.put(_hq(height, seq), json.dumps({"k": "transfer", "tok": token, "rcp": recipient,
                    "adr": sender, "amt": amount, "txid": txid}).encode(), db=self.journal)
            self._bump_anchor(txn, b"tok_anchor", height)
            return True

    def mark_noop(self, height, txid: str) -> bool:
        """Record an INVALID transfer as a no-op (legacy inserts an empty placeholder row): mark the txid
        seen so it is never reprocessed, journal it (kind=noop) so a rollback re-opens it. No balance/links."""
        xb = txid.encode()
        with self.env.begin(write=True) as txn:
            if txn.get(xb, db=self.seen) is not None:
                return False
            seq = self._next_seq(txn)
            txn.put(xb, b"", db=self.seen)
            txn.put(_hq(height, seq), json.dumps({"k": "noop", "txid": txid}).encode(), db=self.journal)
            self._bump_anchor(txn, b"tok_anchor", height)
            return True

    def _incr_addrtok(self, txn, address: str, token: str, delta: int):
        key = address.encode() + _SEP + token.encode()
        n = self._get_int(txn, self.addrtok, key, 0) + delta
        if n > 0:
            txn.put(key, str(n).encode(), db=self.addrtok)
        else:
            txn.delete(key, db=self.addrtok)

    def _incr_count(self, txn, db, key: bytes, delta: int):
        n = self._get_int(txn, db, key, 0) + delta
        if n > 0:
            txn.put(key, str(n).encode(), db=db)
        else:
            txn.delete(key, db=db)

    # ---- token reads (the query surface: tokensget / /api/tokens / /api/token) ----
    def token_balance(self, token: str, address: str) -> int:
        """Unbounded credit-minus-debit (the ``tokensget`` balance: Σ received − Σ sent for this token)."""
        cred = self._sum_party(self.cred, token, address, 1 << 63, inclusive=True)
        deb = self._sum_party(self.deb, token, address, 1 << 63, inclusive=True)
        return cred - deb

    def tokens_user(self, address: str):
        """DISTINCT tokens an address has touched (sender OR recipient) — shape matches the SQLite fetchall
        the node iterates: a list of 1-tuples ``[(token,), ...]``."""
        prefix = address.encode() + _SEP
        out = []
        with self.env.begin() as txn:
            cur = txn.cursor(db=self.addrtok)
            if cur.set_range(prefix):
                for k, _ in cur:
                    if not k.startswith(prefix):
                        break
                    out.append((k[len(prefix):].decode(),))
        return out

    def _group_sums(self, db, token: str) -> dict:
        """{party: Σ amount} over all of a token's cred/deb rows (the GROUP BY recipient/address queries)."""
        prefix = _party_prefix(token)
        sums = {}
        with self.env.begin() as txn:
            cur = txn.cursor(db=db)
            if cur.set_range(prefix):
                for k, v in cur:
                    if not k.startswith(prefix):
                        break
                    party = k[len(prefix):-17].decode()      # strip token\0 ... and the \0 + 16-byte tail
                    sums[party] = sums.get(party, 0) + int(v)
        return sums

    def token_transfers(self, token: str) -> int:
        with self.env.begin() as txn:
            return self._get_int(txn, self.tokset, token.encode(), 0)

    def tokens_list(self, limit: int = 500):
        """All tokens ranked by row count desc (``/api/tokens``): [{"token","transfers"}]."""
        rows = []
        with self.env.begin() as txn:
            cur = txn.cursor(db=self.tokset)
            for k, v in cur:
                rows.append((k.decode(), int(v)))
        rows.sort(key=lambda r: -r[1])
        return rows[:limit]

    def token_detail(self, token: str, limit: int = 200, offset: int = 0):
        """``/api/token`` holders/supply/transfers, or None if the token is unknown (no rows). The holders
        array (sorted by balance desc) is windowed by ``limit`` (default 200, clamped 1..1000) and
        ``offset``, and the result carries ``holder_count`` (the full total), ``holders_returned``,
        ``offset``, ``limit`` and ``truncated`` so a caller can tell when the array is partial and page
        through the rest. ``supply``/``holder_count`` are always the full-set aggregates regardless of the
        window. (The holders array used to be silently capped at the top 200 with no flag.)"""
        credits = self._group_sums(self.cred, token)
        debits = self._group_sums(self.deb, token)
        holders = []
        for addr in (set(credits) | set(debits)):
            bal = credits.get(addr, 0) - debits.get(addr, 0)
            if bal > 0:
                holders.append({"address": addr, "balance": bal})
        holders.sort(key=lambda h: -h["balance"])
        transfers = self.token_transfers(token)
        if not holders and not transfers:
            return None
        limit = max(1, min(int(limit), 1000))
        offset = max(0, int(offset))
        total = len(holders)
        window = holders[offset:offset + limit]
        return {"token": token, "supply": sum(h["balance"] for h in holders),
                "holder_count": total, "transfers": transfers, "holders": window,
                "holders_returned": len(window), "offset": offset, "limit": limit,
                "truncated": (offset + len(window)) < total}

    # ---- token rollback (reorg: drop everything at height >= h) -----------
    def tokens_rollback(self, height):
        """Undo every token op at block_height >= ``height``: range-scan the height-ordered journal, reverse
        each op's cred/deb/addrtok/count/registry/seen effects, and reset the anchor to height-1. Mirrors
        the legacy ``DELETE FROM tokens WHERE block_height >= ?`` (so a later reindex re-applies the range)."""
        floor = _hbe(int(height))
        with self.env.begin(write=True) as txn:
            cur = txn.cursor(db=self.journal)
            entries = []
            if cur.set_range(floor):
                for k, v in cur:
                    entries.append((bytes(k), json.loads(v)))
            for jk, op in entries:
                h, seq = struct.unpack(">QQ", jk)
                kind = op["k"]
                if kind in ("issue", "transfer"):
                    token = op["tok"]
                    txn.delete(_party_key(token, op["rcp"], h, seq), db=self.cred)
                    txn.delete(_party_key(token, op["adr"], h, seq), db=self.deb)
                    self._incr_addrtok(txn, op["rcp"], token, -1)
                    self._incr_addrtok(txn, op["adr"], token, -1)
                    self._incr_count(txn, self.tokset, token.encode(), -1)
                    if kind == "issue":
                        txn.delete(token.encode(), db=self.tokreg)
                    txn.delete(op["txid"].encode(), db=self.seen)
                else:  # noop
                    txn.delete(op["txid"].encode(), db=self.seen)
                txn.delete(jk, db=self.journal)
            txn.put(b"tok_anchor", str(max(0, int(height) - 1)).encode(), db=self.meta)

    # ---- alias writes ----------------------------------------------------
    def has_alias(self, alias: str) -> bool:
        with self.env.begin() as txn:
            return txn.get(alias.encode(), db=self.alias_fwd) is not None

    def register_alias(self, height, address: str, alias: str) -> bool:
        """Register ``alias -> address`` (first claimant wins; idempotent if already registered)."""
        ab = alias.encode()
        with self.env.begin(write=True) as txn:
            if txn.get(ab, db=self.alias_fwd) is not None:
                self._bump_anchor(txn, b"alias_anchor", height)
                return False
            seq = self._next_seq(txn)
            txn.put(ab, json.dumps({"a": address, "h": int(height)}).encode(), db=self.alias_fwd)
            txn.put(address.encode() + _SEP + _hbe(height) + _SEP + ab, b"", db=self.alias_rev)
            txn.put(_hq(height, seq), json.dumps({"al": alias, "a": address}).encode(), db=self.ajournal)
            self._bump_anchor(txn, b"alias_anchor", height)
            return True

    # ---- alias reads -----------------------------------------------------
    def addfromalias(self, alias: str) -> str:
        with self.env.begin() as txn:
            v = txn.get(alias.encode(), db=self.alias_fwd)
        return json.loads(v)["a"] if v is not None else "No alias"

    def _aliases_for(self, address: str):
        """Every alias of ``address`` in registration (height) order — the suffix of the alias_rev keys."""
        prefix = address.encode() + _SEP
        out = []
        with self.env.begin() as txn:
            cur = txn.cursor(db=self.alias_rev)
            if cur.set_range(prefix):
                for k, _ in cur:
                    if not k.startswith(prefix):
                        break
                    out.append(k[len(prefix) + 8 + 1:].decode())   # strip address\0 + 8B height + \0
        return out

    def aliasget(self, address: str):
        """All aliases of an address as the SQLite shape ``[[alias], ...]``; ``[[address]]`` if none."""
        rows = [[a] for a in self._aliases_for(address)]
        return rows if rows else [[address]]

    def aliasesget(self, addresses):
        """First alias (earliest height) per address, else the address itself — matches the legacy LIMIT 1."""
        results = []
        for address in addresses:
            aliases = self._aliases_for(address)
            results.append(aliases[0] if aliases else address)
        return results

    # ---- alias rollback --------------------------------------------------
    def aliases_rollback(self, height):
        """Drop alias registrations at block_height >= ``height`` from both maps and reset the anchor."""
        floor = _hbe(int(height))
        with self.env.begin(write=True) as txn:
            cur = txn.cursor(db=self.ajournal)
            entries = []
            if cur.set_range(floor):
                for k, v in cur:
                    entries.append((bytes(k), json.loads(v)))
            for jk, op in entries:
                h = struct.unpack(">QQ", jk)[0]
                alias, address = op["al"], op["a"]
                # only remove the forward map if THIS registration is the one that won it (height matches)
                fwd = txn.get(alias.encode(), db=self.alias_fwd)
                if fwd is not None and json.loads(fwd).get("h") == h:
                    txn.delete(alias.encode(), db=self.alias_fwd)
                txn.delete(address.encode() + _SEP + _hbe(h) + _SEP + alias.encode(), db=self.alias_rev)
                txn.delete(jk, db=self.ajournal)
            txn.put(b"alias_anchor", str(max(0, int(height) - 1)).encode(), db=self.meta)

    # ---- stats / lifecycle -----------------------------------------------
    def stats(self) -> dict:
        with self.env.begin() as txn:
            return {"tokens": txn.stat(self.tokreg)["entries"],
                    "aliases": txn.stat(self.alias_fwd)["entries"],
                    "tok_anchor": self._get_int(txn, self.meta, b"tok_anchor", 0),
                    "alias_anchor": self._get_int(txn, self.meta, b"alias_anchor", 0)}

    def close(self):
        try:
            self.env.close()
        except Exception:
            pass


def open_for(ledger_path: str) -> "TokenIndex":
    """Open the LMDB token/alias index NAMESPACED BY LEDGER FILENAME — tokenindex-<ledger> — so a regnet
    run can never hand its token/alias projection to a mainnet node (the doc/18 pollution class), exactly
    like the shielded sidecar. A stale pre-LMDB ``index.db`` is NOT touched here: it is a shared legacy file
    (tokens/aliases/staking) retired only when the whole SQLite trio is dropped; this store is the separate
    post-fork home for the tokens/aliases projection, rebuilt from the ledger by the scanners."""
    base = os.path.basename(ledger_path) or "ledger.db"
    path = os.path.join(os.path.dirname(ledger_path) or ".", "tokenindex-%s" % base)
    return TokenIndex(path)
