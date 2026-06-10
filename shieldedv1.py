"""Shielded value — stages 1 + 2 (stealth addresses + ring signatures). See doc/22-shielded.md.

The in-core successor to the off-chain ``shielded-tokens`` prototype.

Stage 1 gives RECIPIENT privacy (CryptoNote one-time stealth keys: no shared per-token key).
Stage 2 gives SENDER privacy: a spend names a RING of same-amount notes and proves, with a CryptoNote
linkable ring signature, that it owns one of them without revealing which. The spent-set is therefore a
set of KEY IMAGES (unlinkable to notes) rather than per-note nullifiers — so consensus prevents double
spends without learning which note was spent. Amounts stay TRANSPARENT (RingCT is stage 3, deferred);
because the ring can only hide a spender among EQUAL amounts, consensus requires every ring member to
share one amount (CryptoNote-style denominations).

Built only on ``coincurve`` (libsecp256k1) + ``Cryptodome`` — both already hard deps; no hand-rolled
field math, no new crypto lib. secp256k1's cofactor 1 makes ring signatures cleaner than ed25519 (every
valid point is in the prime-order group — no subgroup checks).

This module is two things:
  1. a pure crypto/codec library (stealth derivation, detection, key image, ring sign/verify) usable by
     wallets AND by consensus;
  2. the per-ledger sidecar (``ShieldedState``: decoy/scan note set + key-image spent-set + pool flows)
     plus the ``validate_block`` / ``apply_block`` consensus hooks the digester calls.
"""
import json
import os
import struct
from base64 import b64decode, b64encode
from hashlib import sha224, sha256

import coincurve as cc

import amounts

__version__ = "0.2.0"

# secp256k1 group order N and field prime P. Scalars live in [1, N-1] (libsecp256k1 rejects 0 / >= N).
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_P_FIELD = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F

# Canonical pool sink: a valid 56-hex address with no known private key (mirrors vm_engine.VM_SINK), so it
# can only be debited by the consensus-generated redeem payout. Its ledger balance == the pool's value.
SHIELD_SINK = sha224(b"bismuth-shield-pool").hexdigest()

OP_MINT = "shield:mint"
OP_SPEND = "shield:spend"
OP_REDEEM = "shield:redeem"
SHIELD_OPS = (OP_MINT, OP_SPEND, OP_REDEEM)

ADDR_PREFIX = "shield1"
MAX_RING = 16            # bound ring size: caps validation cost + openfield size (n=1 == no anonymity)


# --- domain-separated hashing ---------------------------------------------------------------------
def _h(tag: bytes, *parts: bytes) -> bytes:
    d = sha256(tag)
    for p in parts:
        d.update(p)
    return d.digest()


def _scalar(tag: bytes, *parts: bytes) -> bytes:
    """A 32-byte secp256k1 scalar in [1, N-1] derived from a domain-separated hash."""
    v = int.from_bytes(_h(tag, *parts), "big") % _N
    if v == 0:
        v = 1
    return v.to_bytes(32, "big")


def _sb(x: int) -> bytes:
    """A scalar int -> 32 bytes in [1, N-1] (callers pass already-reduced values)."""
    return (x % _N).to_bytes(32, "big")


# --- secp256k1 point helpers (all via libsecp256k1) -----------------------------------------------
def _g_mul(s: int) -> cc.PublicKey:           # s*G
    return cc.PublicKey.from_valid_secret(_sb(s))


def _point_mul(P: cc.PublicKey, s: int) -> cc.PublicKey:   # s*P
    return P.multiply(_sb(s))


def _point_add(*pts: cc.PublicKey) -> cc.PublicKey:        # P1 + P2 + ...
    return cc.PublicKey.combine_keys(list(pts))


def hash_to_point(data: bytes) -> cc.PublicKey:
    """Map bytes to a curve point with unknown discrete log (try-and-increment). The iteration count
    depends only on PUBLIC input (a ring member's pubkey), so variable timing leaks nothing secret."""
    i = 0
    while True:
        x = int.from_bytes(_h(b"bis-shield-htp/v1", data, i.to_bytes(4, "big")), "big") % _P_FIELD
        xb = x.to_bytes(32, "big")
        for prefix in (b"\x02", b"\x03"):
            try:
                return cc.PublicKey(prefix + xb)
            except Exception:
                pass
        i += 1


# --- keys / addresses -----------------------------------------------------------------------------
def new_keypair() -> dict:
    """A fresh shielded identity: scan key (a, A) for detection, spend key (b, B) for spending.
    Returns hex privates + the published address. (a) alone is a delegable view key."""
    a = cc.PrivateKey()
    b = cc.PrivateKey()
    A = a.public_key.format()  # compressed 33 bytes
    B = b.public_key.format()
    return {
        "a": a.to_hex(), "b": b.to_hex(),
        "A": A.hex(), "B": B.hex(),
        "address": ADDR_PREFIX + A.hex() + B.hex(),
    }


