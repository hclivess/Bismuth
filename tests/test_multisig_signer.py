"""Native M-of-N multisig signer (polysign/signer_multisig.py + multisig_wallet.py) — doc/23 §2.

A BASE-LAYER multisig address type: spends carry M signatures over an N-pubkey redeem, verified by the
node's own signature path (post-fork gated). These tests deliberately target the consensus hazards a
native multisig must get right — signature ORDERING, THRESHOLD rules, REPLAY/tamper, and SERIALIZATION —
driving the verifier exactly as the node does (SignerFactory.verify_bis_signature over the canonical
buffer). A LIVE regnet test funds a 2-of-3 multisig and spends from it with two owners, post-fork.

Run with: python3 -m pytest tests/test_multisig_signer.py -v
"""
import itertools
import json
import time
import urllib.request
from base64 import b64decode, b64encode

import pytest
import coincurve as cc

import bismuth_serialize
import hd_wallet
from multisig_wallet import MultisigAccount, pubkey_of
from polysign.signer import SignerSubType as ST
from polysign.signer_multisig import SignerMultisig as MS
from polysign.signerfactory import SignerFactory

API = "http://127.0.0.1:3031"
cc_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141   # secp256k1 group order


def _owners(n):
    return [cc.PrivateKey() for _ in range(n)]


def _buf(addr, recipient="Bis1recipientxxxxxxxxxxxxxxxxx", amount=1.25, op="", of=""):
    ts = "%.2f" % 1700000000.0
    return ts, bismuth_serialize.signature_buffer(ts, addr, recipient, "%.8f" % amount, op, of)


# ----------------------------------------------------------------------------- offline (no node)
def test_address_routes_and_is_valid():
    acct = MultisigAccount.from_owners(_owners(3), 2)
    assert acct.address.startswith("Bism")
    assert SignerFactory.address_to_signer(acct.address) is MS
    assert SignerFactory.address_is_multisig(acct.address)
    assert SignerFactory.address_is_valid(acct.address)
    # a regular ECDSA address is NOT mistaken for multisig
    reg = hd_wallet.HDWallet(b"\x07" * 32).receive_address(0)
    assert reg.startswith("Bis1") and not SignerFactory.address_is_multisig(reg)


def test_address_independent_of_owner_order():
    os_ = _owners(3)
    a = MultisigAccount.from_owners(os_, 2).address
    b = MultisigAccount.from_owners(list(reversed(os_)), 2).address
    assert a == b, "BIP67: address must depend on the owner SET, not the listing order"
    # but the threshold is part of the address
    assert MultisigAccount.from_owners(os_, 2).address != MultisigAccount.from_owners(os_, 3).address


def test_every_quorum_verifies():
    os_ = _owners(3)
    acct = MultisigAccount.from_owners(os_, 2)
    ts, buf = _buf(acct.address)
    for pair in itertools.combinations(os_, 2):           # all C(3,2) quorums
        tx = acct.sign_transaction(list(pair), ts, "Bis1recipientxxxxxxxxxxxxxxxxx", 1.25)
        SignerFactory.verify_bis_signature(tx[4], tx[5], buf, tx[1])
    # 3-of-3 (all owners) also satisfies a 2-of-3
    tx = acct.sign_transaction(os_, ts, "Bis1recipientxxxxxxxxxxxxxxxxx", 1.25)
    SignerFactory.verify_bis_signature(tx[4], tx[5], buf, tx[1])


def test_threshold_not_met_rejected():
    os_ = _owners(3)
    acct = MultisigAccount.from_owners(os_, 2)
    ts, buf = _buf(acct.address)
    # build a 1-signature blob by hand (bypassing the build-time self-verify) -> verifier must reject
    idx, sig = acct.partial_sign(buf, os_[0])
    one = b64encode(MS.serialize_sigs([(idx, sig)])).decode()
    with pytest.raises(ValueError):
        MS.verify_bis_signature(one, acct.public_key_field(), buf, acct.address)


