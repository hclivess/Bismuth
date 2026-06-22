"""Factory and dispatch helpers for Bismuth's pluggable signature schemes.

Maps signer *types* (:class:`~polysign.signer.SignerType`) and on-the-wire
*addresses* to the concrete :class:`~polysign.signer.Signer` subclass that
handles them, and exposes convenience constructors (from a private key, a seed,
or full info) plus address validation / signature verification entry points
used across the node.

Only the RSA signer (Bismuth mainnet) is imported eagerly; every other scheme
is loaded lazily via :func:`_load_optional_signer` so an RSA-only deployment
carries no hard dependency on the optional native-crypto packages.
"""

import re
from os import urandom
from typing import Type, Union

import bismuth_serialize
from polysign.signer import Signer, SignerType, SignerSubType
from polysign.signer_rsa import SignerRSA  # RSA = Bismuth mainnet scheme; depends only on pycryptodomex

# Non-RSA signers depend on optional native crypto (coincurve, ed25519/pynacl). They are imported
# lazily so an RSA-only node (Bismuth mainnet) has no hard dependency on those packages.
# Reintegrated into the core tree from the external `polysign` package; see doc/14.
_OPTIONAL_SIGNERS = {
    "SignerECDSA": ("polysign.signer_ecdsa", "SignerECDSA"),
    "SignerED25519": ("polysign.signer_ed25519", "SignerED25519"),
    "SignerMLDSA": ("polysign.signer_mldsa", "SignerMLDSA"),       # ML-DSA-65 post-quantum (doc/20), back-compat name
    "SignerMLDSA44": ("polysign.signer_mldsa", "SignerMLDSA44"),   # ML-DSA-44 / NIST Cat 2 (doc/20)
    "SignerMLDSA65": ("polysign.signer_mldsa", "SignerMLDSA65"),   # ML-DSA-65 / NIST Cat 3 (doc/20)
    "SignerMLDSA87": ("polysign.signer_mldsa", "SignerMLDSA87"),   # ML-DSA-87 / NIST Cat 5 (doc/20)
    "SignerSECP256R1": ("polysign.signer_secp256r1", "SignerSECP256R1"),   # P-256 / secp256r1 ECDSA
    "SignerBTC": ("polysign.signer_btc", "SignerBTC"),
    "SignerCRW": ("polysign.signer_crw", "SignerCRW"),
    "SignerMultisig": ("polysign.signer_multisig", "SignerMultisig"),      # native M-of-N (doc/23 §2, hf2)
}
_optional_signer_cache = {}


def _load_optional_signer(name):
    """Import and cache a non-RSA signer class on first use (keeps coincurve/ed25519 optional)."""
    if name not in _optional_signer_cache:
        module_name, attr = _OPTIONAL_SIGNERS[name]
        module = __import__(module_name, fromlist=[attr])
        _optional_signer_cache[name] = getattr(module, attr)
    return _optional_signer_cache[name]


RE_RSA_ADDRESS = re.compile(r"^[abcdef0123456789]{56}$")
# Improve ECDSA/ED25519 by Bizzzy
RE_ECDSA_ADDRESS = re.compile(r"^Bis1[123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz]{28,52}$")
# Native M-of-N multisig (doc/23 §2): the MULTISIG address versions base58-encode to a fixed prefix —
# mainnet 'Bism', testnet 'mBis' — distinct from regular ECDSA 'Bis1', so routing never collides.
RE_MULTISIG_ADDRESS = re.compile(r"^(Bism|mBis)[123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz]{28,56}$")


def signer_for_type(signer_type: SignerType) -> Union[Type[Signer], None]:
    """Returns the class matching a signer type."""
    if signer_type == SignerType.RSA:
        return SignerRSA
    optional = {SignerType.ED25519: "SignerED25519", SignerType.ECDSA: "SignerECDSA",
                SignerType.MLDSA: "SignerMLDSA",   # ML-DSA-65 post-quantum (doc/20); == SignerType.MLDSA65
                SignerType.MLDSA44: "SignerMLDSA44",   # ML-DSA-44 / NIST Cat 2 (doc/20)
                SignerType.MLDSA87: "SignerMLDSA87",   # ML-DSA-87 / NIST Cat 5 (doc/20)
                SignerType.SECP256R1: "SignerSECP256R1",   # P-256 / secp256r1 ECDSA
                SignerType.BTC: "SignerBTC", SignerType.CRW: "SignerCRW"}
    name = optional.get(signer_type)
    return _load_optional_signer(name) if name else None


