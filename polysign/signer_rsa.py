
import json
import re
from base64 import b64encode, b64decode
from hashlib import sha224
from typing import Union

from Cryptodome.Hash import SHA
from Cryptodome.PublicKey import RSA
from Cryptodome.Signature import PKCS1_v1_5

from polysign.signer import Signer, SignerType, SignerSubType

# Compile once and for all
PEM_BEGIN = re.compile(r"\s*-----BEGIN (.*)-----\s+")
PEM_END = re.compile(r"-----END (.*)-----\s*$")


class SignerRSA(Signer):

    __slots__ = ('_key', )

    def __init__(self, private_key: Union[bytes, str]=b'', public_key: Union[bytes, str]=b'', address: str='',
                 compressed: bool = True, subtype: SignerSubType=SignerSubType.MAINNET_REGULAR):
        super().__init__(private_key, public_key, address, compressed=compressed, subtype=subtype)
        # RSA does not have compressed format
        self._type = SignerType.RSA
        # For the Key object
        self._key = None

    @classmethod
    def validate_pem(cls, pem_data: str) -> None:
        """ Validate PEM data
        returns None, raise on error.
        """
        # verify pem as cryptodome does
        match = PEM_BEGIN.match(pem_data)
        if not match:
            raise ValueError("Not a valid PEM pre boundary")
        marker = match.group(1)
        match = PEM_END.search(pem_data)
        if not match or match.group(1) != marker:
            raise ValueError("Not a valid PEM post boundary")
            # verify pem as cryptodome does

    @classmethod
    def public_key_to_address(cls, public_key: Union[bytes, str],
                              subtype: SignerSubType=SignerSubType.MAINNET_REGULAR) -> str:
        """Reconstruct an address from the public key"""
        if type(public_key) != str:
            # But union annotation kept for common interface sake.
            raise ValueError("RSA pubkey are str, pem format")
        return sha224(public_key.encode('utf-8')).hexdigest()

    @classmethod
    def public_key_bin_to_address(cls, public_key: bytes, subtype: SignerSubType=SignerSubType.MAINNET_REGULAR) -> str:
        """Reconstruct an address from the public key"""
        if type(public_key) != bytes:
            # But union annotation kept for common interface sake.
            raise ValueError("public_key_bin_to_address expect bytes, pem format")
        return sha224(public_key).hexdigest()

    def to_json(self) -> str:
        """for RSA, keys are stored as PEM format, not binary"""
        info = self.to_dict()
        return json.dumps(info)

    def from_private_key(self, private_key: Union[bytes, str],
                         subtype: SignerSubType=SignerSubType.MAINNET_REGULAR) -> None:
        if type(private_key) is not str:
            raise RuntimeError('RSA private key have to be strings')
        if subtype != SignerSubType.MAINNET_REGULAR:
            self._subtype = subtype
        try:
            key = RSA.importKey(private_key)
            public_key_readable = key.publickey().exportKey().decode("utf-8")
            if len(public_key_readable) not in (271, 799):
                raise ValueError("Invalid public key length: {}".format(len(public_key_readable)))
            address = sha224(public_key_readable.encode('utf-8')).hexdigest()
            # If we had no error, we can store
            self._key = key
            self._private_key = private_key
            self._public_key = public_key_readable
            self._address = address
        except Exception as e:
            print("Exception {} reading RSA private key".format(e))

    def from_seed(self, seed: str='', subtype: SignerSubType=SignerSubType.MAINNET_REGULAR) -> None:
        """
        if subtype != SignerSubType.MAINNET_REGULAR:
            self._subtype = subtype
        """
        raise ValueError("SignerRsa.from_seed not impl. - seed {}".format(seed))

    def from_full_info(self, private_key: Union[bytes, str], public_key: Union[bytes, str]=b'', address: str='',
                       subtype: SignerSubType = SignerSubType.MAINNET_REGULAR, verify: bool=True):
        raise ValueError("SignerRsa.from_full_info not impl.")

    @classmethod
    def verify_signature(cls, signature: Union[bytes, str], public_key: Union[bytes, str], buffer: bytes,
                         address: str='') -> None:
        """Verify signature from raw signature. Address may be used to determine the sig type"""
        raise ValueError("SignerRsa.verify_signature not impl.")

    @classmethod
    def verify_bis_signature(cls, signature: str, public_key: str, buffer: bytes, address: str = '') -> None:
        """Verify signature from bismuth tx network format (rsa sig is b64 encoded twice)
        Returns None, but raises ValueError if needed."""
        public_key_pem = b64decode(public_key).decode('utf-8')
        # Will raise if does not match
        cls.validate_pem(public_key_pem)
        public_key_object = RSA.importKey(public_key_pem)
        signature_decoded = b64decode(signature)
        verifier = PKCS1_v1_5.new(public_key_object)
        sha_hash = SHA.new(buffer)
        if not verifier.verify(sha_hash, signature_decoded):
            raise ValueError(f"Invalid signature from {address}")
        # Reconstruct address from pubkey to make sure it matches
        if address != cls.public_key_to_address(public_key_pem):
            raise ValueError("Attempt to spend from a wrong address")

    @classmethod
    def normalize_key(cls, s: str) -> str:
        """Re add boundaries and segment by 64 chars wide."""
        chunks = [s[i:i + 64] for i in range(0, len(s), 64)]
        chunks.insert(0, "-----BEGIN PUBLIC KEY-----")
        chunks.append("-----END PUBLIC KEY-----")
        return "\n".join(chunks)

    @classmethod
    def verify_bis_signature_raw(cls, signature: bytes, public_key: bytes, buffer: bytes, address: str = '') -> None:
        """Verify signature from bismuth tx bin stored format (b64 decoded)
        Returns None, but raises ValueError if needed."""
        # Will raise if does not match
        # TODO: Probably be better to use DER (bin) format there for rsa, instead of re-encode to PEM
        # However, if we want to check address... we need the PEM, see below.
        # move to ed25519 would free significant ressources.
        # pem = "-----BEGIN PUBLIC KEY-----\n" + b64encode(public_key).decode() + "\n-----END PUBLIC KEY-----"
        pem = cls.normalize_key(b64encode(public_key).decode())
        cls.validate_pem(pem)
        public_key_object = RSA.importKey(pem)
        verifier = PKCS1_v1_5.new(public_key_object)
        sha_hash = SHA.new(buffer)
        if not verifier.verify(sha_hash, signature):
            raise ValueError(f"Invalid signature from {address}")
        # Reconstruct address from pubkey to make sure it matches
        # print(address, pem, cls.public_key_to_address(pem))
        if address != cls.public_key_to_address(pem):
            raise ValueError("Attempt to spend from a wrong address")

    def sign_buffer_raw(self, buffer: bytes) -> bytes:
        """Sign a buffer, returns a raw bytes array"""
        h = SHA.new(buffer)
        signer = PKCS1_v1_5.new(self._key)
        return signer.sign(h)

    def sign_buffer_for_bis(self, buffer: bytes) -> str:
        """Sign a buffer, sends under the format expected by bismuth network format"""
        # For RSA, sig is b64 encoded
        return b64encode(self.sign_buffer_raw(buffer))