def test_non_owner_signature_rejected():
    os_ = _owners(3)
    acct = MultisigAccount.from_owners(os_, 2)
    ts, buf = _buf(acct.address)
    attacker = cc.PrivateKey()
    # one real owner sig + one attacker sig claiming a free owner slot
    owner_idx, owner_sig = acct.partial_sign(buf, os_[0])
    free = next(i for i in range(3) if i != owner_idx)
    blob = b64encode(MS.serialize_sigs([(owner_idx, owner_sig), (free, attacker.sign(buf))])).decode()
    with pytest.raises(ValueError):
        MS.verify_bis_signature(blob, acct.public_key_field(), buf, acct.address)


def test_one_owner_cannot_fill_two_slots():
    os_ = _owners(3)
    acct = MultisigAccount.from_owners(os_, 2)
    ts, buf = _buf(acct.address)
    idx, sig = acct.partial_sign(buf, os_[0])
    # same owner's signature presented under two indices: the duplicate-index guard rejects the blob,
    # and even a (idx, idx) pair fails the strictly-increasing rule -> a lone owner can't reach M=2.
    with pytest.raises(ValueError):
        MS.serialize_sigs([(idx, sig), (idx, sig)])


def test_signature_indices_must_be_strictly_increasing():
    os_ = _owners(3)
    acct = MultisigAccount.from_owners(os_, 2)
    ts, buf = _buf(acct.address)
    i0, s0 = acct.partial_sign(buf, os_[0])
    i1, s1 = acct.partial_sign(buf, os_[1])
    hi, lo = max(i0, i1), min(i0, i1)
    shi = s0 if i0 == hi else s1
    slo = s0 if i0 == lo else s1
    # hand-assemble OUT OF ORDER (descending) -> canonical-ordering rule rejects (anti-malleability)
    blob = bytearray([2]) + bytes([hi, len(shi)]) + shi + bytes([lo, len(slo)]) + slo
    with pytest.raises(ValueError):
        MS.verify_bis_signature(b64encode(bytes(blob)).decode(), acct.public_key_field(), buf, acct.address)


def test_replay_on_different_message_rejected():
    os_ = _owners(3)
    acct = MultisigAccount.from_owners(os_, 2)
    ts, _ = _buf(acct.address)
    tx = acct.sign_transaction(os_[:2], ts, "Bis1recipientxxxxxxxxxxxxxxxxx", 1.25)
    # take a VALID signature set but verify it against a DIFFERENT buffer (changed recipient/amount)
    _, other = _buf(acct.address, recipient="Bis1someoneelsexxxxxxxxxxxxxxx", amount=9.99)
    with pytest.raises(ValueError):
        MS.verify_bis_signature(tx[4], tx[5], other, tx[1])


def test_redeem_must_match_address():
    os_ = _owners(3)
    acct = MultisigAccount.from_owners(os_, 2)
    ts, buf = _buf(acct.address)
    tx = acct.sign_transaction(os_[:2], ts, "Bis1recipientxxxxxxxxxxxxxxxxx", 1.25)
    # present a DIFFERENT redeem (different owner set) for the same claimed address -> mismatch reject
    other = MultisigAccount.from_owners(_owners(3), 2)
    with pytest.raises(ValueError):
        MS.verify_bis_signature(tx[4], other.public_key_field(), buf, acct.address)


def test_redeem_must_be_canonically_sorted():
    os_ = _owners(3)
    pubs = sorted(pubkey_of(o) for o in os_)
    good = MS.serialize_redeem(2, pubs)
    MS.parse_redeem(good)                                   # canonical -> ok
    unsorted = bytes([2, 3]) + b"".join(reversed(pubs))     # same set, wrong order
    with pytest.raises(ValueError):
        MS.parse_redeem(unsorted)