class SignerFactory:
    """Create signers and dispatch verification by signer type or address."""

    @classmethod
    def from_private_key(cls, private_key: Union[bytes, str], signer_type: SignerType=SignerType.RSA,
                         subtype: SignerSubType=SignerSubType.MAINNET_REGULAR) -> Signer:
        """Detect the type of the key, creates and return the matching signer"""
        # TODO: detect by private_key
        signer_class = signer_for_type(signer_type)
        if signer_class is None:
            raise ValueError("Unsupported Key type")
        signer = signer_class()
        signer.from_private_key(private_key, subtype=subtype)
        return signer

    @classmethod
    def from_full_info(cls, private_key: Union[bytes, str], public_key: Union[bytes, str]=b'', address: str='',
                       signer_type: SignerType=SignerType.RSA, subtype: SignerSubType=SignerSubType.MAINNET_REGULAR,
                       verify: bool=True) -> Signer:
        """Build a signer from full key info (not implemented; placeholder)."""
        pass

    # ML-DSA (44/65/87) and secp256r1 carry a distinct 4-byte base58 address VERSION that self-identifies the
    # scheme. Their leading characters can collide (ML-DSA-44 and -87 both render "KALo…"), so they are routed
    # by the DECODED version, not a prefix regex. base58 is imported lazily so an RSA-only node stays light.
    _VERSIONED_SIGNERS = ("SignerMLDSA44", "SignerMLDSA65", "SignerMLDSA87", "SignerSECP256R1")

    @classmethod
    def _versioned_signer(cls, address: str):
        """Signer class for a post-quantum / NIST address family (ML-DSA, secp256r1) matched by its 4-byte
        base58 version prefix, or None if `address` is not one of those (incl. unreadable/short input)."""
        try:
            import base58
            raw = base58.b58decode(address)
        except Exception:
            return None
        if len(raw) < 8:
            return None
        version = bytes(raw[:4])
        for name in cls._VERSIONED_SIGNERS:
            try:
                sc = _load_optional_signer(name)
            except Exception:
                continue
            if version in sc._address_versions.values():
                return sc
        return None

    @classmethod
    def address_to_signer(cls, address: str) -> Type[Signer]:
        """Return the signer class matching an address (by its textual format / version prefix)."""
        if RE_RSA_ADDRESS.match(address):
            return SignerRSA
        elif RE_MULTISIG_ADDRESS.match(address):
            return _load_optional_signer("SignerMultisig")
        elif RE_ECDSA_ADDRESS.match(address):
            if len(address) > 50:
                return _load_optional_signer("SignerED25519")
            else:
                return _load_optional_signer("SignerECDSA")
        vs = cls._versioned_signer(address)   # ML-DSA / secp256r1 (post-quantum / NIST families, doc/20)
        if vs is not None:
            return vs
        raise ValueError("Unsupported Address type")

    @classmethod
    def address_is_multisig(cls, address: str) -> bool:
        """True iff ``address`` is a native M-of-N multisig address (doc/23 §2). Used by the digester to
        fork-gate multisig SENDERS to post-hf2 (an upgraded node must not accept what a pre-fork node
        would reject). Cheap textual check — the verifier re-derives the address from the redeem anyway."""
        return RE_MULTISIG_ADDRESS.match(address) is not None

    @classmethod
    def address_is_post_fork_sender_type(cls, address: str) -> bool:
        """True iff `address` is a base-layer sender type only a POST-hf2 node understands — native multisig
        (doc/23) OR the ML-DSA / secp256r1 families (doc/20). The digester fork-gates these SENDERS to
        post-activation so an upgraded node never accepts a spend a pre-fork node would reject (chain-split
        safety). Receiving INTO such an address is always fine (it's just an address)."""
        return RE_MULTISIG_ADDRESS.match(address) is not None or cls._versioned_signer(address) is not None

    @classmethod
    def address_is_valid(cls, address: str) -> bool:
        """Return whether the address matches any supported (RSA / ECDSA / ED25519) format."""
        if RE_RSA_ADDRESS.match(address):
            # RSA, 56 hex
            return True
        elif RE_MULTISIG_ADDRESS.match(address):
            # native M-of-N multisig (doc/23 §2), ~37 chars — a valid sender (post-fork) and recipient
            return True
        elif RE_ECDSA_ADDRESS.match(address):
            if 50 < len(address) < 60:
                # ED25519, around 54
                return True
            if 30 < len(address) < 50:
                # ecdsa, around 37
                return True
        if cls._versioned_signer(address) is not None:
            return True   # ML-DSA (44/65/87) / secp256r1 (doc/20)
        return False

    @classmethod
    def address_is_rsa(cls, address: str) -> bool:
        """Returns whether the given address is a legacy RSA one"""
        return RE_RSA_ADDRESS.match(address) is not None

    @classmethod
    def from_seed(cls, seed: str='', signer_type: SignerType=SignerType.RSA,
                  subtype: SignerSubType=SignerSubType.MAINNET_REGULAR) -> Signer:
        """Create a signer of the given type from a seed (random if ``seed`` is empty)."""
        if seed == '':
            seed = urandom(32).hex()
        signer_class = signer_for_type(signer_type)
        if signer_class is None:
            raise ValueError("Unsupported Key type")
        signer = signer_class()
        signer.from_seed(seed, subtype)
        return signer

    @classmethod
    def verify_bis_signature(cls, signature: str, public_key: str, buffer: bytes, address: str, ) -> None:
        """Verify signature from bismuth tx network format"""
        # Find the right signer class
        verifier = cls.address_to_signer(address)
        # let it do the job
        verifier.verify_bis_signature(signature, public_key, buffer, address)

    @classmethod
    def verify_bis_signature_raw(cls, signature: bytes, public_key: bytes, buffer: bytes, address: str, ) -> None:
        """Verify signature from decoded - bin - sig and pubkey"""
        verifier = cls.address_to_signer(address)
        verifier.verify_bis_signature_raw(signature, public_key, buffer, address)

    @classmethod
    def is_single_sig_ecdsa(cls, address: str) -> bool:
        """True iff ``address`` is a regular (non-multisig) secp256k1 ECDSA address — the ONLY scheme that
        uses the post-hf2 Ethereum-shape path (recoverable sig over the txid, public key dropped, ecrecover).
        ED25519 (Bis1 with len > 50), RSA (56 hex) and native multisig (Bism/mBis) are excluded."""
        return bool(RE_ECDSA_ADDRESS.match(address)) and len(address) <= 50

    @classmethod
    def is_single_sig_ed25519(cls, address: str) -> bool:
        """True iff `address` is an ED25519 address (Bis1... with len > 50 — distinct from the shorter
        secp256k1 ECDSA addresses). Post-hf2 these DROP the public key (it is embedded in the address and
        recovered via SignerED25519.public_key_from_address), the stateless analogue of secp256k1 ecrecover."""
        return bool(RE_ECDSA_ADDRESS.match(address)) and len(address) > 50

    @classmethod
    def verify_tx_signature(cls, post_fork: bool, timestamp, address, recipient, amount,
                            operation, openfield, signature, public_key) -> None:
        """Single fork-aware tx-signature verification used by BOTH the digester and the mempool.

        Post-hf2, an ordinary single-sig secp256k1 sender uses the Ethereum-shape model: the signature
        signs the 32-byte content txid and is a recoverable compact sig with NO public key (the signer is
        recovered via ecrecover and must match the sender address). Every other case — all pre-fork txs,
        and post-fork RSA / ED25519 / native-multisig — keeps the legacy scheme: verify the signature over
        the frozen ``signature_buffer`` against the explicit public key. (The content-hash txid is the
        canonical id for ALL post-fork txs regardless of which signing scheme verified them.)"""
        if post_fork and cls.is_single_sig_ecdsa(address):
            # audit L-1: the recoverable path DROPS the public key (the signer is recovered via ecrecover),
            # so a non-empty public_key is unverified malleability noise — reject it rather than ignore it.
            if str(public_key or "").strip() != "":
                raise ValueError("post-fork single-sig secp256k1 tx must carry an empty public key")
            # Stage 2 (doc/29): the recoverable sig signs the BINARY-pre-image content txid (tx_id_v2_s).
            txid = bismuth_serialize.tx_id_v2_s(timestamp, address, recipient, amount, operation, openfield)
            _load_optional_signer("SignerECDSA").verify_bis_signature_recovered(signature, txid, address)
            return
        if post_fork and cls.is_single_sig_ed25519(address):
            # doc/29 Stage 3: ED25519 drops the pubkey post-fork — recover it from the address (the key is
            # embedded there) and verify the base64 sig over the legacy buffer with the recovered key.
            # A non-empty pubkey is unverified noise → reject (one canonical form, like the secp256k1 rule).
            if str(public_key or "").strip() != "":
                raise ValueError("post-fork single-sig ED25519 tx must carry an empty public key")
            import base64 as _b64
            ed = _load_optional_signer("SignerED25519")
            pub = ed.public_key_from_address(address)
            buffer = bismuth_serialize.signature_buffer(timestamp, address, recipient, amount, operation, openfield)
            ed.verify_bis_signature_raw(_b64.b64decode(signature), pub, buffer, address)
            return
        buffer = bismuth_serialize.signature_buffer(timestamp, address, recipient, amount, operation, openfield)
        cls.verify_bis_signature(signature, public_key, buffer, address)