def parse_address(address: str):
    """shield1<A:66 hex><B:66 hex> -> (A_bytes, B_bytes). Raises ValueError on a malformed address."""
    if not address.startswith(ADDR_PREFIX):
        raise ValueError("not a shielded address")
    body = address[len(ADDR_PREFIX):]
    if len(body) != 132:
        raise ValueError("bad shielded address length")
    A = bytes.fromhex(body[:66])
    B = bytes.fromhex(body[66:])
    cc.PublicKey(A); cc.PublicKey(B)               # validate they are real curve points
    return A, B


# --- output (note) derivation: sender side (knows A, B; not a, b) ----------------------------------
def _shared_secret_sender(r_priv: cc.PrivateKey, A_bytes: bytes) -> bytes:
    return _h(b"bis-shield-ss/v1", r_priv.ecdh(A_bytes))


def _shared_secret_recipient(a_priv: cc.PrivateKey, R_bytes: bytes) -> bytes:
    return _h(b"bis-shield-ss/v1", a_priv.ecdh(R_bytes))


def _one_time_pub(B_bytes: bytes, ss: bytes) -> bytes:
    ot = _scalar(b"bis-shield-ot/v1", ss)
    return cc.PublicKey(B_bytes).add(ot).format()  # P = B + ot*G


def note_id(P_bytes: bytes) -> str:
    return _h(b"bis-shield-note/v1", P_bytes).hex()


def commitment(P_bytes: bytes, amount_units: int, token: str) -> str:
    return _h(b"bis-shield-commit/v1", P_bytes, str(int(amount_units)).encode(), token.encode()).hex()


def _encrypt_memo(ss: bytes, plaintext: dict) -> str:
    from Cryptodome.Cipher import AES
    key = _h(b"bis-shield-memo/v1", ss)
    cipher = AES.new(key, AES.MODE_GCM)
    ct, tag = cipher.encrypt_and_digest(json.dumps(plaintext, sort_keys=True).encode())
    return b64encode(cipher.nonce + tag + ct).decode()


def _decrypt_memo(ss: bytes, memo_b64: str) -> dict:
    from Cryptodome.Cipher import AES
    key = _h(b"bis-shield-memo/v1", ss)
    blob = b64decode(memo_b64)
    nonce, tag, ct = blob[:16], blob[16:32], blob[32:]
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    return json.loads(cipher.decrypt_and_verify(ct, tag).decode())


def make_output(address: str, amount, token: str = "bis", memo_extra: dict = None) -> dict:
    """Build one shielded output paying ``address`` ``amount`` BIS. ``amount`` is a BIS decimal (str/num);
    stored on chain as integer atomic units. Sender-side: needs only the recipient's public address."""
    A_bytes, B_bytes = parse_address(address)
    r = cc.PrivateKey()
    R = r.public_key.format()
    ss = _shared_secret_sender(r, A_bytes)
    P = _one_time_pub(B_bytes, ss)
    units = amounts.to_units(amount)
    memo = dict(memo_extra or {})
    memo.update({"amt": units, "tok": token})
    return {
        "v": 1, "R": R.hex(), "P": P.hex(), "amt": units, "tok": token,
        "memo": _encrypt_memo(ss, memo), "c": commitment(P, units, token),
        "note_id": note_id(P),
    }


def scan_output(note: dict, a_hex: str, b_hex: str):
    """Is this on-chain note ours? If so return (one_time_private_key_hex, decrypted_memo); else None.
    The returned key is what builds this note's key image + ring signature when spending it."""
    try:
        a = cc.PrivateKey(bytes.fromhex(a_hex))
        R_bytes = bytes.fromhex(note["R"])
        ss = _shared_secret_recipient(a, R_bytes)
        B_bytes = cc.PrivateKey(bytes.fromhex(b_hex)).public_key.format()
        if _one_time_pub(B_bytes, ss).hex() != note["P"]:
            return None
        ot = _scalar(b"bis-shield-ot/v1", ss)
        p = cc.PrivateKey(bytes.fromhex(b_hex)).add(ot)  # one-time private key: p = b + ot
        memo = None
        try:
            memo = _decrypt_memo(ss, note["memo"])
        except Exception:
            pass
        return p.to_hex(), memo
    except Exception:
        return None


# --- ring signature (CryptoNote linkable, sum-of-challenges) ---------------------------------------
def key_image(x: int, P: cc.PublicKey) -> cc.PublicKey:
    """I = x * H_p(P): deterministic in the spent output, unlinkable to P without x."""
    return _point_mul(hash_to_point(P.format()), x)


def _ring_challenge(msg: bytes, points: list) -> int:
    d = sha256(b"bis-shield-ringc/v1")
    d.update(msg)
    for p in points:
        d.update(p.format())
    return int.from_bytes(d.digest(), "big") % _N


