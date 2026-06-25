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
    # the default window covers a small token, so the holders array is complete + not flagged truncated
    assert det["holders_returned"] == 1 and det["truncated"] is False and det["offset"] == 0


def test_token_detail_holders_paging_and_truncated_flag(ti):
    """holders is windowed by limit/offset with holder_count(total)/holders_returned/truncated, so a partial
    page is never mistaken for the whole set (the array used to be silently capped at the top 200)."""
    ti.register_issue(5, 1000, "tokx", "ADDR_A", "tx_issue", "1000000")
    for h, (to, amt) in enumerate([("ADDR_B", 100000), ("ADDR_C", 50000),
                                   ("ADDR_D", 25000), ("ADDR_E", 10000)], start=8):
        assert ti.apply_transfer(h, 2000 + h, "tokx", "ADDR_A", to, "tx_%d" % h, amt) is True
    # five holders (A keeps 815000); the default window holds them all -> not truncated, supply == full sum
    full = ti.token_detail("tokx")
    assert full["holder_count"] == 5 and full["holders_returned"] == 5 and full["truncated"] is False
    assert full["supply"] == 1000000 == sum(h["balance"] for h in full["holders"])
    assert full["holders"][0]["address"] == "ADDR_A"        # sorted by balance desc
    # a middle page: partial -> truncated True, but supply/holder_count stay the FULL-set aggregates
    page = ti.token_detail("tokx", limit=2, offset=1)
    assert page["holder_count"] == 5 and page["supply"] == 1000000
    assert page["holders_returned"] == 2 and page["offset"] == 1 and page["limit"] == 2
    assert page["truncated"] is True
    assert [h["address"] for h in page["holders"]] == [h["address"] for h in full["holders"][1:3]]
    # the last page consumes the remainder -> not truncated
    tail = ti.token_detail("tokx", limit=2, offset=4)
    assert tail["holders_returned"] == 1 and tail["truncated"] is False
    # limit is clamped to the 1000 hard cap (no crash / no unbounded response)
    assert ti.token_detail("tokx", limit=10 ** 9)["limit"] == 1000


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


# --- KV seam: byte-identity + backend swappability (doc/26 storage stage 1) -----------------------
import json as _json      # noqa: E402
import struct as _struct  # noqa: E402


def test_on_disk_bytes_identical_to_direct_lmdb(tmp_path):
    """After migrating onto kvstore.open_store, the LMDB env the store writes must be BYTE-IDENTICAL to what
    the pre-migration direct-lmdb store wrote: the same 11 named sub-dbs, the same raw key layout
    (token\\0party\\0HQ, big-endian heights), and JSON / decimal-string / b"" values unchanged (NOT routed
    through msgpack Codec). A running node must read its existing LMDB files unchanged."""
    import lmdb
    ti = ti_mod.TokenIndex(str(tmp_path / "tidx"), map_size=64 * 1024 * 1024)
    try:
        ti.register_issue(5, 1000, "tokx", "ADDR_A", "tx_issue", "1000000")
        ti.apply_transfer(8, 2000, "tokx", "ADDR_A", "ADDR_B", "tx_t1", 400000)
        ti.mark_noop(9, "tx_bad")
        ti.register_alias(3, "ALICE", "alice")
    finally:
        ti.close()

    # read the raw on-disk bytes back out of the env the store wrote, per named sub-db
    env = lmdb.open(str(tmp_path / "tidx"), subdir=True, max_dbs=12, readonly=True, lock=False)
    on_disk = {}
    for name in ti_mod._DBS:
        db = env.open_db(name.encode(), create=False)
        with env.begin() as t:
            on_disk[name] = {bytes(k): bytes(v) for k, v in t.cursor(db=db)}
    env.close()

    sep = ti_mod._SEP
    hq = lambda h, s: _struct.pack(">QQ", h, s)             # noqa: E731
    hbe = lambda h: _struct.pack(">Q", h)                   # noqa: E731

    # seq order: issue=1, transfer=2, noop=3, alias register=4
    assert on_disk["meta"] == {
        b"seq": b"4", b"tok_anchor": b"9", b"alias_anchor": b"3",
    }
    assert on_disk["tokreg"] == {
        b"tokx": _json.dumps({"h": 5, "ts": 1000, "issuer": "ADDR_A",
                              "txid": "tx_issue", "supply": 1000000}).encode(),
    }
    assert on_disk["seen"] == {b"tx_issue": b"", b"tx_t1": b"", b"tx_bad": b""}
    assert on_disk["cred"] == {
        b"tokx" + sep + b"ADDR_A" + sep + hq(5, 1): b"1000000",
        b"tokx" + sep + b"ADDR_B" + sep + hq(8, 2): b"400000",
    }
    assert on_disk["deb"] == {
        b"tokx" + sep + b"issued" + sep + hq(5, 1): b"1000000",
        b"tokx" + sep + b"ADDR_A" + sep + hq(8, 2): b"400000",
    }
    assert on_disk["addrtok"] == {
        b"ADDR_A" + sep + b"tokx": b"2", b"issued" + sep + b"tokx": b"1",
        b"ADDR_B" + sep + b"tokx": b"1",
    }
    assert on_disk["tokset"] == {b"tokx": b"2"}
    assert on_disk["journal"] == {
        hq(5, 1): _json.dumps({"k": "issue", "tok": "tokx", "rcp": "ADDR_A",
                               "adr": "issued", "amt": 1000000, "txid": "tx_issue"}).encode(),
        hq(8, 2): _json.dumps({"k": "transfer", "tok": "tokx", "rcp": "ADDR_B",
                               "adr": "ADDR_A", "amt": 400000, "txid": "tx_t1", "ts": 2000}).encode(),
        hq(9, 3): _json.dumps({"k": "noop", "txid": "tx_bad"}).encode(),
    }
    assert on_disk["alias_fwd"] == {
        b"alice": _json.dumps({"a": "ALICE", "h": 3}).encode(),
    }
    assert on_disk["alias_rev"] == {b"ALICE" + sep + hbe(3) + sep + b"alice": b""}
    assert on_disk["ajournal"] == {
        hq(3, 4): _json.dumps({"al": "alice", "a": "ALICE"}).encode(),
    }


