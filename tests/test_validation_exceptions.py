"""doc/30 — from-genesis validation exceptions.

Proves the waiver mechanism makes a from-genesis replay accept the specific
historical manual-intervention blocks (coin rescues / fork-edge edits) WITHOUT
loosening anything else, and is byte-identical (inert) by default.

Two layers, all hermetic (no running node, no ledger.db):
  * LOGIC — validation_exceptions.is_exempt / assume_valid_skip_signature / load.
  * WIRING — the real digest.BlockProcessor validation methods: each waiver
    suppresses EXACTLY its own raise (overspend / duplicate / signature), and
    only when registered; everything unregistered still raises.

Run: python3 -m pytest tests/test_validation_exceptions.py -v
"""
import json
import os
import sys
import time
import types
from decimal import Decimal

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import validation_exceptions as V
import digest
import digest_tx
import amounts

GEN = "4edadac9093d9326ee4b17f869b14f1a2534f96f9c5d7b48dc9acaed"   # mainnet genesis (valid RSA address)


# --------------------------------------------------------------------------- fakes ----
class _Log:
    def __init__(self):
        self.warnings = []
    def warning(self, m):
        self.warnings.append(m)
    def info(self, m):
        pass


def _node(**kw):
    n = types.SimpleNamespace()
    n.is_mainnet = kw.get("is_mainnet", False)
    n.is_testnet = kw.get("is_testnet", False)
    n.is_regnet = kw.get("is_regnet", True)
    n.fork_height = kw.get("fork_height", None)
    n.last_block = kw.get("last_block", 699999)
    n.last_block_timestamp = kw.get("last_block_timestamp", time.time() - 100)
    n.old_sqlite = kw.get("old_sqlite", False)
    n.balance_index_consensus = "off"
    n.balance_index = None
    n.txid_index = None
    n.parity_strict = False
    n.assume_valid_height = kw.get("assume_valid_height", 0)
    n.validation_exceptions = kw.get("validation_exceptions", None)
    n.logger = types.SimpleNamespace(app_log=_Log())
    return n


class _Cursor:
    def __init__(self):
        self._ret = None
    def fetchone(self):
        return self._ret


class _DB:
    """Minimal db_handler for check_duplicate_signatures. `dup_sig` (if set) makes
    the ledger-lookup report that signature as already present."""
    def __init__(self, dup_sig=None):
        self.h = _Cursor()
        self.c = _Cursor()
        self.dup_sig = dup_sig
    def execute_param(self, cursor, sql, params):
        cursor._ret = (5,) if (self.dup_sig and self.dup_sig in [str(p) for p in params]) else None


def _raw_tx(sig, amount="1.00000000", address=GEN, recipient=GEN, op="", of=""):
    return (time.time() - 100, address, recipient, amount, sig, "pubkey", op, of)


# ====================================================================== LOGIC ====
def test_inert_by_default_on_mainnet():
    """Empty registry + assume_valid off == byte-identical (nothing waived)."""
    n = _node(is_mainnet=True, is_regnet=False)
    assert V.MAINNET_EXCEPTIONS == {}
    assert V.is_exempt(n, 700000, V.OVERSPEND) is False
    assert V.assume_valid_skip_signature(n, 5) is False


def test_regnet_inert_without_override():
    n = _node(is_regnet=True, validation_exceptions=None)
    assert V.is_exempt(n, 700000, V.OVERSPEND) is False


def test_is_exempt_height_and_check_scoping():
    reg = {700000: {"checks": {V.OVERSPEND}, "reason": "edge", "signatures": None}}
    n = _node(validation_exceptions=reg)
    assert V.is_exempt(n, 700000, V.OVERSPEND) is True
    assert V.is_exempt(n, 700000, V.SIGNATURE) is False      # wrong check
    assert V.is_exempt(n, 700001, V.OVERSPEND) is False      # wrong height