def ring_sign(msg: bytes, ring: list, x: int, s: int):
    """Sign ``msg`` proving knowledge of the secret ``x`` for ``ring[s]`` (P_s = x*G), hiding s.
    Returns (key_image, c[], r[]) — the CryptoNote ring signature. Raises only on a degenerate RNG draw."""
    n = len(ring)
    I = key_image(x, ring[s])
    c = [0] * n
    r = [0] * n
    while True:
        k = int.from_bytes(os.urandom(32), "big") % _N
        if k != 0:
            break
    Ls = [None] * n
    Rs = [None] * n
    Ls[s] = _g_mul(k)
    Rs[s] = _point_mul(hash_to_point(ring[s].format()), k)
    sum_c = 0
    for i in range(n):
        if i == s:
            continue
        ci = int.from_bytes(os.urandom(32), "big") % _N or 1
        ri = int.from_bytes(os.urandom(32), "big") % _N or 1
        c[i], r[i] = ci, ri
        Ls[i] = _point_add(_g_mul(ri), _point_mul(ring[i], ci))
        Rs[i] = _point_add(_point_mul(hash_to_point(ring[i].format()), ri), _point_mul(I, ci))
        sum_c = (sum_c + ci) % _N
    flat = [p for pair in zip(Ls, Rs) for p in pair]
    h = _ring_challenge(msg, flat)
    c[s] = (h - sum_c) % _N
    r[s] = (k - c[s] * x) % _N
    return I, c, r


def ring_verify(msg: bytes, ring: list, I: cc.PublicKey, c: list, r: list) -> bool:
    """True iff (I, c, r) is a valid ring signature on ``msg`` for ``ring``. Fails safe on any malformed
    input or degenerate point (negligible probability for honest sigs)."""
    try:
        n = len(ring)
        if not (n == len(c) == len(r)):
            return False
        Hp_I = I
        Ls = []
        Rs = []
        for i in range(n):
            Li = _point_add(_g_mul(r[i]), _point_mul(ring[i], c[i]))
            Ri = _point_add(_point_mul(hash_to_point(ring[i].format()), r[i]), _point_mul(Hp_I, c[i]))
            Ls.append(Li)
            Rs.append(Ri)
        flat = [p for pair in zip(Ls, Rs) for p in pair]
        return (sum(c) % _N) == _ring_challenge(msg, flat)
    except Exception:
        return False


# --- spend / redeem builders (ring form, wallet side) ---------------------------------------------
def _ring_message(ring_ids: list, image_hex: str, payload: dict) -> bytes:
    """The bytes the ring signs — binds the ring, the key image, and the outputs/redeem target, so a
    pending spend can't be re-pointed. Canonical (sorted payload) so signer and verifier agree."""
    return (b"bis-shield-ring/v1|" + "|".join(ring_ids).encode() + b"|" + image_hex.encode() + b"|"
            + json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())


def _spend_payload(outputs: list) -> dict:
    return {"out": [{"P": o["P"], "amt": o["amt"], "tok": o["tok"], "c": o["c"]} for o in outputs]}


def make_spend(ring_notes: list, real_index: int, p_hex: str, outputs: list) -> dict:
    """Spend the note at ``ring_notes[real_index]`` (whose one-time key is ``p_hex``) hidden among the
    other (same-amount) ring members, into ``outputs`` (Σ amt must equal the ring amount)."""
    ring_pubs = [cc.PublicKey(bytes.fromhex(nt["P"])) for nt in ring_notes]
    ring_ids = [nt["note_id"] for nt in ring_notes]
    x = int(p_hex, 16)
    image = key_image(x, ring_pubs[real_index])
    image_hex = image.format().hex()
    msg = _ring_message(ring_ids, image_hex, _spend_payload(outputs))
    _, c, r = ring_sign(msg, ring_pubs, x, real_index)
    return {"v": 2, "ring": ring_ids, "I": image_hex,
            "c": [_sb(ci).hex() for ci in c], "r": [_sb(ri).hex() for ri in r], "out": outputs}


def make_redeem(ring_notes: list, real_index: int, p_hex: str, to_address: str, amount_units: int) -> dict:
    ring_pubs = [cc.PublicKey(bytes.fromhex(nt["P"])) for nt in ring_notes]
    ring_ids = [nt["note_id"] for nt in ring_notes]
    x = int(p_hex, 16)
    image = key_image(x, ring_pubs[real_index])
    image_hex = image.format().hex()
    payload = {"to": to_address, "amt": int(amount_units)}
    msg = _ring_message(ring_ids, image_hex, payload)
    _, c, r = ring_sign(msg, ring_pubs, x, real_index)
    return {"v": 2, "ring": ring_ids, "I": image_hex,
            "c": [_sb(ci).hex() for ci in c], "r": [_sb(ri).hex() for ri in r],
            "to": to_address, "amt": int(amount_units)}


# --- the per-ledger sidecar -----------------------------------------------------------------------
def _hbe(height) -> bytes:
    return struct.pack(">Q", int(height))      # ordered height key: lexicographic == numeric