def _drive(backend, path):
    """Identical token+alias workload against a TokenIndex on the given backend; return observable results
    across the full surface (issuance, overspend sums, holders/paging, reverse index, tx history, rollback,
    alias register/transfer/free + rollback)."""
    ti = ti_mod.TokenIndex(path, map_size=64 * 1024 * 1024, backend=backend)
    try:
        ti.register_issue(5, 1000, "tokx", "A", "tx_issue", "1000000")
        ti.apply_transfer(8, 2000, "tokx", "A", "B", "tx_t1", 400000)
        ti.apply_transfer(10, 3000, "tokx", "A", "C", "tx_c_in", 100)
        ti.mark_noop(9, "tx_bad")
        ti.register_alias(3, "ALICE", "alice")
        ti.register_alias(4, "ALICE", "al2")
        ti.register_alias(6, "BOB", "bob")
        ti.transfer_alias(7, "BOB", "CAROL", "bob")
        out = {
            "has_token": ti.has_token("tokx"),
            "has_txid": ti.has_txid("tx_t1"),
            "anchor": ti.token_anchor(),
            "alias_anchor": ti.alias_anchor(),
            "bal_A": ti.token_balance("tokx", "A"),
            "bal_B": ti.token_balance("tokx", "B"),
            "credit_C_same": ti.token_credit("tokx", "C", 10),
            "credit_C_later": ti.token_credit("tokx", "C", 11),
            "debit_A": ti.token_debit("tokx", "A", 10),
            "tokens_user_B": ti.tokens_user("B"),
            "detail": ti.token_detail("tokx"),
            "page": ti.token_detail("tokx", limit=2, offset=1),
            "list": ti.tokens_list(),
            "txs_A": ti.token_txs_for_address("A"),
            "transfers": ti.token_transfers("tokx"),
            "alias_owner_bob": ti.alias_owner("bob"),
            "addfromalias": ti.addfromalias("alice"),
            "aliasget_ALICE": ti.aliasget("ALICE"),
            "aliasesget": ti.aliasesget(["ALICE", "NOBODY"]),
            "stats": ti.stats(),
        }
        ti.tokens_rollback(9)
        ti.aliases_rollback(7)            # undo the bob->carol transfer
        out["after_tok_rollback_balA"] = ti.token_balance("tokx", "A")
        out["after_tok_rollback_balC"] = ti.token_balance("tokx", "C")
        out["after_tok_anchor"] = ti.token_anchor()
        out["after_has_txid_cin"] = ti.has_txid("tx_c_in")
        out["after_alias_owner_bob"] = ti.alias_owner("bob")   # restored to BOB
        out["after_alias_anchor"] = ti.alias_anchor()
        return out
    finally:
        ti.close()


@pytest.mark.parametrize("backend", ["lmdb", "sqlite-kv"])
def test_backend_swappable_smoke(backend, tmp_path):
    """Each backend independently produces the expected results through the open_store(backend=...) seam."""
    res = _drive(backend, str(tmp_path / ("tidx_" + backend)))
    assert res["bal_A"] == 599900 and res["bal_B"] == 400000
    assert res["credit_C_same"] == 0 and res["credit_C_later"] == 100
    assert res["detail"]["holder_count"] == 3 and res["detail"]["supply"] == 1000000
    assert res["transfers"] == 3
    assert res["after_tok_rollback_balA"] == 600000   # h=8 (<9) transfer survives, h=10 reopened
    assert res["after_has_txid_cin"] is False
    assert res["after_alias_owner_bob"] == "BOB"      # transfer at h=7 undone
    assert res["stats"] == {"tokens": 1, "aliases": 3, "tok_anchor": 10, "alias_anchor": 7}


def test_swappability_lmdb_equals_sqlite(tmp_path):
    """The SAME TokenIndex run on a 2nd engine (sqlite-kv) yields IDENTICAL observable results to lmdb —
    proving the KV seam is engine-independent for this store (incl. prefix iterate, height range, the JSON
    journal rollbacks and KVStore.stat). [doc/26 storage stage 1]"""
    res_lmdb = _drive("lmdb", str(tmp_path / "tidx_lmdb"))
    res_sqlite = _drive("sqlite-kv", str(tmp_path / "tidx_sqlite"))
    assert res_lmdb == res_sqlite, "TokenIndex results differ across backends -> seam not engine-independent"
