"""

"""

import hashlib
import random
from base64 import b64decode, b64encode
from hashlib import sha256
from os import urandom
from typing import Union

import base58
from coincurve import PrivateKey, verify_signature
from polysign.signer import Signer, SignerType, SignerSubType


class SignerECDSA(Signer):

    __slots__ = ('_key', )

    _address_versions = {SignerSubType.MAINNET_REGULAR: b'\x4f\x54\x5b',
                         SignerSubType.MAINNET_MULTISIG: b'\x4f\x54\xc8',
                         SignerSubType.TESTNET_REGULAR: b'\x01\x7a\xb6\x85',
                         SignerSubType.TESTNET_MULTISIG: b'\x01\x46\xeb\xa5'}

    def __init__(self, private_key: Union[bytes, str]=b'', public_key: Union[bytes, str]=b'', address: str='',
                 compressed: bool=True, subtype: SignerSubType=SignerSubType.MAINNET_REGULAR):
        super().__init__(private_key, public_key, address, compressed=compressed, subtype=subtype)
        self._key = None
        self._type = SignerType.ECDSA

    def from_private_key(self, private_key: Union[bytes, str], subtype: SignerSubType=SignerSubType.MAINNET_REGULAR):
        """Accepts both bytes[32] or str (hex format)"""
        if subtype != SignerSubType.MAINNET_REGULAR:
            self._subtype = subtype
        if type(private_key) == str:
            return self.from_seed(private_key, subtype=self._subtype)
        return self.from_seed(private_key.hex())

    def from_full_info(self, private_key: Union[bytes, str], public_key: Union[bytes, str]=b'', address: str='',
                       subtype: SignerSubType = SignerSubType.MAINNET_REGULAR, verify: bool=True):
        raise ValueError("SignerRsa.from_full_info not impl.")

    def from_seed(self, seed: str='', subtype: SignerSubType=SignerSubType.MAINNET_REGULAR):
        """Creates key from seed - for ecdsa, seed = pk - 32 bytes random buffer"""
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
            key = PrivateKey.from_hex(seed)
            public_key = key.public_key.format(compressed=True).hex()
            # print("Public Key", public_key)
            self._key = key
            self._private_key = key.to_hex()  # == seed
            self._public_key = public_key
        except Exception as e:
            print("Exception {} reading RSA private key".format(e))
        # print("identifier", self.identifier().hex())
        self._address = self.address()

    def identifier(self):
        """Returns double hash of pubkey as per btc standards"""
        return hashlib.new('ripemd160', sha256(bytes.fromhex(self._public_key)).digest()).digest()

    def address(self):
        """Returns properly serialized address from pubkey as per btc standards"""
        vh160 = self.address_version_for_subtype(self._subtype) + self.identifier()  # raw content
        chk = sha256(sha256(vh160).digest()).digest()[:4]
        return base58.b58encode(vh160 + chk).decode('utf-8')

    @classmethod
    def public_key_to_address(cls, public_key: Union[bytes, str],
                              subtype: SignerSubType=SignerSubType.MAINNET_REGULAR) -> str:
        """Reconstruct an address from the public key"""
        if type(public_key) == str:
            identifier = hashlib.new('ripemd160', sha256(bytes.fromhex(public_key)).digest()).digest()
        else:
            identifier = hashlib.new('ripemd160', sha256(public_key).digest()).digest()
        vh160 = cls.address_version_for_subtype(subtype) + identifier  # raw content
        checksum = sha256(sha256(vh160).digest()).digest()[:4]
        return base58.b58encode(vh160 + checksum).decode('utf-8')

    @classmethod
    def verify_signature(cls, signature: Union[bytes, str], public_key: Union[bytes, str], buffer: bytes,
                         address: str='') -> None:
        """Verify signature from raw signature. Address may be used to determine the sig type"""
        raise ValueError("SignerECDSA.verify_signature not impl.")

    @classmethod
    def verify_bis_signature(cls, signature: str, public_key: str, buffer: bytes, address: str = '') -> None:
        """Verify signature from bismuth tx network format (ecdsa sig and pubkey are b64 encoded)
        Returns None, but raises ValueError if needed."""
        public_key = b64decode(public_key)
        valid = verify_signature(b64decode(signature), buffer, public_key)
        if not valid:
            raise ValueError(f"Invalid signature from {address}")
        # Reconstruct address from pubkey to make sure it matches
        if address != cls.public_key_to_address(public_key):
            raise ValueError("Attempt to spend from a wrong address")

    @classmethod
    def verify_bis_signature_raw(cls, signature: bytes, public_key: bytes, buffer: bytes, address: str = '') -> None:
        """Verify signature from bin format
        Returns None, but raises ValueError if needed."""
        # print("ecdsa verify_bis_signature_raw pubkey", public_key, "sig", signature)
        valid = verify_signature(signature, buffer, public_key)
        if not valid:
            raise ValueError(f"Invalid signature from {address}")
        # Reconstruct address from pubkey to make sure it matches
        if address != cls.public_key_to_address(public_key):
            raise ValueError("Attempt to spend from a wrong address")

    def sign_buffer_raw(self, buffer: bytes) -> bytes:
        """Sign a buffer, sends a raw bytes array"""
        # TODO: see "custom_nonce" optional item
        return self._key.sign(buffer)

    def sign_buffer_for_bis(self, buffer: bytes) -> str:
        """Sign a buffer, sends under the format expected by bismuth network format"""
        return b64encode(self.sign_buffer_raw(buffer)).decode('utf-8')
