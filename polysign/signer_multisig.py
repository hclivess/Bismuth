"""Native M-of-N multisig signer for Bismuth (secp256k1 / coincurve).

A BASE-LAYER (consensus) multisig: a dedicated address TYPE whose spends carry M signatures over an
N-pubkey "redeem script", verified by the node's own signature path. This is the on-chain analogue of
Bitcoin's P2SH multisig (the address commits to ``hash160(redeem)``; a spend reveals the redeem + the M
signatures), and the post-fork counterpart to the VM-custody vault in ``contracts/multisig.py`` (which
needs no consensus change). It reuses the ``MAINNET_MULTISIG`` / ``TESTNET_MULTISIG`` address versions
already reserved in :mod:`polysign.signer` and :class:`~polysign.signer_ecdsa.SignerECDSA`.

Wire format — everything rides the EXISTING 8 transaction fields (no new tx fields, no format change):
  * address    = base58check( version_multisig || hash160(redeem) ), version chosen by subtype.
  * redeem     = bytes([M, N]) || pub_1(33) || ... || pub_N(33), the pubkeys SORTED (BIP67).
  * public_key = base64(redeem)                                       (the tx ``public_key`` field)
  * signature  = base64( bytes([k]) || (idx, len, der_sig) * k ), idx STRICTLY INCREASING, k >= M
                                                                      (the tx ``signature`` field)

Verification (:meth:`verify_bis_signature`): parse the redeem -> rebuild and match the sender address;
parse the signature list -> require at least M valid DER signatures over the canonical tx buffer, each
from a DISTINCT owner pubkey (strictly increasing index, which also canonicalises the ordering), every
provided signature valid. Pure crypto — the post-fork TIMING gate (multisig senders are only valid
after hf2 activates) lives in the digester (``digest.py``), exactly like the shielded / VM gates.

HONEST CONSTRAINT: Bismuth truncates the tx ``signature`` field at 684 chars and ``public_key`` at 1068
(the frozen base tx format — see ``digest_tx.py``). A 71-byte DER signature plus framing means the
THRESHOLD M fits up to ~6 provided signatures; N (owners) fits up to ``MAX_OWNERS`` in the redeem.
``serialize_redeem`` / ``MultisigAccount.assemble_signature`` enforce both ceilings precisely rather
than letting an over-long field be silently truncated (which would make a valid spend unverifiable).
"""

import hashlib
from base64 import b64decode, b64encode
from hashlib import sha256
from typing import List, Tuple, Union

import base58
from coincurve import PublicKey, verify_signature

from polysign.signer import Signer, SignerType, SignerSubType


