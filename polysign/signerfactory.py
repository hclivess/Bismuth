import re
from os import urandom
from typing import Type, Union

from polysign.signer import Signer, SignerType, SignerSubType
from polysign.signer_rsa import SignerRSA  # RSA = Bismuth mainnet scheme; depends only on pycryptodomex

# Non-RSA signers depend on optional native crypto (coincurve, ed25519/pynacl). They are imported
# lazily so an RSA-only node (Bismuth mainnet) has no hard dependency on those packages.
# Reintegrated into the core tree from the external `polysign` package; see doc/14.
_OPTIONAL_SIGNERS = {
    "SignerECDSA": ("polysign.signer_ecdsa", "SignerECDSA"),
    "SignerED25519": ("polysign.signer_ed25519", "SignerED25519"),
    "SignerBTC": ("polysign.signer_btc", "SignerBTC"),
    "SignerCRW": ("polysign.signer_crw", "SignerCRW"),
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


def signer_for_type(signer_type: SignerType) -> Union[Type[Signer], None]:
    """Returns the class matching a signer type."""
    if signer_type == SignerType.RSA:
        return SignerRSA
    optional = {SignerType.ED25519: "SignerED25519", SignerType.ECDSA: "SignerECDSA",
                SignerType.BTC: "SignerBTC", SignerType.CRW: "SignerCRW"}
    name = optional.get(signer_type)
    return _load_optional_signer(name) if name else None


class SignerFactory:
    """"""

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
        pass

    @classmethod
    def address_to_signer(cls, address: str) -> Type[Signer]:
        if RE_RSA_ADDRESS.match(address):
            return SignerRSA
        elif RE_ECDSA_ADDRESS.match(address):
            if len(address) > 50:
                return _load_optional_signer("SignerED25519")
            else:
                return _load_optional_signer("SignerECDSA")

        raise ValueError("Unsupported Address type")

    @classmethod
    def address_is_valid(cls, address: str) -> bool:
        if RE_RSA_ADDRESS.match(address):
            # RSA, 56 hex
            return True
        elif RE_ECDSA_ADDRESS.match(address):
            if 50 < len(address) < 60:
                # ED25519, around 54
                return True
            if 30 < len(address) < 50:
                # ecdsa, around 37
                return True
        return False

    @classmethod
    def address_is_rsa(cls, address: str) -> bool:
        """Returns whether the given address is a legacy RSA one"""
        return RE_RSA_ADDRESS.match(address) is not None

    @classmethod
    def from_seed(cls, seed: str='', signer_type: SignerType=SignerType.RSA,
                  subtype: SignerSubType=SignerSubType.MAINNET_REGULAR) -> Signer:
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
