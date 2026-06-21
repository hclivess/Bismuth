"""
hd_wallet.py — BIP32-style hierarchical-deterministic wallet for Bismuth.

One seed -> unlimited deterministic keypairs -> a fresh ``Bis1...`` recipient address for every payment
(Bitcoin's HD-wallet idea). Built on secp256k1 (``coincurve``), which the in-tree ECDSA signer
(``polysign/signer_ecdsa.py``) already uses, so every derived address is a REAL Bismuth address that the
node accepts and that can sign transactions — no consensus change. Addresses are public-key hashes, so
how the key was derived is invisible to (and irrelevant to) the chain: this is purely wallet-side and
works identically pre- and post-fork.

WHY ECDSA (not the default RSA): BIP32 child derivation is a scalar tweak on the private key
(child = parse256(IL) + k_par mod n), which is natural on secp256k1 and impossible to do cheaply for RSA.
secp256k1's cofactor 1 and standard tweak-add make this textbook BIP32.

DERIVATION (BIP32, https://github.com/bitcoin/bips/blob/master/bip-0032.mediawiki)
  master:  I = HMAC-SHA512("Bismuth seed", seed);  k = I[:32] (must be 1..n-1),  chain = I[32:]
  CKDpriv(k_par, c_par, i):
     hardened (i >= 2^31):  I = HMAC-SHA512(c_par, 0x00 || ser256(k_par) || ser32(i))
     normal   (i <  2^31):  I = HMAC-SHA512(c_par, serP(point(k_par)) || ser32(i))
     k_i = (parse256(I[:32]) + k_par) mod n   (invalid if I[:32] >= n or k_i == 0 -> use the next index)
  Default account path:  m / 44' / coin' / account' / change / index   (BIP44 layout)

The domain tag is "Bismuth seed" (not Bitcoin's "Bitcoin seed"): these are Bismuth keys, a deliberately
distinct keyspace so a Bismuth seed never collides with a Bitcoin wallet's. ``coin_type`` defaults to 0
and is NOT an official SLIP-44 assignment — it only namespaces accounts within a wallet.
"""
import hashlib
import hmac
from base64 import b64encode

from coincurve import PrivateKey

from polysign.signer_ecdsa import SignerECDSA
from polysign.signer import SignerSubType

__version__ = "0.1.0"

_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141   # secp256k1 group order
HARDENED = 0x80000000
DEFAULT_COIN_TYPE = 0                                                    # app-local; not official SLIP-44
_SEED_DOMAIN = b"Bismuth seed"


class InvalidKeyError(ValueError):
    """A derivation step produced an invalid key (IL >= n or child == 0) — astronomically rare; per BIP32
    the caller should retry with the next index."""


class HDNode:
    """One node in the derivation tree: a private scalar + chain code. Knows how to derive children and
    to produce a Bismuth ECDSA signer/address."""

    __slots__ = ("key", "chain", "depth", "index")

    def __init__(self, key: bytes, chain: bytes, depth: int = 0, index: int = 0):
        if len(key) != 32 or len(chain) != 32:
            raise ValueError("key and chain must be 32 bytes")
        ik = int.from_bytes(key, "big")
        if ik == 0 or ik >= _N:
            raise InvalidKeyError("private key out of range")
        self.key = key
        self.chain = chain
        self.depth = depth
        self.index = index

    # --- key material -------------------------------------------------------
    @property
    def private_hex(self) -> str:
        return self.key.hex()

    @property
    def public_key_compressed(self) -> bytes:
        return PrivateKey(self.key).public_key.format(compressed=True)

    @property
    def public_key_hex(self) -> str:
        return self.public_key_compressed.hex()

    # --- derivation ---------------------------------------------------------
    def child(self, index: int) -> "HDNode":
        """CKDpriv: derive the child at ``index`` (>= HARDENED for a hardened child)."""
        index &= 0xFFFFFFFF
        if index & HARDENED:
            data = b"\x00" + self.key + index.to_bytes(4, "big")
        else:
            data = self.public_key_compressed + index.to_bytes(4, "big")
        I = hmac.new(self.chain, data, hashlib.sha512).digest()
        IL = int.from_bytes(I[:32], "big")
        if IL >= _N:
            raise InvalidKeyError("IL >= n at index %d (retry next index)" % index)
        child_k = (IL + int.from_bytes(self.key, "big")) % _N
        if child_k == 0:
            raise InvalidKeyError("child key == 0 at index %d (retry next index)" % index)
        return HDNode(child_k.to_bytes(32, "big"), I[32:], depth=self.depth + 1, index=index)

    def derive_path(self, path: str) -> "HDNode":
        """Derive a node at a BIP32 path like ``m/44'/0'/0'/0/5`` (``'`` or ``h``/``H`` marks hardened)."""
        node = self
        parts = [p for p in path.replace(" ", "").split("/") if p and p.lower() != "m"]
        for p in parts:
            hardened = p[-1] in ("'", "h", "H")
            n = int(p[:-1] if hardened else p)
            node = node.child(n + HARDENED if hardened else n)
        return node

    # --- Bismuth identity ---------------------------------------------------
    def signer(self, subtype: SignerSubType = SignerSubType.MAINNET_REGULAR) -> SignerECDSA:
        s = SignerECDSA()
        s.from_private_key(self.private_hex, subtype=subtype)
        return s

    def address(self, subtype: SignerSubType = SignerSubType.MAINNET_REGULAR) -> str:
        return SignerECDSA.public_key_to_address(self.public_key_compressed, subtype)