class ShieldedState:
    """Decoy/scan note set + key-image spent-set + pool flow ledger for one ledger — an **LMDB** store (NO
    SQLite, the post-fork storage architecture, doc/26). A deterministic projection of the chain's shield:
    txs; reorg-safe because every record carries a height-ordered secondary key, so rollback is a range
    delete. With ring spends, "which note is spent" is UNKNOWABLE on-chain (the anonymity); the pool's value
    is tracked via flows (mint +A / redeem -A), which equals balance(SINK).

    Sub-DBs: notes (note_id->json), notes_h (height||note_id), kimg (image->height), kimg_h
    (height||image), flows (height||seq->delta), meta (pool / counts / flow seq). Writes are buffered per
    block and flushed ATOMICALLY in one txn at commit() (so a block's shield ops are all-or-nothing); begin()
    drops any partial buffer from a failed prior apply."""

    _GIB = 1024 ** 3

    def __init__(self, path: str, map_size: int = 4 * 1024 ** 3, readonly: bool = False):
        import lmdb
        self.path = path
        self.env = lmdb.open(path, subdir=True, max_dbs=6, map_size=map_size, readonly=readonly, lock=True)
        self.notes_db = self.env.open_db(b"notes")
        self.notes_h = self.env.open_db(b"notes_h")
        self.kimg_db = self.env.open_db(b"kimg")
        self.kimg_h = self.env.open_db(b"kimg_h")
        self.flows_db = self.env.open_db(b"flows")
        self.meta_db = self.env.open_db(b"meta")
        self._pending = []

    # --- meta (pool total + counts + flow sequence) -----------------------
    def _meta(self, txn, key, default=0):
        v = txn.get(key, db=self.meta_db)
        return int(v) if v is not None else default

    # --- reads (committed state; used by validate_block BEFORE apply) -----
    def note(self, note_id_hex: str):
        with self.env.begin() as txn:
            v = txn.get(note_id_hex.encode(), db=self.notes_db)
        if v is None:
            return None
        d = json.loads(v)
        return {"note_id": note_id_hex, "create_height": d["h"], "token": d["tok"], "amount": d["amt"],
                "r_pub": d["R"], "p_pub": d["P"], "memo": d.get("memo", ""), "commitment": d["C"]}

    def has_note(self, note_id_hex: str) -> bool:
        with self.env.begin() as txn:
            return txn.get(note_id_hex.encode(), db=self.notes_db) is not None

    def has_key_image(self, image_hex: str) -> bool:
        with self.env.begin() as txn:
            return txn.get(image_hex.encode(), db=self.kimg_db) is not None

    def pool_units(self) -> int:
        with self.env.begin() as txn:
            return self._meta(txn, b"pool", 0)

    def stats(self) -> dict:
        with self.env.begin() as txn:
            return {"notes": self._meta(txn, b"nnotes", 0), "key_images": self._meta(txn, b"nki", 0),
                    "pool_units": self._meta(txn, b"pool", 0)}

    # --- writes (buffered; flushed atomically at commit) ------------------
    def begin(self):
        """Start a fresh write batch (drop any partial buffer left by a prior apply that did not commit)."""
        self._pending = []

    def add_note(self, height: int, note: dict):
        nid = note_id(bytes.fromhex(note["P"]))
        self._pending.append(("note", nid, int(height),
                              {"h": int(height), "tok": note["tok"], "amt": int(note["amt"]),
                               "R": note["R"], "P": note["P"], "memo": note.get("memo", ""), "C": note["c"]}))

    def add_confidential_note(self, height: int, note: dict):
        """Stage-3 (RingCT) note: the commitment field holds the Pedersen commitment ``C`` and the amount is
        HIDDEN (0 for spend outputs; a public deposit amount only for a mint). Same store/rollback as
        transparent notes, so reorg handling is unchanged."""
        nid = note_id(bytes.fromhex(note["P"]))
        self._pending.append(("note", nid, int(height),
                              {"h": int(height), "tok": note.get("tok", "bis"), "amt": int(note.get("amt", 0)),
                               "R": note["R"], "P": note["P"], "memo": note.get("memo", ""), "C": note["C"]}))

    def add_key_image(self, height: int, image_hex: str):
        self._pending.append(("kimg", image_hex, int(height), None))

    def add_flow(self, height: int, delta: int):
        self._pending.append(("flow", None, int(height), int(delta)))

    def commit(self):
        """Flush the buffered block ops in ONE write transaction (atomic), with INSERT-OR-IGNORE semantics
        (a note_id / key image already present is a no-op — read-your-writes within the txn)."""
        if not self._pending:
            return
        with self.env.begin(write=True) as txn:
            pool = self._meta(txn, b"pool", 0)
            nnotes = self._meta(txn, b"nnotes", 0)
            nki = self._meta(txn, b"nki", 0)
            seq = self._meta(txn, b"flowseq", 0)
            for kind, key, height, payload in self._pending:
                if kind == "note":
                    kb = key.encode()
                    if txn.get(kb, db=self.notes_db) is None:
                        txn.put(kb, json.dumps(payload).encode(), db=self.notes_db)
                        txn.put(_hbe(height) + kb, b"", db=self.notes_h)
                        nnotes += 1
                elif kind == "kimg":
                    kb = key.encode()
                    if txn.get(kb, db=self.kimg_db) is None:
                        txn.put(kb, _hbe(height), db=self.kimg_db)
                        txn.put(_hbe(height) + kb, b"", db=self.kimg_h)
                        nki += 1
                else:  # flow
                    seq += 1
                    txn.put(_hbe(height) + _hbe(seq), str(int(payload)).encode(), db=self.flows_db)
                    pool += int(payload)
            txn.put(b"pool", str(pool).encode(), db=self.meta_db)
            txn.put(b"nnotes", str(nnotes).encode(), db=self.meta_db)
            txn.put(b"nki", str(nki).encode(), db=self.meta_db)
            txn.put(b"flowseq", str(seq).encode(), db=self.meta_db)
        self._pending = []

    def rollback_under(self, height: int):
        """Drop everything recorded at height >= ``height`` (a reorg). Removing the key images of undone
        spends makes those notes spendable again on the new branch; removing the flow rows restores the pool
        value. Range-scans the height-ordered secondary keys, so it is O(rolled-back rows), not a full scan."""
        self._pending = []                          # discard any unflushed batch first
        floor = _hbe(int(height))
        with self.env.begin(write=True) as txn:
            pool = self._meta(txn, b"pool", 0)
            nnotes = self._meta(txn, b"nnotes", 0)
            nki = self._meta(txn, b"nki", 0)
            # notes: height||note_id >= floor
            cur = txn.cursor(db=self.notes_h)
            removed = []
            if cur.set_range(floor):
                for k, _ in cur:
                    removed.append(bytes(k))
            for k in removed:
                txn.delete(k, db=self.notes_h)
                if txn.delete(k[8:], db=self.notes_db):
                    nnotes -= 1
            # key images: height||image >= floor
            cur = txn.cursor(db=self.kimg_h)
            removed = []
            if cur.set_range(floor):
                for k, _ in cur:
                    removed.append(bytes(k))
            for k in removed:
                txn.delete(k, db=self.kimg_h)
                if txn.delete(k[8:], db=self.kimg_db):
                    nki -= 1
            # flows: height||seq >= floor  (subtract their deltas from the pool total)
            cur = txn.cursor(db=self.flows_db)
            fdel = []
            if cur.set_range(floor):
                for k, v in cur:
                    fdel.append((bytes(k), int(v)))
            for k, delta in fdel:
                pool -= delta
                txn.delete(k, db=self.flows_db)
            txn.put(b"pool", str(pool).encode(), db=self.meta_db)
            txn.put(b"nnotes", str(max(0, nnotes)).encode(), db=self.meta_db)
            txn.put(b"nki", str(max(0, nki)).encode(), db=self.meta_db)

    def close(self):
        try:
            self.env.close()
        except Exception:
            pass


