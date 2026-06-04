"""
Abstract and Factory class to handle various Bismuth signature and addresses schemes
"""

import json

from abc import ABC, abstractmethod
from enum import Enum
from typing import Union


class SignerType(Enum):
    """
    Possible signing schemes
    """
    NONE = 0
    RSA = 1
    ECDSA = 2
    ED25519 = 3
    BTC = 1000  # Tests
    CRW = 1001  # Tests


class SignerSubType(Enum):
    """
    Possible addresses subtype
    """
    NONE = 0
    MAINNET_REGULAR = 1
    MAINNET_MULTISIG = 2
    TESTNET_REGULAR = 3
    TESTNET_MULTISIG = 4


class Signer(ABC):

    # Slots allow to spare ram when there can be several instances
    __slot__ = ('_private_key', '_public_key', '_address', '_type', '_subtype', '_compressed', 'verbose')

    _address_versions = {SignerSubType.MAINNET_REGULAR: b'\x00'}

    def __init__(self, private_key: Union[bytes, str]=b'', public_key: Union[bytes, str]=b'', address: str='',
                 compressed: bool=True, subtype: SignerSubType=SignerSubType.MAINNET_REGULAR):
        self._private_key = private_key
        self._public_key = public_key
        self._address = address
        self._type = SignerType.NONE
        self._subtype = subtype
        self.verbose = False
        self._compressed = compressed

    @property
    def compressed(self):
        return self._compressed

    @property
    def type(self):
        """Name of the signer instance"""
        return self._type.name

    @abstractmethod
    def from_private_key(self, private_key: Union[bytes, str], subtype: SignerSubType=SignerSubType.MAINNET_REGULAR):
        pass

    @abstractmethod
    def from_full_info(self, private_key: Union[bytes, str], public_key: Union[bytes, str]=b'', address: str='',
                       subtype: SignerSubType = SignerSubType.MAINNET_REGULAR, verify: bool=True):
        pass

    @abstractmethod
    def from_seed(self, seed: str='', subtype: SignerSubType=SignerSubType.MAINNET_REGULAR):
        """Use seed == '' to generate a random key"""
        pass

    @classmethod
    @abstractmethod
    def public_key_to_address(cls, public_key: Union[bytes, str],
                              subtype: SignerSubType=SignerSubType.MAINNET_REGULAR) -> str:
        """Reconstruct an address from the public key"""
        pass

    @classmethod
    def address_version_for_subtype(cls, subtype: SignerSubType) -> bytes:
        # Specific one if exists, else mainnet regular, else \x00
        return cls._address_versions.get(subtype, cls._address_versions.get(subtype.MAINNET_REGULAR, b'\x00'))

    @classmethod
    @abstractmethod
    def verify_signature(cls, signature: Union[bytes, str], public_key: Union[bytes, str], buffer: bytes,
                         address: str=''):
        """Verify signature from raw signature & pubkey. Address may be used to determine the sig type"""
        pass

    @classmethod
    @abstractmethod
    def verify_bis_signature(cls, signature: str, public_key: str, buffer: bytes, address: str='') -> None:
        """Verify signature from bismuth tx network format
        pubkey is b64 encoded twice - ecdsa and ed25519 are b64 encoded)"""
        pass

    @classmethod
    def verify_bis_signature_raw(cls, signature: bytes, public_key: bytes, buffer: bytes, address: str='') -> None:
        """Verify signature from bismuth tx network format
        pubkey is b64 encoded twice - ecdsa and ed25519 are b64 encoded)"""
        print("Abstract Method verify_bis_signature_raw")

    @abstractmethod
    def sign_buffer_raw(self, buffer: bytes) -> bytes:
        """Sign a buffer, sends a raw bytes array"""
        pass

    @abstractmethod
    def sign_buffer_for_bis(self, buffer: bytes) -> str:
        """Sign a buffer, sends under the format expected by bismuth network format"""
        pass

    def to_dict(self):
        """Returns core properties as dict, compact bin form"""
        info = {'address': self._address, 'private_key': self._private_key, 'public_key': self._public_key,
                'compressed': self._compressed, 'type': self._type.name, 'sub_type': self._subtype.name}
        return info

    def to_json(self):
        """Returns a json string, with bin items as hex strings"""
        info = self.to_dict()
        info['private_key'] = info['private_key'].hex()
        info['public_key'] = info['public_key'].hex()
        return json.dumps(info)