class HDWallet:
    """A seed-rooted HD wallet. ``receive_address(i)``/``receive_signer(i)`` walk the external chain so you
    can hand out a fresh address per payment; ``derive(path)`` exposes arbitrary BIP32 paths."""

    def __init__(self, seed: bytes, coin_type: int = DEFAULT_COIN_TYPE,
                 subtype: SignerSubType = SignerSubType.MAINNET_REGULAR):
        if len(seed) < 16:
            raise ValueError("seed should be >= 16 bytes of entropy")
        I = hmac.new(_SEED_DOMAIN, seed, hashlib.sha512).digest()
        self.master = HDNode(I[:32], I[32:], depth=0, index=0)
        self.coin_type = coin_type
        self.subtype = subtype

    @classmethod
    def from_passphrase(cls, passphrase: str, salt: str = "bismuth", iterations: int = 2048, **kw) -> "HDWallet":
        """Stretch a raw human passphrase into a 64-byte seed (a brain-wallet; PBKDF2-HMAC-SHA512). This is
        NOT BIP39 — for the standard 12/24-word mnemonic backup use ``from_mnemonic``/``new_mnemonic``."""
        seed = hashlib.pbkdf2_hmac("sha512", passphrase.encode("utf-8"),
                                   ("bismuth-hd:" + salt).encode("utf-8"), iterations, dklen=64)
        return cls(seed, **kw)

    @classmethod
    def from_mnemonic(cls, mnemonic: str, passphrase: str = "", **kw) -> "HDWallet":
        """Restore a wallet from a BIP39 mnemonic (the portable 12/24-word backup). Rejects a phrase whose
        checksum fails (a typo) before seeding. The optional ``passphrase`` is BIP39's 25th-word."""
        import bip39
        if not bip39.check(mnemonic):
            raise ValueError("invalid BIP39 mnemonic (unknown word or bad checksum)")
        return cls(bip39.to_seed(mnemonic, passphrase), **kw)

    @classmethod
    def new_mnemonic(cls, strength: int = 128, passphrase: str = "", **kw):
        """Create a brand-new wallet and return ``(mnemonic, wallet)``. Write the mnemonic down — it is the
        ONLY backup; ``strength`` 128 -> 12 words, 256 -> 24 words."""
        import bip39
        mnemonic = bip39.generate(strength)
        return mnemonic, cls.from_mnemonic(mnemonic, passphrase, **kw)

    def account_node(self, account: int = 0) -> HDNode:
        """The BIP44 account node m/44'/coin'/account'."""
        return self.master.derive_path("m/44'/%d'/%d'" % (self.coin_type, account))

    def node_at(self, index: int, account: int = 0, change: int = 0) -> HDNode:
        """The leaf node m/44'/coin'/account'/change/index. change=0 external (receive), 1 internal."""
        return self.account_node(account).child(change).child(index)

    def receive_address(self, index: int, account: int = 0) -> str:
        return self.node_at(index, account, change=0).address(self.subtype)

    def receive_signer(self, index: int, account: int = 0) -> SignerECDSA:
        return self.node_at(index, account, change=0).signer(self.subtype)

    def addresses(self, count: int, account: int = 0, change: int = 0, start: int = 0):
        """Yield ``count`` consecutive addresses (skipping the astronomically-rare invalid index)."""
        i, made = start, 0
        while made < count:
            try:
                yield i, self.node_at(i, account, change).address(self.subtype)
                made += 1
            except InvalidKeyError:
                pass            # per BIP32, skip and continue
            i += 1

    def scan(self, is_used, gap_limit: int = 20, account: int = 0, change: int = 0, start: int = 0,
             max_index: int = HARDENED) -> list:
        """Discover the USED addresses on one chain via the BIP44 gap-limit rule: walk indices from
        ``start`` and stop after ``gap_limit`` (default 20) CONSECUTIVE unused addresses. ``is_used(addr)``
        is a caller-supplied oracle — e.g. "does this address have any on-chain history / balance" — so
        this stays node-agnostic (wire it to the REST API / a balance call). Returns ``[(index, address)]``
        of the used addresses found, in order. A reset of the gap counter on each hit means a wallet with
        gaps smaller than ``gap_limit`` is fully recovered."""
        found, gap, i = [], 0, start
        while gap < gap_limit and i < max_index:
            try:
                addr = self.node_at(i, account, change).address(self.subtype)
            except InvalidKeyError:
                i += 1
                continue        # invalid index can't have been used; it doesn't count toward the gap
            if is_used(addr):
                found.append((i, addr))
                gap = 0
            else:
                gap += 1
            i += 1
        return found

    def scan_used_addresses(self, is_used, gap_limit: int = 20, account: int = 0,
                            include_change: bool = True) -> dict:
        """Convenience: scan BOTH the external (receive, change=0) and internal (change=1) chains, returning
        ``{"receive": [(i, addr)…], "change": [(i, addr)…]}`` — the full recovered address set for an
        account (the typical 'restore wallet from seed' sweep)."""
        out = {"receive": self.scan(is_used, gap_limit, account, change=0)}
        if include_change:
            out["change"] = self.scan(is_used, gap_limit, account, change=1)
        return out