def open_state_for(ledger_path: str) -> ShieldedState:
    """Open the LMDB sidecar NAMESPACED BY LEDGER FILENAME — shielded-<ledger> — so a regnet run can never
    hand its note/key-image set to a mainnet node (the doc/18 pollution class). The store is now an LMDB
    directory (doc/26, no SQLite). If a stale pre-LMDB SQLite FILE sits at the path (from before the storage
    rework), remove it first: the shielded sidecar is a post-fork-only, rebuildable projection, so a
    pre-activation file holds nothing canonical — and lmdb.open needs the path to be a directory."""
    base = os.path.basename(ledger_path) or "ledger.db"
    path = os.path.join(os.path.dirname(ledger_path) or ".", "shielded-%s" % base)
    if os.path.exists(path) and not os.path.isdir(path):
        for p in (path, path + "-shm", path + "-wal"):       # the old sqlite file + its WAL sidecars
            try:
                os.remove(p)
            except OSError:
                pass
    return ShieldedState(path)


# --- consensus hooks ------------------------------------------------------------------------------
def _amount_units(stored_amount: str) -> int:
    """A ledger amount column (block_transactions form) -> integer atomic units, honoring storage mode."""
    return int(stored_amount) if amounts.LEDGER_INTEGER else amounts.to_units(stored_amount)


class ShieldError(ValueError):
    """A shield: rule violation — raised so the digester REJECTS the whole block (never committed)."""


def _require_point(hex_or_none, what: str) -> bytes:
    try:
        b = bytes.fromhex(hex_or_none)
        cc.PublicKey(b)  # must be a real curve point
        return b
    except Exception:
        raise ShieldError(f"invalid {what}")


def _resolve_ring(state, data, height):
    """Resolve a v2 spend/redeem's ring -> (ring_pubkeys, amount, token, image_hex). Enforces: members
    exist, are distinct, share one amount+token, ring size in [1, MAX_RING], and the key image is fresh.
    Verifies the ring signature against the message bound to ``payload`` (caller supplies it)."""
    ring_ids = data.get("ring") or []
    n = len(ring_ids)
    if n < 1 or n > MAX_RING:
        raise ShieldError(f"ring size {n} out of [1,{MAX_RING}] at {height}")
    if len(set(ring_ids)) != n:
        raise ShieldError(f"ring has duplicate members at {height}")
    notes = []
    for rid in ring_ids:
        note = state.note(rid)
        if note is None:
            raise ShieldError(f"ring references unknown note {str(rid)[:16]} at {height}")
        notes.append(note)
    if len({nt["amount"] for nt in notes}) != 1 or len({nt["token"] for nt in notes}) != 1:
        raise ShieldError(f"ring members are not the same amount/token at {height}")
    return notes


