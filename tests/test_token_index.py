"""
Node-free unit tests for the LMDB token/alias side-index (``token_index.TokenIndex``, doc/26 stage 2).

These pin the behaviour that must stay byte-identical to the retired SQLite ``index.db`` projection:
token issuance/registry, the EXACT overspend rule (credit at block_height < h, debit at <= h), holders /
supply / reverse-index queries, first-claimant-wins aliases, and height-keyed reorg rollback for both.

They build a tiny on-disk LMDB store and drive the real module, so they need no running node — only the
``lmdb`` dependency (skipped if absent).
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

pytest.importorskip("lmdb")
import token_index as ti_mod


@pytest.fixture
def ti(tmp_path):
    store = ti_mod.TokenIndex(str(tmp_path / "tidx"), map_size=64 * 1024 * 1024)
    yield store
    store.close()


# --- token issuance -------------------------------------------------------------------------------
def test_issue_credits_issuer_full_supply(ti):
    assert ti.register_issue(5, 1000, "tokx", "ADDR_A", "tx_issue", "1000000") is True
    assert ti.has_token("tokx")
    assert ti.token_balance("tokx", "ADDR_A") == 1000000
    assert ti.token_anchor() == 5
    # first name wins: a second issuance of the same name is a no-op
    assert ti.register_issue(6, 1001, "tokx", "ADDR_B", "tx_issue2", "999") is False
    assert ti.token_balance("tokx", "ADDR_B") == 0


def test_issue_detail_and_listing(ti):
    ti.register_issue(5, 1000, "tokx", "ADDR_A", "tx_issue", "1000000")
    det = ti.token_detail("tokx")
    assert det["supply"] == 1000000
    assert det["holder_count"] == 1
    assert det["holders"][0] == {"address": "ADDR_A", "balance": 1000000}
    assert ti.tokens_list()[0] == ("tokx", 1)          # one row so far (the issuance)
    assert ti.token_detail("nope") is None             # unknown -> None (REST maps to 404)


# --- transfers + the overspend rule ---------------------------------------------------------------
def test_valid_transfer_moves_balance(ti):
    ti.register_issue(5, 1000, "tokx", "ADDR_A", "tx_issue", "1000000")
    assert ti.apply_transfer(8, 2000, "tokx", "ADDR_A", "ADDR_B", "tx_t1", 400000) is True
    assert ti.token_balance("tokx", "ADDR_A") == 600000
    assert ti.token_balance("tokx", "ADDR_B") == 400000
    # the reverse index lists the token for both parties
    assert ti.tokens_user("ADDR_B") == [("tokx",)]
    assert ti.token_detail("tokx")["transfers"] == 2   # issue + one transfer


def test_overspend_check_credit_lt_h_debit_le_h(ti):
    """The legacy asymmetry: you cannot re-spend tokens received in the SAME block (credit uses < h)."""
    ti.register_issue(5, 1000, "tokx", "ADDR_A", "tx_issue", "1000000")
    # C receives 100 at h=10
    ti.apply_transfer(10, 3000, "tokx", "ADDR_A", "ADDR_C", "tx_c_in", 100)
    # C tries to spend at the SAME height: credit(<10) excludes the just-received 100 -> balance 0
    assert ti.token_credit("tokx", "ADDR_C", 10) == 0
    # at a LATER height the receipt counts
    assert ti.token_credit("tokx", "ADDR_C", 11) == 100
    # debit uses <= h
    assert ti.token_debit("tokx", "ADDR_A", 10) == 100 + 0  # the h=10 send out of A counts at <= 10


def test_txid_dedup_is_idempotent(ti):
    ti.register_issue(5, 1000, "tokx", "ADDR_A", "tx_issue", "1000000")
    assert ti.apply_transfer(8, 2000, "tokx", "ADDR_A", "ADDR_B", "tx_t1", 400000) is True
    # replaying the same txid (catch-up rescan) is a no-op
    assert ti.apply_transfer(8, 2000, "tokx", "ADDR_A", "ADDR_B", "tx_t1", 400000) is False
    assert ti.token_balance("tokx", "ADDR_B") == 400000
    # a no-op (invalid transfer) also dedups
    assert ti.mark_noop(9, "tx_bad") is True
    assert ti.has_txid("tx_bad")
    assert ti.mark_noop(9, "tx_bad") is False


# --- token rollback (reorg) ----------------------------------------------------------------------
def test_token_rollback_reopens_and_restores(ti):
    ti.register_issue(5, 1000, "tokx", "ADDR_A", "tx_issue", "1000000")
    ti.apply_transfer(8, 2000, "tokx", "ADDR_A", "ADDR_B", "tx_t1", 400000)
    ti.apply_transfer(10, 3000, "tokx", "ADDR_A", "ADDR_C", "tx_c_in", 100)
    ti.mark_noop(9, "tx_bad")

    ti.tokens_rollback(9)                               # drop everything at height >= 9
    assert not ti.has_txid("tx_bad")                    # the h=9 no-op reopened
    assert not ti.has_txid("tx_c_in")                   # the h=10 transfer reopened
    assert ti.token_balance("tokx", "ADDR_C") == 0
    assert ti.tokens_user("ADDR_C") == []
    assert ti.token_balance("tokx", "ADDR_A") == 600000  # the h=8 transfer (< 9) survives
    assert ti.token_balance("tokx", "ADDR_B") == 400000
    assert ti.token_anchor() == 8
    assert ti.has_token("tokx")                          # issued at h=5 < 9, still registered

    ti.tokens_rollback(5)                                # drop the issuance too
    assert not ti.has_token("tokx")
    assert ti.token_balance("tokx", "ADDR_A") == 0
    assert ti.token_detail("tokx") is None
    assert ti.token_anchor() == 4


# --- aliases (first claimant wins) ---------------------------------------------------------------
def test_alias_first_claimant_wins_and_reads(ti):
    assert ti.register_alias(3, "ALICE", "alice") is True
    assert ti.register_alias(7, "MALLORY", "alice") is False   # later claim on the same name loses
    assert ti.register_alias(4, "ALICE", "al2") is True        # an address may hold several aliases
    assert ti.addfromalias("alice") == "ALICE"
    assert ti.addfromalias("ghost") == "No alias"
    assert ti.aliasget("ALICE") == [["alice"], ["al2"]]        # all, in registration order
    assert ti.aliasget("NOBODY") == [["NOBODY"]]               # SQLite shape when none
    assert ti.aliasesget(["ALICE", "NOBODY"]) == ["alice", "NOBODY"]  # first per address, else itself
    assert ti.alias_anchor() == 7


def test_alias_rollback(ti):
    ti.register_alias(3, "ALICE", "alice")
    ti.register_alias(4, "ALICE", "al2")
    ti.aliases_rollback(4)                               # drop al2 (h=4), keep alice (h=3)
    assert ti.aliasget("ALICE") == [["alice"]]
    assert ti.addfromalias("al2") == "No alias"
    assert ti.alias_anchor() == 3
    ti.aliases_rollback(3)
    assert ti.addfromalias("alice") == "No alias"
    assert ti.alias_anchor() == 2
