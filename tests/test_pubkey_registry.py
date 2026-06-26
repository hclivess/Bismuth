"""hf2 pubkey-by-reference: registry store mechanics + the verify-path resolution/reject branch (doc/40).

Pure/unit — no node/network. The full positive resolve-and-verify with real RSA/multisig crypto is covered
by the regnet multinode test; here we lock the consensus-critical mechanics: first-appearance determinism,
height rollback, rebuild==apply, and the two new reject rules.

Run with: python3 -m pytest tests/test_pubkey_registry.py -v
"""
import sqlite3

import pytest

pytest.importorskip("lmdb")

import pubkey_registry
from pubkey_registry import PubkeyRegistry
from polysign.signerfactory import SignerFactory


def _row(h, addr, pub):
    # 12-field converted row: block_height, ts, address, recipient, amount, sig, public_key, ...
    return [h, "%.2f" % (1e9 + h), addr, "recip", "1.00000000", "sig", pub, "bh", "0", "0", "", ""]


def test_register_first_appearance_and_skips(tmp_path):
    r = PubkeyRegistry(str(tmp_path / "pk"))
    try:
        rows = [
            _row(10, "rsaA", "KEY_A1"),          # first use of rsaA -> registered
            _row(10, "rsaA", "KEY_A2"),          # same addr again -> ignored (first-appearance)
            _row(10, "rsaB", ""),                # by-reference (empty) -> nothing to register
            _row(-1, "mirror", "KEYX"),          # negative-height mirror -> skipped
            _row(0, "coinbase", "KEYC"),         # height 0 -> skipped
        ]
        assert r.register_from_block(rows, fork_height=5) == 1
        assert r.get("rsaA") == b"KEY_A1"        # first key kept, verbatim
        assert r.get("rsaB") is None
        assert r.get("mirror") is None
        assert r.first_seen("rsaA") == 10
        # pre-fork (fork_height None) registers nothing
        assert PubkeyRegistry(str(tmp_path / "pk2")).register_from_block(rows, None) == 0
    finally:
        r.close()


def test_rollback_by_height(tmp_path):
    r = PubkeyRegistry(str(tmp_path / "pk"))
    try:
        r.register_from_block([_row(10, "a", "KA")], 5)
        r.register_from_block([_row(20, "b", "KB")], 5)
        assert r.rollback(15) == 1                # only b (first_seen 20 > 15) removed
        assert r.get("a") == b"KA" and r.get("b") is None
    finally:
        r.close()


def test_rebuild_from_cursor_matches_apply_and_is_deterministic(tmp_path):
    # in-memory ledger: two addresses, each appearing twice; first-appearance must win deterministically
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE transactions (block_height INT, address TEXT, public_key TEXT)")
    conn.executemany("INSERT INTO transactions VALUES (?,?,?)", [
        (10, "a", "KA1"), (11, "a", "KA2"), (11, "b", "KB1"), (12, "b", "KB2"), (3, "old", "PRE")])
    conn.commit()

    r = PubkeyRegistry(str(tmp_path / "pk"))
    try:
        n = r.rebuild_from_cursor(conn.cursor(), fork_height=5)
        assert n == 2                            # a, b (old at height 3 < fork skipped)
        assert r.get("a") == b"KA1" and r.get("b") == b"KB1"   # first appearance per address
        assert r.get("old") is None
        # rebuild is idempotent / deterministic
        r.rebuild_from_cursor(conn.cursor(), 5)
        assert r.get("a") == b"KA1" and r.count() == 2
    finally:
        r.close()
        conn.close()


def test_verify_reject_no_registry_and_unregistered(tmp_path):
    rsa_addr = "ab" * 28                          # 56-hex -> RSA family (not single-sig)
    # post-fork, empty pubkey, no registry -> reject
    with pytest.raises(ValueError, match="no registry available"):
        SignerFactory.verify_tx_signature(True, "1.0", rsa_addr, "r", "1.00000000", "", "",
                                          "sig", "", registry=None)
    # post-fork, empty pubkey, registry has no entry -> "referencing unregistered address"
    r = PubkeyRegistry(str(tmp_path / "pk"))
    try:
        with pytest.raises(ValueError, match="unregistered address"):
            SignerFactory.verify_tx_signature(True, "1.0", rsa_addr, "r", "1.00000000", "", "",
                                              "sig", "", registry=r)
    finally:
        r.close()


def test_verify_resolves_registered_key_into_verifier(tmp_path, monkeypatch):
    """A registered address with an empty wire pubkey resolves to the registered key, which is handed to
    the per-scheme verifier (whose address-rebuild check is the real binding — stubbed here to capture it)."""
    rsa_addr = "ab" * 28
    r = PubkeyRegistry(str(tmp_path / "pk"))
    try:
        r.register_from_block([_row(10, rsa_addr, "RESOLVED_PEM")], 5)
        captured = {}
        monkeypatch.setattr(SignerFactory, "verify_bis_signature",
                            staticmethod(lambda sig, pub, buf, addr: captured.update(pub=pub, addr=addr)))
        SignerFactory.verify_tx_signature(True, "1.0", rsa_addr, "r", "1.00000000", "", "",
                                          "sig", "", registry=r)
        assert captured["pub"] == "RESOLVED_PEM"   # the omitted key was resolved from the registry
        assert captured["addr"] == rsa_addr
    finally:
        r.close()