class SignerMultisig(Signer):

    __slots__ = ()

    # MUST match SignerECDSA._address_versions for the multisig subtypes — the address space is shared.
    _address_versions = {SignerSubType.MAINNET_MULTISIG: b'\x4f\x54\xc8',
                         SignerSubType.TESTNET_MULTISIG: b'\x01\x46\xeb\xa5'}

    MAX_OWNERS = 15              # N ceiling (Bitcoin-style); redeem stays well under the 1068-char field
    SIG_FIELD_MAX = 684         # tx signature field truncation (digest_tx.py) — consensus-frozen
    PUBKEY_FIELD_MAX = 1068     # tx public_key field truncation — consensus-frozen

    def __init__(self, private_key: Union[bytes, str] = b'', public_key: Union[bytes, str] = b'',
                 address: str = '', compressed: bool = True,
                 subtype: SignerSubType = SignerSubType.MAINNET_MULTISIG):
        super().__init__(private_key, public_key, address, compressed=compressed, subtype=subtype)
        self._type = SignerType.ECDSA    # the component signatures are plain secp256k1 ECDSA

    # ---- redeem script: the (M, N, sorted pubkeys) the address commits to ----------------------------
    @staticmethod
    def sort_pubkeys(pubkeys: List[bytes]) -> List[bytes]:
        """BIP67 deterministic ordering: lexicographic on the compressed pubkey bytes. Makes the address
        a pure function of the owner SET + threshold, independent of the order owners were listed in."""
        return sorted(pubkeys)

    @classmethod
    def serialize_redeem(cls, m: int, pubkeys: List[bytes]) -> bytes:
        """(M, [compressed pubkeys]) -> canonical redeem bytes. Validates M/N bounds, that each pubkey is
        a real 33-byte compressed secp256k1 point, and rejects duplicates."""
        n = len(pubkeys)
        if not isinstance(m, int) or not (1 <= m <= n <= cls.MAX_OWNERS):
            raise ValueError("require 1 <= M <= N <= %d (got M=%r N=%d)" % (cls.MAX_OWNERS, m, n))
        seen = set()
        for pk in pubkeys:
            if not isinstance(pk, (bytes, bytearray)) or len(pk) != 33 or pk[0] not in (2, 3):
                raise ValueError("each owner pubkey must be a 33-byte compressed secp256k1 key")
            PublicKey(bytes(pk))                       # must be a real curve point
            if bytes(pk) in seen:
                raise ValueError("duplicate owner pubkey")
            seen.add(bytes(pk))
        ordered = cls.sort_pubkeys([bytes(pk) for pk in pubkeys])
        return bytes([m, n]) + b"".join(ordered)

    @classmethod
    def parse_redeem(cls, redeem: bytes) -> Tuple[int, int, List[bytes]]:
        """Redeem bytes -> (M, N, [pubkeys]). Strict: enforces length, valid points, distinctness, and
        CANONICAL (BIP67-sorted) ordering — so a given address has exactly one accepted redeem."""
        if len(redeem) < 2:
            raise ValueError("redeem too short")
        m, n = redeem[0], redeem[1]
        if not (1 <= m <= n <= cls.MAX_OWNERS):
            raise ValueError("bad M/N in redeem (M=%d N=%d)" % (m, n))
        body = redeem[2:]
        if len(body) != 33 * n:
            raise ValueError("redeem length mismatch")
        pubkeys = [body[i * 33:(i + 1) * 33] for i in range(n)]
        for pk in pubkeys:
            if pk[0] not in (2, 3):
                raise ValueError("non-compressed pubkey in redeem")
            PublicKey(pk)                              # reject an off-curve owner key outright
        if len(set(pubkeys)) != n:
            raise ValueError("duplicate pubkey in redeem")
        if pubkeys != cls.sort_pubkeys(pubkeys):
            raise ValueError("redeem pubkeys not canonically sorted (BIP67)")
        return m, n, pubkeys

    # ---- address (P2SH-style: version || hash160(redeem)) --------------------------------------------
    @classmethod
    def redeem_to_address(cls, redeem: bytes,
                          subtype: SignerSubType = SignerSubType.MAINNET_MULTISIG) -> str:
        h160 = hashlib.new('ripemd160', sha256(redeem).digest()).digest()
        vh = cls.address_version_for_subtype(subtype) + h160
        chk = sha256(sha256(vh).digest()).digest()[:4]
        return base58.b58encode(vh + chk).decode('utf-8')

    @classmethod
    def address_for(cls, m: int, pubkeys: List[bytes],
                    subtype: SignerSubType = SignerSubType.MAINNET_MULTISIG) -> str:
        """Convenience: the multisig address for an (M, pubkeys) set."""
        return cls.redeem_to_address(cls.serialize_redeem(m, pubkeys), subtype)

    # ---- signature list codec ------------------------------------------------------------------------
    @classmethod
    def serialize_sigs(cls, entries: List[Tuple[int, bytes]]) -> bytes:
        """[(owner_index, der_sig)] -> blob. Sorts by index (canonical) and requires distinct indices."""
        entries = sorted(entries, key=lambda e: e[0])
        if len({idx for idx, _ in entries}) != len(entries):
            raise ValueError("duplicate owner index in signature set")
        if len(entries) > 255:
            raise ValueError("too many signatures")
        out = bytearray([len(entries)])
        for idx, sig in entries:
            if not (0 <= idx <= 255):
                raise ValueError("owner index out of range")
            if not (1 <= len(sig) <= 255):
                raise ValueError("signature length out of range")
            out += bytes([idx, len(sig)]) + sig
        return bytes(out)

    @classmethod
    def parse_sigs(cls, blob: bytes) -> List[Tuple[int, bytes]]:
        if not blob:
            raise ValueError("empty signature blob")
        k = blob[0]
        off = 1
        entries = []
        for _ in range(k):
            if off + 2 > len(blob):
                raise ValueError("truncated signature header")
            idx, ln = blob[off], blob[off + 1]
            off += 2
            if ln == 0 or off + ln > len(blob):
                raise ValueError("truncated signature body")
            entries.append((idx, blob[off:off + ln]))
            off += ln
        if off != len(blob):
            raise ValueError("trailing bytes in signature blob")
        return entries

    # ---- the consensus verifier ----------------------------------------------------------------------
    @classmethod
    def verify_bis_signature(cls, signature: str, public_key: str, buffer: bytes, address: str = '') -> None:
        """Verify a Bismuth multisig spend. Raises ValueError on ANY failure (the node's reject path).

        1. public_key field -> redeem -> (M, N, owners); rebuild the address and require it == sender.
        2. signature field  -> [(idx, der)]; require k >= M, indices strictly increasing and < N, EVERY
           provided signature valid over ``buffer`` under owners[idx]. Threshold met iff valid count >= M.
        """
        redeem = b64decode(public_key)
        m, n, pubkeys = cls.parse_redeem(redeem)
        # rebuild the address with the SAME network subtype it was minted with (mainnet vs testnet)
        subtype = cls.subtype_for_address(address, default=SignerSubType.MAINNET_MULTISIG)
        if cls.redeem_to_address(redeem, subtype) != address:
            raise ValueError("multisig redeem does not match the spending address")
        entries = cls.parse_sigs(b64decode(signature))
        if len(entries) < m:
            raise ValueError("multisig needs >= %d signatures, got %d" % (m, len(entries)))
        if len(entries) > n:
            raise ValueError("more signatures (%d) than owners (%d)" % (len(entries), n))
        last = -1
        valid = 0
        for idx, sig in entries:
            if idx <= last:
                raise ValueError("multisig signature indices must be strictly increasing (distinct owners)")
            last = idx
            if idx >= n:
                raise ValueError("multisig signature owner index out of range")
            if not verify_signature(sig, buffer, pubkeys[idx]):
                raise ValueError("invalid multisig component signature from owner %d" % idx)
            valid += 1
        if valid < m:
            raise ValueError("multisig threshold not met (%d/%d)" % (valid, m))

    @classmethod
    def verify_bis_signature_raw(cls, signature: bytes, public_key: bytes, buffer: bytes, address: str = '') -> None:
        cls.verify_bis_signature(b64encode(signature).decode(), b64encode(public_key).decode(), buffer, address)

    # ---- abstract surface that does not apply to a multi-key construct -------------------------------
    @classmethod
    def public_key_to_address(cls, public_key: Union[bytes, str],
                              subtype: SignerSubType = SignerSubType.MAINNET_MULTISIG) -> str:
        """A multisig address is hash160 of the REDEEM, not of a single pubkey — use ``redeem_to_address``
        (the ``public_key`` argument here is the redeem bytes, accepted for factory-call symmetry)."""
        redeem = bytes.fromhex(public_key) if isinstance(public_key, str) else public_key
        return cls.redeem_to_address(redeem, subtype)

    @classmethod
    def verify_signature(cls, signature, public_key, buffer, address=''):
        raise NotImplementedError("use verify_bis_signature for multisig")

    def from_private_key(self, private_key, subtype: SignerSubType = SignerSubType.MAINNET_MULTISIG):
        raise NotImplementedError("multisig is a multi-key construct; build it with multisig_wallet")

    def from_full_info(self, private_key, public_key=b'', address='',
                       subtype: SignerSubType = SignerSubType.MAINNET_MULTISIG, verify: bool = True):
        raise NotImplementedError("multisig is a multi-key construct; build it with multisig_wallet")

    def from_seed(self, seed: str = '', subtype: SignerSubType = SignerSubType.MAINNET_MULTISIG):
        raise NotImplementedError("multisig is a multi-key construct; build it with multisig_wallet")

    def sign_buffer_raw(self, buffer: bytes) -> bytes:
        raise NotImplementedError("multisig signing is per-owner; see multisig_wallet.MultisigAccount")

    def sign_buffer_for_bis(self, buffer: bytes) -> str:
        raise NotImplementedError("multisig signing is per-owner; see multisig_wallet.MultisigAccount")