def test_signature_waiver_scoped_to_prefix():
    reg = {812345: {"checks": {V.SIGNATURE}, "reason": "rescue", "signatures": {"abcd"}}}
    n = _node(validation_exceptions=reg)
    assert V.is_exempt(n, 812345, V.SIGNATURE, "abcd1234") is True
    assert V.is_exempt(n, 812345, V.SIGNATURE, "ffff0000") is False
    assert V.is_exempt(n, 812345, V.SIGNATURE, None) is False
    # an unscoped signature waiver matches any tx
    n2 = _node(validation_exceptions={1: {"checks": {V.SIGNATURE}, "reason": "x", "signatures": None}})
    assert V.is_exempt(n2, 1, V.SIGNATURE, "anything") is True


def test_assume_valid_threshold():
    n = _node(assume_valid_height=1000000)
    n.checkpoints = {1000000: "x"}                      # signature skip requires checkpoint anchoring
    assert V.assume_valid_skip_signature(n, 999999) is True
    assert V.assume_valid_skip_signature(n, 1000000) is True
    assert V.assume_valid_skip_signature(n, 1000001) is False
    n.assume_valid_height = 0
    assert V.assume_valid_skip_signature(n, 5) is False


def test_in_trusted_prefix():
    n = _node(assume_valid_height=4000000)
    n.checkpoints = {4000000: "x", 4800000: "y"}        # anchored
    assert V.in_trusted_prefix(n, 1) is True
    assert V.in_trusted_prefix(n, 4000000) is True
    assert V.in_trusted_prefix(n, 4000001) is False
    # off
    off = _node(assume_valid_height=0); off.checkpoints = {4000000: "x"}
    assert V.in_trusted_prefix(off, 1) is False
    # SAFETY: no checkpoints -> never skip (regnet/testnet with a configured horizon)
    bare = _node(assume_valid_height=4000000); bare.checkpoints = {}
    assert V.in_trusted_prefix(bare, 1) is False
    # SAFETY: horizon beyond the last checkpoint -> unanchored tail -> never skip
    past = _node(assume_valid_height=9999999); past.checkpoints = {4000000: "x"}
    assert V.in_trusted_prefix(past, 1) is False


def test_checkpoint_verify_match_mismatch_and_none():
    cp = {4000000: "6f4794262f38f5de41379fd860bb58332f1825c1c6e04885ca11db7f"}
    n = _node()
    n.checkpoints = cp
    assert V.checkpoint_hash(n, 4000000) == cp[4000000]
    assert V.verify_checkpoint(n, 4000000, cp[4000000]) is True
    assert V.verify_checkpoint(n, 3999999, "anything") is False        # not a checkpoint height
    with pytest.raises(ValueError, match="MISMATCH"):
        V.verify_checkpoint(n, 4000000, "deadbeef")


def test_mainnet_checkpoints_present_inert_elsewhere():
    m = _node(is_mainnet=True, is_regnet=False)
    assert 4000000 in V.MAINNET_CHECKPOINTS
    assert V.checkpoint_hash(m, 4000000) == V.MAINNET_CHECKPOINTS[4000000]
    # regnet/testnet have no mainnet checkpoints unless explicitly injected
    assert V.checkpoint_hash(_node(is_regnet=True), 4000000) is None


def test_trusted_prefix_skips_overspend():
    """In the trusted prefix an overspend does NOT raise (anchored by the checkpoint),
    even with no per-height waiver."""
    n = _node(last_block=699999, assume_valid_height=4000000)
    n.checkpoints = {4000000: "x"}
    proc = digest.BlockProcessor(n, db_handler=None, peer_ip="t")
    proc._validate_balance(GEN, "10.00000000", Decimal("10"), Decimal("0"), {GEN: Decimal("5.0")})  # no raise
    # but ABOVE the horizon it still raises
    n2 = _node(last_block=5000000, assume_valid_height=4000000)
    n2.checkpoints = {4000000: "x"}
    proc2 = digest.BlockProcessor(n2, db_handler=None, peer_ip="t")
    with pytest.raises(ValueError, match="more than spendable"):
        proc2._validate_balance(GEN, "10.00000000", Decimal("10"), Decimal("0"), {GEN: Decimal("5.0")})


