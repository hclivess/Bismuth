"""
polysign/signer_secp256r1.py — a P-256 / secp256r1 (NIST prime256v1) ECDSA signer.

secp256r1 is the classical NIST curve behind WebAuthn/passkeys and most hardware secure-elements
(YubiKey, Secure Enclave, TPM). It is the natural scheme to bridge those devices to a Bismuth address,
alongside the existing secp256k1 (`signer_ecdsa.py`) and ed25519 schemes. It is NOT post-quantum — it
is a useful, widely-deployed classical option in the menu.

This signer follows Bismuth's RSA/ML-DSA convention of a **hash-of-pubkey address** (the address commits
to `sha256(pubkey)`; the full pubkey rides in the tx's `public_key` field), giving short addresses
regardless of how the pubkey is encoded. Conventions:

  * the **wallet key is a 32-byte seed**; the EC private scalar is derived deterministically from it
    (`derive_private_key(int(seed) mod n)`), so re-loading the seed reproduces the keypair;
  * the **public key** is the uncompressed X9.62 point (65 bytes);
  * **signatures** are DER-encoded ECDSA over `SHA-256(message)`, base64-encoded for the bis network
    format (the same b64 envelope as ecdsa/ed25519).

Depends on `cryptography`, imported lazily by the factory so RSA-only nodes have no hard dependency on
it.
"""
from base64 import b64decode, b64encode
from hashlib import sha256
from os import urandom
from typing import Union

import base58
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from polysign.signer import Signer, SignerType, SignerSubType

# Order n of the secp256r1 group; a private scalar must be in [1, n-1].
_SECP256R1_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551


class SignerSECP256R1(Signer):

    __slots__ = ('_key',)

    # distinct 4-byte address version so secp256r1 addresses are self-identifying (base58)
    _address_versions = {SignerSubType.MAINNET_REGULAR: b'\x06\x52\x31\x00',
                         SignerSubType.TESTNET_REGULAR: b'\x14\x52\x31\x00'}

    def __init__(self, private_key: Union[bytes, str] = b'', public_key: Union[bytes, str] = b'', address: str = '',
                 compressed: bool = False, subtype: SignerSubType = SignerSubType.MAINNET_REGULAR):
        super().__init__(private_key, public_key, address, compressed=compressed, subtype=subtype)
        self._key = None
        self._type = SignerType.SECP256R1

    # --- key material -----------------------------------------------------
    @staticmethod
    def _public_bytes(key: ec.EllipticCurvePrivateKey) -> bytes:
        """Uncompressed X9.62 point (65 bytes) for the public key."""
        return key.public_key().public_bytes(serialization.Encoding.X962,
                                              serialization.PublicFormat.UncompressedPoint)

    def _set_from_seed(self, seed_bytes: bytes, subtype: SignerSubType):
        if subtype != SignerSubType.MAINNET_REGULAR:
            self._subtype = subtype
        # Deterministic scalar in [1, n-1] from the 32-byte seed.
        scalar = (int.from_bytes(seed_bytes, 'big') % (_SECP256R1_ORDER - 1)) + 1
        key = ec.derive_private_key(scalar, ec.SECP256R1())
        self._key = key
        self._private_key = seed_bytes.hex()              # the WALLET stores the 32-byte seed
        self._public_key = self._public_bytes(key).hex()
        self._address = self.address()

    def from_seed(self, seed: str = '', subtype: SignerSubType = SignerSubType.MAINNET_REGULAR):
        """Deterministic secp256r1 keypair from a seed (any length -> a stable 32-byte derive seed)."""
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
        return self._addr(self._public_bytes(self._key), self._subtype)

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
        try:
            pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), public_key)
            pub.verify(signature, buffer, ec.ECDSA(hashes.SHA256()))
        except (InvalidSignature, ValueError) as e:
            raise ValueError(f"Invalid secp256r1 signature from {address}: {e}")
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
        return self._key.sign(buffer, ec.ECDSA(hashes.SHA256()))   # DER-encoded ECDSA over SHA-256

    def sign_buffer_for_bis(self, buffer: bytes) -> str:
        return b64encode(self.sign_buffer_raw(buffer)).decode('utf-8')
