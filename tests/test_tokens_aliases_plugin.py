"""
Node-free tests for the modern tokens_aliases PLUGIN (doc/27) — it owns the LMDB token/alias side-index
and reacts to the block lifecycle. These drive the plugin object directly (no running node), with a real
``PluginContext`` over a tiny temp SQLite ledger for the backfill path.

Pins: enable-gating, ``on_block`` projection (issue→transfer→alias, two-pass), the ``token_index`` service
handle, the plugin's own REST routes (/api/tokens, /api/token/<name>), and historical ``backfill``.
"""
import os
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

pytest.importorskip("lmdb")

import importlib.machinery
import importlib.util

import plugin_base


def _load_plugin():
    spec = importlib.machinery.PathFinder().find_spec("__init__", [os.path.join(ROOT, "plugins/tokens_aliases")])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.TokensAliasesPlugin()


class _Log:
    def warning(self, *a, **k): pass
    def info(self, *a, **k): pass
    def error(self, *a, **k): pass


class _Cfg:
    def __init__(self, token_index=True):
        self.token_index = token_index


def _ctx(tmp_path, token_index=True):
    ledger = str(tmp_path / "ledger.db")
    # a minimal ledger so PluginContext.scan_ledger_operations (backfill) has a table to read
    conn = sqlite3.connect(ledger)
    conn.execute("CREATE TABLE transactions (block_height INTEGER, timestamp, address, recipient, amount, "
                 "signature, public_key, block_hash, fee, reward, operation, openfield)")
    conn.commit(); conn.close()
    return plugin_base.PluginContext(logger=_Log(), data_dir=str(tmp_path / "pdata"),
                                     ledger_path=ledger, config=_Cfg(token_index), is_regnet=True)


def _row(bh, addr, recip, sig, op, openfield, reward=0):
    """A 12-field block_transactions row: bh, ts, addr, recip, amt, sig, pubkey, bhash, fee, reward, op, openfield."""
    return [bh, 1000 + bh, addr, recip, "0", sig, "pk", "bhash", "0", reward, op, openfield]


def test_disabled_is_inert(tmp_path):
    p = _load_plugin()
    p.setup(_ctx(tmp_path, token_index=False))
    assert p.ti is None
    assert p.services() == {}
    assert p.peer_commands() == {}
    assert p.rest_routes() == {}
    p.on_block(1, [_row(1, "A", "A", "s1", "token:issue", "tokx:1000000")])  # must not raise
    p.teardown()


def test_on_block_projects_token_lifecycle(tmp_path):
    p = _load_plugin()
    p.setup(_ctx(tmp_path))
    ti = p.services()["token_index"]
    assert ti is not None

    # issue to self at h=5, then transfer A->B at h=8 (separate blocks)
    p.on_block(5, [_row(5, "A", "A", "s_issue", "token:issue", "tokx:1000000")])
    p.on_block(8, [_row(8, "A", "B", "s_t1", "token:transfer", "tokx:400000")])
    assert ti.token_balance("tokx", "A") == 600000
    assert ti.token_balance("tokx", "B") == 400000

    # the plugin's OWN REST routes serve the explorer endpoints
    routes = p.rest_routes()
    tokens = routes[("GET", ("tokens",))](["tokens"], {})
    assert tokens["tokens"][0]["token"] == "tokx"
    detail = routes[("GET", ("token", "*"))](["token", "tokx"], {})
    assert detail["supply"] == 1000000
    assert routes[("GET", ("token", "*"))](["token", "nope"], {}) is None   # -> 404 at the router

    # peer commands are registered
    assert set(p.peer_commands()) == {"tokensget", "aliasget", "aliasesget", "addfromalias"}
    p.teardown()


def test_on_block_same_block_issue_then_transfer(tmp_path):
    p = _load_plugin()
    p.setup(_ctx(tmp_path))
    ti = p.services()["token_index"]
    # issue AND transfer in the SAME block: two-pass (issue first) means the transfer sees the token...
    p.on_block(5, [_row(5, "A", "A", "s_issue", "token:issue", "tokx:1000000"),
                   _row(5, "A", "B", "s_t1", "token:transfer", "tokx:100000")])
    # ...but the same-block CREDIT rule (credit at height < h) means B's just-received 100000 isn't
    # immediately re-spendable; A could send because its issuance credit is at the same height but debit<=h.
    # Net: A issued 1,000,000 (credited at h5), can it send at h5? credit(<5)=0 -> overspend -> rejected.
    assert ti.token_balance("tokx", "A") == 1000000      # the transfer was rejected (no same-block re-spend)
    assert ti.token_balance("tokx", "B") == 0
    p.teardown()


def test_alias_projection_and_first_claimant(tmp_path):
    p = _load_plugin()
    p.setup(_ctx(tmp_path))
    ti = p.services()["token_index"]
    p.on_block(3, [_row(3, "ALICE", "ALICE", "sa", "", "alias=alice")])
    p.on_block(7, [_row(7, "MALLORY", "MALLORY", "sb", "", "alias=alice")])   # later claim loses
    assert ti.addfromalias("alice") == "ALICE"
    assert ti.aliasget("ALICE") == [["alice"]]
    p.teardown()


def test_backfill_from_ledger(tmp_path):
    ctx = _ctx(tmp_path)
    # seed the ledger with a token issue + transfer + alias, THEN backfill (mid-chain enable path)
    conn = sqlite3.connect(ctx.ledger_path)
    conn.executemany("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [
        _row(5, "A", "A", "s_issue", "token:issue", "tokx:1000000"),
        _row(8, "A", "B", "s_t1", "token:transfer", "tokx:400000"),
        _row(3, "ALICE", "ALICE", "sa", "json", "alias=alice"),
    ])
    conn.commit(); conn.close()

    p = _load_plugin()
    p.setup(ctx)
    p.backfill()
    ti = p.services()["token_index"]
    assert ti.token_balance("tokx", "A") == 600000
    assert ti.token_balance("tokx", "B") == 400000
    assert ti.addfromalias("alice") == "ALICE"
    p.teardown()