def test_malformed_registry_fails_closed():
    n = _node(validation_exceptions={700000: "not-a-dict"})
    assert V.is_exempt(n, 700000, V.OVERSPEND) is False      # never crashes consensus


def test_load_external_file_merges_and_validates(tmp_path):
    path = str(tmp_path / "exc.json")
    json.dump({"700000": {"checks": ["overspend"], "reason": "edge", "signatures": None},
               "812345": {"checks": ["signature"], "reason": "rescue", "signatures": ["abcd"]}},
              open(path, "w"))
    n = _node(is_mainnet=True, is_regnet=False)
    n.validation_exceptions_file = path
    reg = V.load(n)
    assert reg[700000]["checks"] == {"overspend"}
    assert reg[812345]["signatures"] == {"abcd"}
    # unknown check is rejected loudly
    bad = str(tmp_path / "bad.json")
    json.dump({"1": {"checks": ["nonsense"]}}, open(bad, "w"))
    n.validation_exceptions_file = bad
    with pytest.raises(ValueError):
        V.load(n)
    # no file -> None (fall back to built-in mainnet gate, behaviour unchanged)
    n.validation_exceptions_file = ""
    assert V.load(n) is None


# ================================================================ WIRING: overspend ====
def test_overspend_raises_without_waiver():
    n = _node(last_block=699999)
    proc = digest.BlockProcessor(n, db_handler=None, peer_ip="t")
    balances = {GEN: Decimal("5.0")}                      # cached -> no DB needed
    with pytest.raises(ValueError, match="more than spendable"):
        proc._validate_balance(GEN, "10.00000000", Decimal("10"), Decimal("0"), balances)


def test_overspend_waived_at_registered_height():
    reg = {700000: {"checks": {V.OVERSPEND}, "reason": "coin rescue", "signatures": None}}
    n = _node(last_block=699999, validation_exceptions=reg)   # block being validated == 700000
    proc = digest.BlockProcessor(n, db_handler=None, peer_ip="t")
    balances = {GEN: Decimal("5.0")}
    proc._validate_balance(GEN, "10.00000000", Decimal("10"), Decimal("0"), balances)  # no raise
    assert any("overspend" in w for w in n.logger.app_log.warnings)


def test_overspend_not_waived_at_other_height():
    reg = {700000: {"checks": {V.OVERSPEND}, "reason": "x", "signatures": None}}
    n = _node(last_block=700000, validation_exceptions=reg)   # block 700001 -> not registered
    proc = digest.BlockProcessor(n, db_handler=None, peer_ip="t")
    with pytest.raises(ValueError):
        proc._validate_balance(GEN, "10.00000000", Decimal("10"), Decimal("0"), {GEN: Decimal("5.0")})


# ================================================================ WIRING: duplicate ====
def _block_instance(height, tx_count):
    return types.SimpleNamespace(block_height_new=height, tx_count=tx_count)


def test_in_block_duplicate_raises_without_waiver():
    n = _node()
    proc = digest.BlockProcessor(n, db_handler=_DB(), peer_ip="t")
    block = [_raw_tx("samesig"), _raw_tx("samesig")]          # two identical signatures
    bi = _block_instance(700000, 2)
    with pytest.raises(ValueError, match="duplicate transactions in this block"):
        proc.check_duplicate_signatures(block, bi)


def test_in_block_duplicate_waived():
    reg = {700000: {"checks": {V.DUPLICATE}, "reason": "dup rescue", "signatures": None}}
    n = _node(validation_exceptions=reg)
    proc = digest.BlockProcessor(n, db_handler=_DB(), peer_ip="t")
    block = [_raw_tx("samesig"), _raw_tx("samesig")]
    proc.check_duplicate_signatures(block, _block_instance(700000, 2))   # no raise
    assert any("duplicate" in w for w in n.logger.app_log.warnings)


