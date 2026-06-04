"""

"""

import base58
import hashlib
from polysign.signer import Signer, SignerType, SignerSubType
from typing import Union
from hashlib import sha256
from base64 import b64decode, b64encode
from coincurve import PrivateKey


class SignerCRW(Signer):

    __slots__ = ('_key', )

    _address_versions = {SignerSubType.MAINNET_REGULAR: b'\x01\x75\x07'}

    def __init__(self, private_key: Union[bytes, str]=b'', public_key: Union[bytes, str]=b'', address: str='',
                 compressed=False, subtype: SignerSubType=SignerSubType.MAINNET_REGULAR):
        super().__init__(private_key, public_key, address, compressed=compressed, subtype=subtype)
        self._key = None
        self._type = SignerType.ECDSA

    def from_private_key(self, private_key: Union[bytes, str], subtype: SignerSubType=SignerSubType.MAINNET_REGULAR):
        print('TODO')

    def from_full_info(self, private_key: Union[bytes, str], public_key: Union[bytes, str]=b'', address: str='',
                       subtype: SignerSubType = SignerSubType.MAINNET_REGULAR, verify: bool=True):
        print('TODO')

    def from_seed(self, seed: str='', subtype: SignerSubType = SignerSubType.MAINNET_REGULAR):
        print('crw from seed {}'.format(seed))
        if subtype != SignerSubType.MAINNET_REGULAR:
            self._subtype = subtype
        try:
            key = PrivateKey.from_hex(seed)
            public_key = key.public_key.format(compressed=False).hex()
            print("Public Key", public_key)
            self._key = key
            self._private_key = key.to_hex()  # == seed
            self._public_key = public_key
        except Exception as e:
            print("Exception {} reading RSA private key".format(e))
        print("identifier", self.identifier().hex())
        self._address = self.address()

    def identifier(self):
        """Returns double hash of pubkey"""
        # faafd1966c79c472360ef1cf8860169df6e7554a
        return hashlib.new('ripemd160', sha256(bytes.fromhex(self._public_key)).digest()).digest()

    def address(self):
        # 1PrWZ4CXSXWbg87XS9ShhwMV6TiSXtycT7
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
        raise ValueError("SignerCRW.verify_signature not impl.")

    @classmethod
    def verify_bis_signature(cls, signature: str, public_key: str, buffer: bytes, address: str = '') -> None:
        """Verify signature from bismuth tx network format (ecdsa sig and pubkey are b64 encoded)
        Returns None, but raises ValueError if needed."""
        public_key = b64decode(public_key).decode('utf-8')
        # print(public_key)

        """ TODO
        public_key_object = RSA.importKey(public_key_pem)
        signature_decoded = b64decode(signature)
        verifier = PKCS1_v1_5.new(public_key_object)
        sha_hash = SHA.new(buffer)
        if not verifier.verify(sha_hash, signature_decoded):
            raise ValueError(f"Invalid signature from {address}")
        """
        # Reconstruct address from pubkey to make sure it matches
        if address != cls.public_key_to_address(public_key):
            raise ValueError("Attempt to spend from a wrong address")

    def sign_buffer_raw(self, buffer: bytes) -> bytes:
        """Sign a buffer, sends a raw bytes array"""
        pass

    def sign_buffer_for_bis(self, buffer: bytes) -> str:
        """Sign a buffer, sends under the format expected by bismuth network format"""
        # sig is b64 encoded
        return b64encode(self.sign_buffer_raw(buffer))