def _resolve_ring_v3(state, ring_ids, height):
    """Resolve a v3 (RingCT) ring -> sidecar note rows. Unlike v2, there is NO same-amount rule (RingCT
    hides amounts, so a ring may mix any amounts); we only require members exist, are distinct, and the
    ring size is in [1, MAX_RING]. The rows carry p_pub + commitment, which ringct.verify_* consumes."""
    n = len(ring_ids)
    if n < 1 or n > MAX_RING:
        raise ShieldError(f"v3 ring size {n} out of [1,{MAX_RING}] at {height}")
    if len(set(ring_ids)) != n:
        raise ShieldError(f"v3 ring has duplicate members at {height}")
    notes = []
    for rid in ring_ids:
        note = state.note(rid)
        if note is None:
            raise ShieldError(f"v3 ring references unknown note {str(rid)[:16]} at {height}")
        if not note.get("commitment"):
            raise ShieldError(f"v3 ring member {str(rid)[:16]} has no commitment at {height}")
        notes.append(note)
    return notes


def _require_confidential_note(o, what, height):
    """Validate a v3 output/mint note has the fields apply will read, with real curve points, and a
    note_id matching P. Returns the canonical note_id. (Amount validity for OUTPUTS is enforced by the
    range proof inside ringct.verify_spend, not here.)"""
    if not isinstance(o, dict):
        raise ShieldError(f"{what} is not an object at {height}")
    for k in ("R", "P", "C"):
        if k not in o:
            raise ShieldError(f"{what} missing field '{k}' at {height}")
    P = _require_point(o.get("P"), f"{what} P")
    _require_point(o.get("R"), f"{what} R")
    _require_point(o.get("C"), f"{what} C")
    if not isinstance(o.get("tok", "bis"), str) or not (0 < len(o.get("tok", "bis")) <= 64):
        raise ShieldError(f"{what} bad token at {height}")
    if not isinstance(o.get("memo", ""), str) or len(o.get("memo", "")) > 65536:
        raise ShieldError(f"{what} bad memo at {height}")
    # apply_block stores int(o.get("amt", 0)); a confidential output normally has NO amt (it is hidden in
    # C), but an attacker can attach a junk amt ("x", 1.5, {}, …) that PASSES validation here yet THROWS in
    # int() during apply — and apply runs AFTER to_db under a swallowed except, so the block commits while
    # the sidecar desyncs (key image burned, outputs dropped). Reject any non-(non-negative-int) amt now so
    # validate-pass can never apply-fail. (Adversarial-pass / consensus-audit find.)
    if "amt" in o and (not isinstance(o["amt"], int) or isinstance(o["amt"], bool) or o["amt"] < 0):
        raise ShieldError(f"{what} amount must be a non-negative integer at {height}")
    nid = note_id(P)
    if "note_id" in o and o["note_id"] != nid:
        raise ShieldError(f"{what} note_id does not match P at {height}")
    return nid


def _require_note_fields(d, what: str, height) -> bytes:
    """Validate a note/output dict has EVERY field apply_block will read, correctly typed — so a
    validate-pass can never apply-fail (which would desync the sidecar from the committed ledger). Also
    the single home of the amount rules: a strictly-positive INTEGER amount (rejects float/str coercion
    and the negative-output inflation trick) bound to a matching commitment. Returns the P point bytes."""
    if not isinstance(d, dict):
        raise ShieldError(f"{what} is not an object at {height}")
    for k in ("R", "P", "amt", "tok", "c"):
        if k not in d:
            raise ShieldError(f"{what} missing field '{k}' at {height}")
    P = _require_point(d.get("P"), f"{what} P")
    _require_point(d.get("R"), f"{what} R")                 # ephemeral pubkey must be a real curve point
    if not isinstance(d["amt"], int) or isinstance(d["amt"], bool):
        raise ShieldError(f"{what} amount must be an integer at {height}")
    if d["amt"] <= 0:
        raise ShieldError(f"{what} non-positive amount {d['amt']} at {height}")
    if not isinstance(d["tok"], str) or not (0 < len(d["tok"]) <= 64):
        raise ShieldError(f"{what} bad token at {height}")
    if not isinstance(d.get("memo", ""), str) or len(d.get("memo", "")) > 4096:
        raise ShieldError(f"{what} bad memo at {height}")
    if d["c"] != commitment(P, int(d["amt"]), d["tok"]):
        raise ShieldError(f"{what} commitment mismatch at {height}")
    return P


def _verify_ring(notes, data, payload, height):
    raw = data.get("I", "")
    _require_point(raw, "key image")
    # CANONICALIZE the key image to compressed form before it is used anywhere. The spent-set keys on
    # this string, so without canonicalization the SAME image point submitted uncompressed (65B) vs
    # compressed (33B) is two different strings -> slips past has_key_image -> the note double-spends.
    image_hex = cc.PublicKey(bytes.fromhex(raw)).format().hex()
    ring_pubs = [cc.PublicKey(bytes.fromhex(nt["p_pub"])) for nt in notes]
    try:
        c = [int(x, 16) for x in data.get("c", [])]
        r = [int(x, 16) for x in data.get("r", [])]
    except (TypeError, ValueError):
        raise ShieldError(f"malformed ring signature scalars at {height}")
    msg = _ring_message([nt["note_id"] for nt in notes], image_hex, payload)
    if not ring_verify(msg, ring_pubs, cc.PublicKey(bytes.fromhex(image_hex)), c, r):
        raise ShieldError(f"ring signature does not verify at {height}")
    return image_hex


