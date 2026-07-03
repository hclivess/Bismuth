"""multisig_wallet.py — build and co-sign native M-of-N multisig spends (polysign SignerMultisig).

Wallet-side companion to :mod:`polysign.signer_multisig`. Turns a set of secp256k1 ECDSA owner keys + a
threshold into a Bismuth multisig address, and co-signs a spend from it into a complete 8-field tx tuple
ready for ``mpinsert``. Owners can be anything that yields a secp256k1 private key — a ``coincurve``
``PrivateKey``, a hex/bytes secret, a :class:`~polysign.signer_ecdsa.SignerECDSA`, or an HD-derived
``hd_wallet.HDNode`` — so a multisig vault composes directly with the HD wallet (each owner derives its
key from its own seed).

This is purely wallet-side: the chain only ever sees the redeem (in ``public_key``) and the M signatures
(in ``signature``); how each owner key was generated is invisible to consensus.
"""
from base64 import b64encode
from typing import List, Sequence

import coincurve as cc

import bismuth_serialize
from polysign.signer import SignerSubType
from polysign.signer_multisig import SignerMultisig
from polysign.signerfactory import SignerFactory


def _to_privkey(owner) -> cc.PrivateKey:
    """Normalise an owner key handle to a coincurve PrivateKey."""
    if isinstance(owner, cc.PrivateKey):
        return owner
    if isinstance(owner, (bytes, bytearray)):
        return cc.PrivateKey(bytes(owner))
    if isinstance(owner, str):
        return cc.PrivateKey(bytes.fromhex(owner))
    # hd_wallet.HDNode exposes the 32-byte scalar as .key
    key = getattr(owner, "key", None)
    if isinstance(key, (bytes, bytearray)) and len(key) == 32:
        return cc.PrivateKey(bytes(key))
    # SignerECDSA exposes _private_key as a hex string
    pk = getattr(owner, "_private_key", None)
    if isinstance(pk, str) and pk:
        return cc.PrivateKey(bytes.fromhex(pk))
    if isinstance(pk, (bytes, bytearray)) and len(pk) == 32:
        return cc.PrivateKey(bytes(pk))
    raise TypeError("unsupported owner key type: %r" % type(owner))


def pubkey_of(owner) -> bytes:
    """The 33-byte compressed secp256k1 pubkey for an owner key handle."""
    return _to_privkey(owner).public_key.format(compressed=True)


class MultisigAccount:
    """An M-of-N multisig account: the owner pubkey set + threshold, the derived redeem and address, and
    the co-signing helpers. The address is a pure function of the pubkey SET and M (BIP67 ordering)."""

    def __init__(self, owner_pubkeys: Sequence[bytes], threshold: int,
                 subtype: SignerSubType = SignerSubType.MAINNET_MULTISIG):
        self.redeem = SignerMultisig.serialize_redeem(threshold, list(owner_pubkeys))
        self.m, self.n, self.pubkeys = SignerMultisig.parse_redeem(self.redeem)  # canonical (sorted)
        self.subtype = subtype
        self.address = SignerMultisig.redeem_to_address(self.redeem, subtype)

    @classmethod
    def from_owners(cls, owners: Sequence, threshold: int,
                    subtype: SignerSubType = SignerSubType.MAINNET_MULTISIG) -> "MultisigAccount":
        """Build from owner key HANDLES (private keys / signers / HD nodes) rather than raw pubkeys."""
        return cls([pubkey_of(o) for o in owners], threshold, subtype)

    def public_key_field(self) -> str:
        """The base64 redeem for the tx ``public_key`` field; rejects an over-long redeem up front."""
        s = b64encode(self.redeem).decode()
        if len(s) > SignerMultisig.PUBKEY_FIELD_MAX:
            raise ValueError("redeem b64 %d > %d-char public_key field; fewer owners"
                             % (len(s), SignerMultisig.PUBKEY_FIELD_MAX))
        return s

    def index_of(self, owner) -> int:
        """The canonical (sorted) position of an owner's pubkey in the redeem."""
        return self.pubkeys.index(pubkey_of(owner))

    def partial_sign(self, buffer: bytes, owner):
        """One owner's contribution: (canonical_owner_index, DER signature over ``buffer``)."""
        key = _to_privkey(owner)
        return self.index_of(key), key.sign(buffer)

    def assemble_signature(self, partials) -> str:
        """[(idx, der)] -> the base64 ``signature`` field; rejects a blob that would overflow (and thus be
        silently truncated to an unverifiable spend) so the failure is loud and at build time."""
        s = b64encode(SignerMultisig.serialize_sigs(list(partials))).decode()
        if len(s) > SignerMultisig.SIG_FIELD_MAX:
            raise ValueError("assembled multisig signature %d > %d-char tx field; lower the threshold "
                             "(DER sigs are ~71 bytes, so M up to ~6 fits)"
                             % (len(s), SignerMultisig.SIG_FIELD_MAX))
        return s

    def sign_transaction(self, signing_owners: Sequence, timestamp, recipient, amount,
                         operation: str = "", openfield: str = "") -> tuple:
        """Build + co-sign a spend FROM this multisig address. ``signing_owners`` must include at least M
        owners. The consensus verifier requires the CANONICAL form of EXACTLY M components (over-signing is
        malleable — see SignerMultisig.verify_bis_signature), so if more than M owners are supplied we keep
        the M lowest-indexed components and drop the rest. Self-verifies through the real SignerFactory
        before returning the 8-field tx tuple."""
        amount_s = "%.8f" % float(amount)
        buffer = bismuth_serialize.signature_buffer(str(timestamp), self.address, str(recipient),
                                                    amount_s, str(operation), str(openfield))
        partials = [self.partial_sign(buffer, o) for o in signing_owners]
        if len(partials) > self.m:                       # trim to the canonical exactly-M form
            partials = sorted(partials, key=lambda p: p[0])[:self.m]
        sig_field = self.assemble_signature(partials)
        pub_field = self.public_key_field()
        SignerFactory.verify_bis_signature(sig_field, pub_field, buffer, self.address)  # belt-and-suspenders
        return (str(timestamp), self.address, str(recipient), amount_s,
                sig_field, pub_field, str(operation), str(openfield))