def test_build_validates_params():
    os_ = _owners(3)
    with pytest.raises(ValueError):
        MultisigAccount.from_owners(os_, 0)                 # threshold < 1
    with pytest.raises(ValueError):
        MultisigAccount.from_owners(os_, 4)                 # threshold > N
    with pytest.raises(ValueError):
        MS.serialize_redeem(1, [pubkey_of(os_[0]), pubkey_of(os_[0])])   # duplicate owner
    with pytest.raises(ValueError):
        MS.serialize_redeem(1, [b"\x02" + b"\x00" * 32])    # not a curve point
    with pytest.raises(ValueError):
        MS.serialize_redeem(1, _owners(MS.MAX_OWNERS + 1))  # too many owners (pubkeys, will fail type)


def test_signature_field_size_is_bounded():
    """The tx signature field truncates at 684 chars (frozen). A threshold whose assembled signatures
    would overflow MUST fail loudly at build time, not be silently truncated into an unverifiable spend."""
    os_ = _owners(10)
    acct = MultisigAccount.from_owners(os_, 8)              # 8 DER sigs (~71B each) overflow 684 b64 chars
    ts, _ = _buf(acct.address)
    with pytest.raises(ValueError, match="signature"):
        acct.sign_transaction(os_[:8], ts, "Bis1recipientxxxxxxxxxxxxxxxxx", 1.0)
    # a modest threshold fits comfortably
    small = MultisigAccount.from_owners(os_[:5], 3)
    tx = small.sign_transaction(os_[:3], ts, "Bis1recipientxxxxxxxxxxxxxxxxx", 1.0)
    assert len(tx[4]) <= MS.SIG_FIELD_MAX and len(tx[5]) <= MS.PUBKEY_FIELD_MAX


def test_sig_blob_serialize_roundtrip_and_corruption():
    os_ = _owners(3)
    acct = MultisigAccount.from_owners(os_, 2)
    _, buf = _buf(acct.address)
    parts = [acct.partial_sign(buf, os_[0]), acct.partial_sign(buf, os_[1])]
    blob = MS.serialize_sigs(parts)
    assert MS.parse_sigs(blob) == sorted(parts, key=lambda e: e[0])
    with pytest.raises(ValueError):
        MS.parse_sigs(blob + b"\x00")                       # trailing bytes
    with pytest.raises(ValueError):
        MS.parse_sigs(blob[:-3])                            # truncated body


def test_signature_malleability_low_s_rejected():
    """ECDSA signatures are malleable (s and N−s both satisfy the equation) — the classic txid-malleability
    class. libsecp256k1 enforces low-s on VERIFY (BIP146), so a high-s malleated component signature must be
    rejected, keeping a multisig spend non-malleable at the signature level."""
    os_ = _owners(3)
    acct = MultisigAccount.from_owners(os_, 2)
    ts, buf = _buf(acct.address)
    tx = acct.sign_transaction(os_[:2], ts, "Bis1recipientxxxxxxxxxxxxxxxxx", 1.25)
    bufv = bismuth_serialize.signature_buffer(tx[0], tx[1], tx[2], tx[3], tx[6], tx[7])
    SignerFactory.verify_bis_signature(tx[4], tx[5], bufv, tx[1])     # baseline verifies
    entries = MS.parse_sigs(b64decode(tx[4]))

    def flip_s(der):                                  # DER: 30 L 02 rl r 02 sl s  ->  s := N−s
        assert der[0] == 0x30 and der[2] == 0x02
        rl = der[3]
        r = der[4:4 + rl]
        j = 4 + rl
        assert der[j] == 0x02
        sl = der[j + 1]
        s = int.from_bytes(der[j + 2:j + 2 + sl], "big")
        nb = (cc_N - s).to_bytes(32, "big").lstrip(b"\x00")
        if nb[0] & 0x80:
            nb = b"\x00" + nb
        body = b"\x02" + bytes([len(r)]) + r + b"\x02" + bytes([len(nb)]) + nb
        return b"\x30" + bytes([len(body)]) + body

    mal = b64encode(MS.serialize_sigs([(idx, flip_s(sig)) for idx, sig in entries])).decode()
    with pytest.raises(ValueError):
        MS.verify_bis_signature(mal, tx[5], bufv, tx[1])


