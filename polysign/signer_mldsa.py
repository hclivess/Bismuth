"""
polysign/signer_mldsa.py — the ML-DSA family (FIPS 204 / CRYSTALS-Dilithium) post-quantum signers,
covering all three NIST levels: ML-DSA-44 (Cat 2), ML-DSA-65 (Cat 3) and ML-DSA-87 (Cat 5).

A drop-in `polysign` signer family with the SAME contract as RSA/ECDSA/ed25519, so the digester,
mempool and wallet drive them through the `SignerFactory` unchanged. Two adaptations for a lattice
scheme:

  * the **wallet key is a 32-byte seed** (FIPS 204 deterministic KeyGen), not the multi-KB secret key —
    small to store, and the full keypair is re-derived from it;
  * the **address is a HASH of the public key** (an ML-DSA pubkey is 1.3–2.6 KB — far too large to
    embed the way ed25519 does), exactly like Bismuth's RSA scheme. The full pubkey rides in the tx's
    `public_key` field; the address commits to it.

This is the post-quantum OPTION made real and testable (doc/20). It is **inert on consensus** until a
signalled `pq` fork enables acceptance of these signature types — the code exists and round-trips, but
no mainnet path mints or validates ML-DSA txs yet.

Each level is its own self-identifying signer (own `SignerType`, own distinct 4-byte address version),
sharing all behaviour through `_SignerMLDSABase`. `SignerMLDSA` is kept as a back-compat alias for the
Category-3 `SignerMLDSA65`.

Depends on `dilithium-py` (pure-Python ML-DSA), imported lazily by the factory so RSA-only nodes have
no hard dependency on it.
"""
from base64 import b64decode, b64encode
from hashlib import sha256
from os import urandom
from typing import Union

import base58
from dilithium_py.ml_dsa import ML_DSA_44, ML_DSA_65, ML_DSA_87

from polysign.signer import Signer, SignerType, SignerSubType


class _SignerMLDSABase(Signer):
    """Shared ML-DSA behaviour. Concrete subclasses bind `_ml_dsa` (the dilithium_py parameter set),
    `_type` (their own SignerType) and `_address_versions` (their own distinct prefixes)."""

    __slots__ = ('_sk', '_pk')

    # Concrete subclasses override these three.
    _ml_dsa = None                       # one of dilithium_py.ml_dsa.ML_DSA_{44,65,87}
    _level_name = 'ML-DSA'               # for error messages
    # _address_versions is inherited from Signer and overridden per subclass.

    def __init__(self, private_key: Union[bytes, str] = b'', public_key: Union[bytes, str] = b'', address: str = '',
                 compressed: bool = False, subtype: SignerSubType = SignerSubType.MAINNET_REGULAR):
        super().__init__(private_key, public_key, address, compressed=compressed, subtype=subtype)
        self._sk = None
        self._pk = None
        # self._type is set by the concrete subclass below.

    # --- key material -----------------------------------------------------
    def _set_from_seed(self, seed_bytes: bytes, subtype: SignerSubType):
        if subtype != SignerSubType.MAINNET_REGULAR:
            self._subtype = subtype
        pk, sk = self._ml_dsa._keygen_internal(seed_bytes)   # FIPS 204 deterministic KeyGen
        self._sk, self._pk = sk, pk
        self._private_key = seed_bytes.hex()              # the WALLET stores the 32-byte seed
        self._public_key = pk.hex()
        self._address = self.address()

    def from_seed(self, seed: str = '', subtype: SignerSubType = SignerSubType.MAINNET_REGULAR):
        """Deterministic ML-DSA keypair from a seed (any length -> a stable 32-byte KeyGen seed)."""
        if not seed:
            seed_bytes = urandom(32)
        else:
            seed_bytes = sha256(seed.encode() if isinstance(seed, str) else bytes(seed)).digest()
        self._set_from_seed(seed_bytes, subtype)

    def from_private_key(self, private_key: Union[bytes, str], subtype: SignerSubType = SignerSubType.MAINNET_REGULAR):
        """Load from the stored 32-byte seed (hex or bytes); re-derives the full keypair."""
        seed_bytes = bytes.fromhex(private_key) if isinstance(private_key, str) else bytes(private_key)
        self._set_from_seed(seed_bytes, subtype)

    def from_full_info(self, private_key: Union[bytes, str], public_key: Union[bytes, str] = b'', address: str = '',
                       subtype: SignerSubType = SignerSubType.MAINNET_REGULAR, verify: bool = True):
        self.from_private_key(private_key, subtype=subtype)

    # --- address (hash of pubkey) ----------------------------------------
    @classmethod
    def _addr(cls, public_key: bytes, subtype: SignerSubType = SignerSubType.MAINNET_REGULAR) -> str:
        base = cls.address_version_for_subtype(subtype) + sha256(public_key).digest()   # COMMIT, not embed
        return base58.b58encode(base + sha256(base).digest()[:4]).decode('utf-8')

    def address(self) -> str:
        return self._addr(self._pk, self._subtype)

    @classmethod
    def public_key_to_address(cls, public_key: Union[bytes, str],
                              subtype: SignerSubType = SignerSubType.MAINNET_REGULAR) -> str:
        if isinstance(public_key, str):
            public_key = bytes.fromhex(public_key)
        return cls._addr(public_key, subtype)

    # --- verify / sign ----------------------------------------------------
    @classmethod
    def verify_signature(cls, signature: Union[bytes, str], public_key: Union[bytes, str], buffer: bytes,
                         address: str = '') -> None:
        if isinstance(signature, str):
            signature = bytes.fromhex(signature)
        if isinstance(public_key, str):
            public_key = bytes.fromhex(public_key)
        if not cls._ml_dsa.verify(public_key, buffer, signature):
            raise ValueError(f"Invalid {cls._level_name} signature from {address}")
        rebuilt = cls.public_key_to_address(public_key)
        if address and address != rebuilt:
            raise ValueError(f"Attempt to spend from a wrong address {address} instead of {rebuilt}")

    @classmethod
    def verify_bis_signature(cls, signature: str, public_key: str, buffer: bytes, address: str = '') -> None:
        """Bismuth network format: sig + pubkey are base64-encoded."""
        cls.verify_signature(b64decode(signature), b64decode(public_key), buffer, address)

    @classmethod
    def verify_bis_signature_raw(cls, signature: bytes, public_key: bytes, buffer: bytes, address: str = '') -> None:
        cls.verify_signature(signature, public_key, buffer, address)

    def sign_buffer_raw(self, buffer: bytes) -> bytes:
        return self._ml_dsa.sign(self._sk, buffer)

    def sign_buffer_for_bis(self, buffer: bytes) -> str:
        return b64encode(self.sign_buffer_raw(buffer)).decode('utf-8')


