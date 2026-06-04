"""

"""

import random
from base64 import b64decode, b64encode
from hashlib import sha256
from os import urandom
from typing import Union

import base58
import ed25519
from polysign.signer import Signer, SignerType, SignerSubType


class SignerED25519(Signer):

    __slots__ = ('_key', )

    _address_versions = {SignerSubType.MAINNET_REGULAR: b'\x03\xb8\x6c\xf3',
                         SignerSubType.MAINNET_MULTISIG: b'\x03\xb8\x72\x14',
                         SignerSubType.TESTNET_REGULAR: b'\x11\xc2\xce\x7c',
                         SignerSubType.TESTNET_MULTISIG: b'\x0f\x54\xfd\x2d'}

    def __init__(self, private_key: Union[bytes, str]=b'', public_key: Union[bytes, str]=b'', address: str='',
                 compressed: bool=False, subtype: SignerSubType=SignerSubType.MAINNET_REGULAR):
        super().__init__(private_key, public_key, address, compressed=compressed, subtype=subtype)
        self._key = None
        self._type = SignerType.ED25519

    def from_private_key(self, private_key: Union[bytes, str], subtype: SignerSubType=SignerSubType.MAINNET_REGULAR):
        """Accepts both bytes[32] or str (hex format)"""
        if subtype != SignerSubType.MAINNET_REGULAR:
            self._subtype = subtype
        if type(private_key) == str:
            return self.from_seed(private_key, subtype=self._subtype)
        return self.from_seed(private_key.hex(), subtype=self._subtype)

    def from_full_info(self, private_key: Union[bytes, str], public_key: Union[bytes, str]=b'', address: str='',
                       subtype: SignerSubType = SignerSubType.MAINNET_REGULAR, verify: bool=True):
        raise ValueError("SignerED25519.from_full_info not impl.")

    def from_seed(self, seed: str='', subtype: SignerSubType=SignerSubType.MAINNET_REGULAR):
        """Creates key from seed - for ED25519, seed = pk - 32 bytes random buffer"""
        if subtype != SignerSubType.MAINNET_REGULAR:
            self._subtype = subtype
        if len(seed) > 64:
            # Too long seed, trim (could use better scheme for more entropy)
            seed = seed[:64]
        elif seed == '':
            # No seed, use urandom
            seed = urandom(32)
        elif len(seed) < 64:
            # Too short seed, use as PRNG seed
            random.seed(seed)
            seed = hex(random.getrandbits(32*8))[2:]
            while len(seed) < 64:
                seed = '0' + seed
            assert len(seed) == 64
        try:
            # print("SEED", seed)
            # TODO: check flow, there may be many unnecessary hex-byte-hex-bytes conversions from top to bottom
            key = ed25519.SigningKey(bytes.fromhex(seed))
            hexa = key.to_ascii(encoding="hex").decode('utf-8')
            # print("ED25519 Privk Key", hexa)  # e5b42f3c-3fe02e16-1d42ff47-07a174a5 715b2badc7d4d3aebbea9081bd9123d5
            verifying_key = key.get_verifying_key()
            public_key = verifying_key.to_ascii(encoding="hex").decode('utf-8')
            # public_key = hexa[32:]
            # print("ED25519 Public Key", public_key)
            self._key = key
            self._private_key = hexa
            self._public_key = public_key
        except Exception as e:
            print("Exception {} reading ED25519 private key".format(e))
        # print("identifier", self.identifier().hex())
        self._address = self.address()

    """
    def identifier(self):
        #Returns double hash of pubkey as per btc standards
        return hashlib.new('ripemd160', sha256(bytes.fromhex(self._public_key)).digest()).digest()
    """

    def address(self) -> str:
        """Returns properly serialized address from pubkey"""
        # No double hash for pubkey, nor for checksum
        base = self.address_version_for_subtype(self._subtype) + bytes.fromhex(self._public_key)  # raw content
        chk = sha256(base).digest()[:4]
        return base58.b58encode(base + chk).decode('utf-8')

    @classmethod
    def public_key_to_address(cls, public_key: Union[bytes, str],
                              subtype: SignerSubType=SignerSubType.MAINNET_REGULAR) -> str:
        """Reconstruct an address from the public key"""
        # TODO: same for this family, could factorize in an ancestor with other methods
        if type(public_key) == str:
            public_key = bytes.fromhex(public_key)
        base = cls.address_version_for_subtype(subtype) + public_key  # raw content
        checksum = sha256(base).digest()[:4]
        return base58.b58encode(base + checksum).decode('utf-8')

    @classmethod
    def verify_signature(cls, signature: Union[bytes, str], public_key: Union[bytes, str], buffer: bytes,
                         address: str='') -> None:
        """Verify signature from raw signature. Address may be used to determine the sig subtype"""
        try:
            # print("verif", signature, public_key, len(public_key))
            verifying_key = ed25519.VerifyingKey(public_key)
            verifying_key.verify(signature, buffer)
        except Exception as e:
            print(e)
            raise ValueError(f"Invalid ED25519 signature from {address}")
        # Reconstruct address from pubkey to make sure it matches
        address_rebuild = cls.public_key_to_address(public_key)
        if address != address_rebuild:
            raise ValueError(f"Attempt to spend from a wrong address {address} instead of {address_rebuild}")

    @classmethod
    def verify_bis_signature(cls, signature: str, public_key: str, buffer: bytes, address: str = '') -> None:
        """Verify signature from bismuth tx network format (ecdsa sig and pubkey are b64 encoded)
        Returns None, but raises ValueError if needed."""
        cls.verify_signature(b64decode(signature), b64decode(public_key), buffer, address)

    @classmethod
    def verify_bis_signature_raw(cls, signature: bytes, public_key: bytes, buffer: bytes, address: str = '') -> None:
        """Verify signature from bin format
        Returns None, but raises ValueError if needed."""
        cls.verify_signature(signature, public_key, buffer, address)

    def sign_buffer_raw(self, buffer: bytes) -> bytes:
        """Sign a buffer, sends a raw bytes array"""
        return self._key.sign(buffer)

    def sign_buffer_for_bis(self, buffer: bytes) -> str:
        """Sign a buffer, sends under the format expected by bismuth network format"""
        return b64encode(self.sign_buffer_raw(buffer)).decode('utf-8')