def _mint3_validate(data, tx, height, seen_notes, state):
    """RingCT mint: a confidential note whose amount EQUALS the transparent deposit (the shielding
    boundary, public). The published (amt, blind) must open the commitment C; consensus does not need to
    keep them secret. Returns the fresh note_id."""
    import ringct
    for k in ("R", "P", "C", "amt", "blind"):
        if k not in data:
            raise ShieldError(f"shield:mint(v3) missing field '{k}' at {height}")
    nid = _require_confidential_note(data, "shield:mint(v3)", height)
    if not isinstance(data["amt"], int) or isinstance(data["amt"], bool) or data["amt"] <= 0:
        raise ShieldError(f"shield:mint(v3) amount must be a positive integer at {height}")
    try:
        opened = ringct.commit(int(data["amt"]), int(data["blind"], 16)).format().hex()
    except Exception:
        raise ShieldError(f"shield:mint(v3) malformed commitment opening at {height}")
    if opened != data["C"]:
        raise ShieldError(f"shield:mint(v3) commitment does not open to the amount at {height}")
    if nid in seen_notes or state.has_note(nid):
        raise ShieldError(f"duplicate shielded note {nid[:16]} at {height}")
    if str(tx[3]) != SHIELD_SINK:
        raise ShieldError(f"shield:mint(v3) must pay the pool sink at {height}")
    if _amount_units(tx[4]) != int(data["amt"]):
        raise ShieldError(f"shield:mint(v3) deposit {tx[4]} != note amount {data['amt']} at {height}")
    return nid


def _spend3_validate(data, height, seen_notes, seen_images, state):
    """RingCT spend: confidential outputs (each range-proven), value conserved (balance), input owned and
    its hidden amount tied to the balance — all verified by ringct.verify_spend. Returns (image, outputs)."""
    import ringct
    notes = _resolve_ring_v3(state, data.get("ring") or [], height)
    outs = data.get("out") or []
    if not (1 <= len(outs) <= ringct.MAX_OUTPUTS):
        raise ShieldError(f"shield:spend(v3) bad output count at {height}")
    for o in outs:                                          # outputs must be fresh, well-formed v3 notes
        oid = _require_confidential_note(o, "shield:spend(v3) output", height)
        if oid in seen_notes or state.has_note(oid):
            raise ShieldError(f"shield:spend(v3) output note {oid[:16]} already exists at {height}")
        seen_notes.add(oid)
    try:
        image = ringct.verify_spend(notes, data)            # MLSAG + range proofs + balance
    except ValueError as e:
        raise ShieldError(f"shield:spend(v3) invalid: {e} at {height}")
    if image in seen_images or state.has_key_image(image):
        raise ShieldError(f"double-spend: key image {image[:16]} already used at {height}")
    return image, outs


def _redeem3_validate(data, height, seen_images, state):
    """RingCT redeem: a confidential note -> transparent payout. The revealed (amount, blind) open the
    pseudo-commitment and the MLSAG ties it to the hidden real input, so the payout provably equals the
    spent note's amount. Returns (image, amount_units, to_address)."""
    import ringct
    notes = _resolve_ring_v3(state, data.get("ring") or [], height)
    try:
        image, amt, to = ringct.verify_redeem(notes, data)
    except ValueError as e:
        raise ShieldError(f"shield:redeem(v3) invalid: {e} at {height}")
    if not isinstance(to, str) or not (0 < len(to) <= 56):
        raise ShieldError(f"shield:redeem(v3) bad payout address at {height}")
    if to == SHIELD_SINK:
        raise ShieldError(f"shield:redeem(v3) to the pool sink is not allowed at {height}")
    if image in seen_images or state.has_key_image(image):
        raise ShieldError(f"double-spend: key image {image[:16]} already used at {height}")
    return image, int(amt), to