class SignerMLDSA44(_SignerMLDSABase):
    """ML-DSA-44 (FIPS 204 / CRYSTALS-Dilithium2, NIST Security Category 2)."""

    __slots__ = ()

    _ml_dsa = ML_DSA_44
    _level_name = 'ML-DSA-44'
    # distinct 4-byte address version so ML-DSA-44 addresses are self-identifying (base58)
    _address_versions = {SignerSubType.MAINNET_REGULAR: b'\x06\x4d\x44\x32',
                         SignerSubType.TESTNET_REGULAR: b'\x14\x4d\x44\x32'}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._type = SignerType.MLDSA44


class SignerMLDSA65(_SignerMLDSABase):
    """ML-DSA-65 (FIPS 204 / CRYSTALS-Dilithium3, NIST Security Category 3)."""

    __slots__ = ()

    _ml_dsa = ML_DSA_65
    _level_name = 'ML-DSA-65'
    # distinct 4-byte address version so ML-DSA-65 addresses are self-identifying (base58, "Mdsa…")
    _address_versions = {SignerSubType.MAINNET_REGULAR: b'\x06\x4d\x44\x53',
                         SignerSubType.TESTNET_REGULAR: b'\x14\x4d\x44\x53'}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._type = SignerType.MLDSA65


class SignerMLDSA87(_SignerMLDSABase):
    """ML-DSA-87 (FIPS 204 / CRYSTALS-Dilithium5, NIST Security Category 5)."""

    __slots__ = ()

    _ml_dsa = ML_DSA_87
    _level_name = 'ML-DSA-87'
    # distinct 4-byte address version so ML-DSA-87 addresses are self-identifying (base58)
    _address_versions = {SignerSubType.MAINNET_REGULAR: b'\x06\x4d\x44\x38',
                         SignerSubType.TESTNET_REGULAR: b'\x14\x4d\x44\x38'}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._type = SignerType.MLDSA87


# Back-compat alias: the original single ML-DSA signer was ML-DSA-65 (Category 3).
# tests/test_pq_signer.py and signerfactory.py both rely on this name.
SignerMLDSA = SignerMLDSA65