# --- transaction signing for any raw-bytes-pubkey signer (ECDSA / ED25519) -------------------------
def tx_public_key_b64(signer) -> str:
    """The base64 the node expects in a tx's public_key field for an ECDSA/ED25519 signer: the RAW
    compressed pubkey BYTES, base64-encoded (NOT the hex string). verify_bis_signature b64decodes this and
    reconstructs the address from the bytes, so the encoding must be the raw key."""
    return b64encode(bytes.fromhex(signer._public_key)).decode("utf-8")


def sign_transaction(signer, timestamp, address, recipient, amount, operation="", openfield="",
                     post_fork=False) -> tuple:
    """Build + sign the 8-field Bismuth tx tuple with an ECDSA/ED25519 signer.

    Pre-fork (default): sign the canonical ``signature_buffer`` and carry the explicit public key (legacy).
    Post-fork (``post_fork=True``) for a single-sig secp256k1 signer: adopt the hf2 Ethereum-shape model —
    sign the 32-byte content txid with a RECOVERABLE compact signature (hex), and DROP the public key
    (field left empty; the node recovers the signer via ecrecover). Self-verifies before returning so a bad
    encoding fails loudly here rather than being silently rejected by the node."""
    import bismuth_serialize
    from polysign.signerfactory import SignerFactory
    amount_s = "%.8f" % float(amount)
    if post_fork and SignerFactory.is_single_sig_ecdsa(str(address)):
        # Stage 2 (doc/29): sign the BINARY-pre-image content txid (tx_id_v2_s), matching the digester.
        txid = bismuth_serialize.tx_id_v2_s(str(timestamp), str(address), str(recipient),
                                            amount_s, str(operation), str(openfield))
        sig_hex = signer.sign_buffer_for_bis_recoverable(bytes.fromhex(txid))
        signer.verify_bis_signature_recovered(sig_hex, txid, str(address))   # belt-and-suspenders
        return (str(timestamp), str(address), str(recipient), amount_s,
                sig_hex, "", str(operation), str(openfield))   # public_key dropped
    buffer = bismuth_serialize.signature_buffer(str(timestamp), str(address), str(recipient),
                                                amount_s, str(operation), str(openfield))
    sig_b64 = signer.sign_buffer_for_bis(buffer)
    pub_b64 = tx_public_key_b64(signer)
    SignerFactory.verify_bis_signature(sig_b64, pub_b64, buffer, str(address))   # belt-and-suspenders
    return (str(timestamp), str(address), str(recipient), amount_s,
            sig_b64, pub_b64, str(operation), str(openfield))