def test_hd_owners_compose_with_multisig():
    """Owners can be HD-derived (hd_wallet) — a multisig vault over deterministic-seed keys."""
    w1, w2, w3 = (hd_wallet.HDWallet(bytes([i]) * 32) for i in (1, 2, 3))
    owners = [w1.node_at(0), w2.node_at(0), w3.node_at(0)]
    acct = MultisigAccount.from_owners(owners, 2)
    ts = "%.2f" % 1700000000.0
    tx = acct.sign_transaction([owners[2], owners[0]], ts, "Bis1recipientxxxxxxxxxxxxxxxxx", 2.0)
    buf = bismuth_serialize.signature_buffer(tx[0], tx[1], tx[2], tx[3], tx[6], tx[7])
    SignerFactory.verify_bis_signature(tx[4], tx[5], buf, tx[1])


# ----------------------------------------------------------------------------- live regnet
def _get(path):
    with urllib.request.urlopen(API + path, timeout=8) as r:
        return json.load(r)


def _mine_until(client, predicate, rounds=12):
    for _ in range(rounds):
        if predicate():
            return
        client.mine(2)
        time.sleep(0.2)
    assert predicate(), "condition not reached after mining"


def _wait_fork_active(client):
    for _ in range(30):
        d = _get("/api/shield/stats")
        fh = d.get("fork_height")
        if fh is not None and client.block_height() > fh:
            return
        client.mine(3)
    raise AssertionError("hf2 not active")


def test_native_multisig_spend_live(client):
    _wait_fork_active(client)
    # a 2-of-3 vault over HD-derived owners
    wallets = [hd_wallet.HDWallet(bytes([0xA0 + i]) * 32) for i in range(3)]
    owners = [w.node_at(0) for w in wallets]
    acct = MultisigAccount.from_owners(owners, 2)

    # fund the multisig address from the RSA wallet (receiving INTO multisig is allowed any time)
    client.send(acct.address, 6, "", "")
    _mine_until(client, lambda: client.balance(acct.address) >= 6)
    assert abs(client.balance(acct.address) - 6) < 1e-6, "multisig vault not funded"

    # spend 4 BIS out of the vault, signed by owners 0 and 2 (2-of-3), to a fresh HD address
    dest = wallets[0].node_at(1).address()
    ts = "%.2f" % time.time()
    tx = acct.sign_transaction([owners[0], owners[2]], ts, dest, 4, "", "msig-spend")
    client.send_raw_tx(tx)
    _mine_until(client, lambda: client.balance(dest) >= 4)
    assert abs(client.balance(dest) - 4) < 1e-6, "multisig 2-of-3 spend did not pay the recipient"
    assert client.balance(acct.address) < 2.001, "vault not debited (amount + fee)"


def test_under_threshold_spend_rejected_live(client):
    _wait_fork_active(client)
    wallets = [hd_wallet.HDWallet(bytes([0xB0 + i]) * 32) for i in range(3)]
    owners = [w.node_at(0) for w in wallets]
    acct = MultisigAccount.from_owners(owners, 2)

    client.send(acct.address, 5, "", "")
    _mine_until(client, lambda: client.balance(acct.address) >= 5)
    bal = client.balance(acct.address)

    # hand-build a 1-of-2 (under threshold) spend, bypassing the build-time self-verify
    ts = "%.2f" % time.time()
    buf = bismuth_serialize.signature_buffer(ts, acct.address, owners[1].address(), "%.8f" % 3.0, "", "")
    idx, sig = acct.partial_sign(buf, owners[0])
    bad_sig = b64encode(MS.serialize_sigs([(idx, sig)])).decode()
    tx = (ts, acct.address, owners[1].address(), "%.8f" % 3.0, bad_sig, acct.public_key_field(), "", "")
    try:
        client.send_raw_tx(tx)            # node should reject at mempool admission (verify fails)
    except Exception:
        pass
    client.mine(2)
    time.sleep(0.3)
    assert abs(client.balance(acct.address) - bal) < 1e-6, "under-threshold multisig spend was accepted!"