def test_already_in_ledger_raises_without_waiver():
    n = _node()
    proc = digest.BlockProcessor(n, db_handler=_DB(dup_sig="DUPSIG"), peer_ip="t")
    block = [_raw_tx("DUPSIG"), _raw_tx("other")]
    with pytest.raises(ValueError, match="already in ledger"):
        proc.check_duplicate_signatures(block, _block_instance(700000, 2))


def test_already_in_ledger_waived_scoped_to_signature():
    reg = {700000: {"checks": {V.DUPLICATE}, "reason": "replay rescue", "signatures": {"DUPSIG"}}}
    n = _node(validation_exceptions=reg)
    proc = digest.BlockProcessor(n, db_handler=_DB(dup_sig="DUPSIG"), peer_ip="t")
    block = [_raw_tx("DUPSIG"), _raw_tx("other")]
    proc.check_duplicate_signatures(block, _block_instance(700000, 2))   # no raise


# ================================================================ WIRING: signature ====
def _patch_verify(monkeypatch):
    """verify_tx_signature that rejects only signatures starting with BADSIG."""
    from polysign.signerfactory import SignerFactory
    def fake(post_fork, ts, addr, rec, amt, op, of, sig, pub, *a, **k):  # *a/**k: tolerate registry= kwarg
        if str(sig).startswith("BADSIG"):
            raise ValueError("bad signature (test)")
        return True
    monkeypatch.setattr(SignerFactory, "verify_tx_signature", staticmethod(fake))


def _sig_block():
    # tx0 = regular tx with a bad signature; tx1 = coinbase (zero amount, RSA addr, good sig)
    return [_raw_tx("BADSIG_rescue_tx", amount="1.00000000"),
            _raw_tx("GOODSIG_coinbase", amount="0.00000000", of="nonce")]


def test_bad_signature_raises_without_waiver(monkeypatch):
    _patch_verify(monkeypatch)
    n = _node(last_block=699999)
    proc = digest.BlockProcessor(n, db_handler=None, peer_ip="t")
    bi = digest_tx.Block(n); bi.tx_count = 2
    with pytest.raises(ValueError, match="bad signature"):
        proc.sort_and_validate_transactions(_sig_block(), bi)


def test_bad_signature_waived_scoped(monkeypatch):
    _patch_verify(monkeypatch)
    reg = {700000: {"checks": {V.SIGNATURE}, "reason": "lost-key rescue", "signatures": {"BADSIG"}}}
    n = _node(last_block=699999, validation_exceptions=reg)
    proc = digest.BlockProcessor(n, db_handler=None, peer_ip="t")
    bi = digest_tx.Block(n); bi.tx_count = 2
    proc.sort_and_validate_transactions(_sig_block(), bi)               # no raise
    assert any("signature" in w for w in n.logger.app_log.warnings)


def test_assume_valid_skips_signature_check(monkeypatch):
    """Below assume_valid_height the bad signature is never even verified."""
    called = {"n": 0}
    from polysign.signerfactory import SignerFactory
    def fake(*a, **k):
        called["n"] += 1
        raise ValueError("should not be called")
    monkeypatch.setattr(SignerFactory, "verify_tx_signature", staticmethod(fake))
    n = _node(last_block=699999, assume_valid_height=1000000)
    n.checkpoints = {1000000: "x"}                                      # anchors the trusted prefix
    proc = digest.BlockProcessor(n, db_handler=None, peer_ip="t")
    bi = digest_tx.Block(n); bi.tx_count = 2
    proc.sort_and_validate_transactions(_sig_block(), bi)               # no raise
    assert called["n"] == 0                                             # signature verify skipped entirely
