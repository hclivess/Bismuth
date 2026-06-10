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
class ShieldedState:
    """Decoy/scan note set + key-image spent-set + pool flow ledger for one ledger. A deterministic
    projection of the chain's shield: txs; reorg-safe because every row is height-stamped, so rollback is
    a pure delete. Note: with ring spends, "which note is spent" is UNKNOWABLE on-chain (that is the
    anonymity); the pool's value is tracked via flows (mint +A / redeem -A), which equals balance(SINK)."""

    def __init__(self, path: str):
        self.path = path
        import sqlite3
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.text_factory = str
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS notes (note_id TEXT PRIMARY KEY, create_height INTEGER, "
            "token TEXT, amount INTEGER, r_pub TEXT, p_pub TEXT, memo TEXT, commitment TEXT)")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS keyimages (image TEXT PRIMARY KEY, spend_height INTEGER)")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS flows (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "height INTEGER, delta INTEGER)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS ki_height ON keyimages (spend_height)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS note_height ON notes (create_height)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS flow_height ON flows (height)")
        self.conn.commit()

    # reads
    def note(self, note_id_hex: str):
        r = self.conn.execute(
            "SELECT note_id, create_height, token, amount, r_pub, p_pub, memo, commitment "
            "FROM notes WHERE note_id = ?", (note_id_hex,)).fetchone()
        if not r:
            return None
        return {"note_id": r[0], "create_height": r[1], "token": r[2], "amount": r[3],
                "r_pub": r[4], "p_pub": r[5], "memo": r[6], "commitment": r[7]}

    def has_note(self, note_id_hex: str) -> bool:
        return self.conn.execute("SELECT 1 FROM notes WHERE note_id = ?", (note_id_hex,)).fetchone() is not None

    def has_key_image(self, image_hex: str) -> bool:
        return self.conn.execute("SELECT 1 FROM keyimages WHERE image = ?", (image_hex,)).fetchone() is not None

    def pool_units(self) -> int:
        row = self.conn.execute("SELECT COALESCE(SUM(delta),0) FROM flows").fetchone()
        return int(row[0] or 0)

    def stats(self) -> dict:
        n = self.conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
        k = self.conn.execute("SELECT COUNT(*) FROM keyimages").fetchone()[0]
        return {"notes": int(n), "key_images": int(k), "pool_units": self.pool_units()}

    # writes (called from the digester AFTER to_db; each op already validated)
    def add_note(self, height: int, note: dict):
        self.conn.execute(
            "INSERT OR IGNORE INTO notes VALUES (?,?,?,?,?,?,?,?)",
            (note_id(bytes.fromhex(note["P"])), int(height), note["tok"], int(note["amt"]),
             note["R"], note["P"], note.get("memo", ""), note["c"]))

    def add_key_image(self, height: int, image_hex: str):
        self.conn.execute("INSERT OR IGNORE INTO keyimages VALUES (?,?)", (image_hex, int(height)))

    def add_flow(self, height: int, delta: int):
        self.conn.execute("INSERT INTO flows (height, delta) VALUES (?,?)", (int(height), int(delta)))

    def commit(self):
        self.conn.commit()

    def rollback_under(self, height: int):
        """Drop everything recorded at height >= ``height`` (mirrors tokens_rollback). Removing the key
        images of undone spends makes those notes spendable again on the new branch — correct reorg
        semantics — and removing the flow rows restores the pool value."""
        h = int(height)
        self.conn.execute("DELETE FROM keyimages WHERE spend_height >= ?", (h,))
        self.conn.execute("DELETE FROM notes WHERE create_height >= ?", (h,))
        self.conn.execute("DELETE FROM flows WHERE height >= ?", (h,))
        self.conn.commit()


def open_state_for(ledger_path: str) -> ShieldedState:
    """Open the sidecar NAMESPACED BY LEDGER FILENAME — shielded-<ledger> — so a regnet run can never
    hand its note/key-image set to a mainnet node (the doc/18 pollution class)."""
    base = os.path.basename(ledger_path) or "ledger.db"
    path = os.path.join(os.path.dirname(ledger_path) or ".", "shielded-%s" % base)
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

        if operation == OP_MINT:
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
    the digester to settle as consensus ledger rows (SINK -> recipient), exactly like vm payouts."""
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
    state.commit()
    return payouts