def validate_block(state: ShieldedState, block_transactions: list, height: int) -> list:
    """Validate every shield: tx in a block against ``state`` (reflecting heights < ``height``).

    Returns parsed, ready-to-apply ops; RAISES ShieldError to reject the block. Read-only w.r.t. ``state``
    so it can run before to_db. block_transactions rows are the digester's 12-field tuples:
    [3]=recipient [4]=amount [10]=operation [11]=openfield.
    """
    parsed = []
    seen_images = set()    # intra-block double-spend guard
    seen_notes = set()     # intra-block duplicate-mint guard
    for tx in block_transactions:
        operation = str(tx[10])
        if operation not in SHIELD_OPS:
            continue
        try:
            data = json.loads(tx[11])
        except Exception:
            raise ShieldError(f"shield: tx with unparseable openfield at {height}")

        ver = data.get("v")

        if operation == OP_MINT:
            if ver == 3:                                            # RingCT: confidential note, public deposit
                nid = _mint3_validate(data, tx, height, seen_notes, state)
                seen_notes.add(nid)
                parsed.append(("mint3", {**data, "height": height}))
                continue
            P = _require_note_fields(data, "shield:mint", height)   # fields + positivity + commitment
            nid = note_id(P)
            if nid in seen_notes or state.has_note(nid):
                raise ShieldError(f"duplicate shielded note {nid[:16]} at {height}")
            if str(tx[3]) != SHIELD_SINK:
                raise ShieldError(f"shield:mint must pay the pool sink, not {str(tx[3])[:16]} at {height}")
            if _amount_units(tx[4]) != int(data["amt"]):
                raise ShieldError(f"shield:mint deposit {tx[4]} != note amount {data['amt']} at {height}")
            seen_notes.add(nid)
            parsed.append(("mint", {**data, "height": height}))

        elif operation == OP_SPEND:
            if ver == 3:                                            # RingCT confidential spend
                image, outs = _spend3_validate(data, height, seen_notes, seen_images, state)
                seen_images.add(image)
                parsed.append(("spend3", {"image": image, "out": outs, "height": height}))
                continue
            notes = _resolve_ring(state, data, height)
            amount, token = notes[0]["amount"], notes[0]["token"]
            outs = data.get("out") or []
            # Validate every output FULLY before the ring check (so the signed payload can't KeyError and
            # so apply can't fail). Outputs must be fresh notes — a colliding note_id would be silently
            # dropped by INSERT OR IGNORE, losing value and leaving the pool over-backed.
            total = 0
            for o in outs:
                oP = _require_note_fields(o, "shield:spend output", height)
                if o["tok"] != token:
                    raise ShieldError(f"shield:spend cannot change token at {height}")
                oid = note_id(oP)
                if oid in seen_notes or state.has_note(oid):
                    raise ShieldError(f"shield:spend output note {oid[:16]} already exists at {height}")
                seen_notes.add(oid)
                total += int(o["amt"])
            if total != int(amount):
                raise ShieldError(f"shield:spend value not conserved ({total}/{amount}) at {height}")
            image = _verify_ring(notes, data, _spend_payload(outs), height)
            if image in seen_images or state.has_key_image(image):
                raise ShieldError(f"double-spend: key image {image[:16]} already used at {height}")
            seen_images.add(image)
            parsed.append(("spend", {"image": image, "out": outs, "height": height}))

        else:  # OP_REDEEM
            if ver == 3:                                            # RingCT confidential redeem -> transparent
                image, amt, to = _redeem3_validate(data, height, seen_images, state)
                seen_images.add(image)
                parsed.append(("redeem3", {"image": image, "to": to, "amt": amt, "height": height}))
                continue
            notes = _resolve_ring(state, data, height)
            amount = notes[0]["amount"]
            to = data.get("to")
            if not isinstance(to, str) or not (0 < len(to) <= 56):
                raise ShieldError(f"shield:redeem bad payout address at {height}")
            if to == SHIELD_SINK:
                raise ShieldError(f"shield:redeem to the pool sink is not allowed at {height}")
            if not isinstance(data.get("amt"), int) or isinstance(data.get("amt"), bool):
                raise ShieldError(f"shield:redeem amount must be an integer at {height}")
            if int(data["amt"]) <= 0:
                raise ShieldError(f"shield:redeem non-positive amount {data.get('amt')} at {height}")
            if int(data["amt"]) != int(amount):
                raise ShieldError(f"shield:redeem amount {data['amt']} != ring amount {amount} at {height}")
            image = _verify_ring(notes, data, {"to": to, "amt": int(data["amt"])}, height)
            if image in seen_images or state.has_key_image(image):
                raise ShieldError(f"double-spend: key image {image[:16]} already used at {height}")
            seen_images.add(image)
            parsed.append(("redeem", {"image": image, "to": data["to"],
                                      "amt": int(data["amt"]), "height": height}))
    return parsed


def apply_block(state: ShieldedState, parsed: list) -> list:
    """Apply validated ops to the sidecar (AFTER to_db). Returns redeem payouts [(to, amount_units)] for
    the digester to settle as consensus ledger rows (SINK -> recipient), exactly like vm payouts. The ops
    are buffered and flushed atomically at commit() (all-or-nothing per block)."""
    state.begin()                                   # fresh batch (drop any partial buffer from a prior apply)
    payouts = []
    for kind, op in parsed:
        height = op["height"]
        if kind == "mint":
            state.add_note(height, op)
            state.add_flow(height, int(op["amt"]))            # mint credits the pool
        elif kind == "spend":
            state.add_key_image(height, op["image"])
            for o in op["out"]:
                state.add_note(height, o)                     # value-neutral: no flow
        elif kind == "redeem":
            state.add_key_image(height, op["image"])
            state.add_flow(height, -int(op["amt"]))           # redeem debits the pool
            payouts.append((op["to"], op["amt"]))
        elif kind == "mint3":                                 # RingCT: confidential note, public deposit
            state.add_confidential_note(height, op)
            state.add_flow(height, int(op["amt"]))
        elif kind == "spend3":                                # RingCT: value-neutral, hidden amounts
            state.add_key_image(height, op["image"])
            for o in op["out"]:
                state.add_confidential_note(height, o)
        elif kind == "redeem3":                               # RingCT: confidential -> transparent payout
            state.add_key_image(height, op["image"])
            state.add_flow(height, -int(op["amt"]))
            payouts.append((op["to"], op["amt"]))
    state.commit()
    return payouts
