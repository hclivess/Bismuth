"""
Node-free tests for the modern tokens_aliases PLUGIN (doc/27) — it owns the LMDB token/alias side-index
and reacts to the block lifecycle. These drive the plugin object directly (no running node), with a real
``PluginContext`` over a tiny temp LMDB ledger (``block_store.BlockStore``) for the backfill path.

Pins: always-on, ``on_block`` projection (issue→transfer→alias, two-pass), the ``token_index`` service
handle, the plugin's own REST routes (/api/tokens, /api/token/<name>), and historical ``backfill``.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

pytest.importorskip("lmdb")

import importlib.machinery
import importlib.util

import block_store
import kvstore
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


_LEDGERS = []


@pytest.fixture(autouse=True)
def _close_ledgers():
    yield
    while _LEDGERS:
        block_store.close_ledger(_LEDGERS.pop())


def _ctx(tmp_path):
    ledger_path = str(tmp_path / "ledger.db")
    store_dir = kvstore.store_path(ledger_path, "blockstore")
    _LEDGERS.append(store_dir)
    store = block_store.open_ledger(store_dir, map_size=64 * 1024 * 1024)
    return plugin_base.PluginContext(logger=_Log(), data_dir=str(tmp_path / "pdata"),
                                     ledger_path=ledger_path, config=None, is_regnet=True, ledger=store)


def _row(bh, addr, recip, sig, op, openfield, reward=0):
    """A 12-field block_transactions row: bh, ts, addr, recip, amt, sig, pubkey, bhash, fee, reward, op, openfield."""
    return [bh, 1000 + bh, addr, recip, "0", sig, "pk", "bhash", "0", reward, op, openfield]


def _commit(ctx, blocks):
    """Write ``{height: [rows]}`` into the LMDB ledger as consecutive blocks 1..max (gaps = empty blocks)."""
    for h in range(1, max(blocks) + 1):
        rows = [[h] + list(r[1:]) for r in blocks.get(h, [])]
        ctx.ledger.commit_block(h, "%040x" % h, rows)


def test_always_enabled(tmp_path):
    p = _load_plugin()
    p.setup(_ctx(tmp_path))
    assert p.ti is not None
    assert "token_index" in p.services()
    assert set(p.peer_commands()) == {"tokensget", "aliasget", "aliasesget", "addfromalias"}
    p.teardown()


@pytest.mark.parametrize("reward,zero", [(0, True), (0.0, True), ("0", True), ("0.0", True), ("0E-8", True),
                                         ("", True), (None, True), (2.5, False), ("2.5", False), (1, False),
                                         ("12.34", False)])
def test_is_reward_zero_typed(reward, zero):
    p = _load_plugin()
    assert p._is_reward_zero(reward) is zero


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
    # seed the LMDB ledger with a token issue + transfer + alias (+ a coinbase that must be skipped and a
    # structured alias op whose openfield also starts "alias="), THEN backfill (mid-chain enable path)
    _commit(ctx, {
        3: [_row(3, "ALICE", "ALICE", "sa", "json", "alias=alice")],
        4: [_row(4, "CB", "CB", "scb", "token:issue", "cbtok:5", reward="2.5")],
        5: [_row(5, "A", "A", "s_issue", "token:issue", "tokx:1000000")],
        6: [_row(6, "EVE", "EVE", "se", "alias:register", "alias=evil")],
        8: [_row(8, "A", "B", "s_t1", "token:transfer", "tokx:400000")],
    })

    p = _load_plugin()
    p.setup(ctx)
    p.backfill()
    ti = p.services()["token_index"]
    assert ti.token_balance("tokx", "A") == 600000
    assert ti.token_balance("tokx", "B") == 400000
    assert ti.addfromalias("alice") == "ALICE"
    assert not ti.has_token("cbtok")                  # coinbase rows are never projected
    assert ti.alias_owner("alias=evil") == "EVE"      # structured register claims its literal name...
    assert ti.alias_owner("evil") is None             # ...and not ALSO the legacy name
    p.backfill()                                      # idempotent re-run from the anchors
    assert ti.token_balance("tokx", "A") == 600000
    p.teardown()


def test_alias_register_transfer_free(tmp_path):
    # alias evolution ops: alias:register / alias:transfer / alias:free with mutable ownership
    p = _load_plugin()
    p.setup(_ctx(tmp_path))
    ti = p.services()["token_index"]

    p.on_block(3, [_row(3, "ALICE", "ALICE", "s1", "alias:register", "alice")])
    assert ti.alias_owner("alice") == "ALICE"
    assert ti.addfromalias("alice") == "ALICE"
    assert ti.aliasget("ALICE") == [["alice"]]

    # duplicate register loses (first claimant keeps it)
    p.on_block(4, [_row(4, "MALLORY", "MALLORY", "s2", "alias:register", "alice")])
    assert ti.alias_owner("alice") == "ALICE"

    # transfer ALICE -> BOB ("recipient:alias")
    p.on_block(5, [_row(5, "ALICE", "ALICE", "s3", "alias:transfer", "BOB:alice")])
    assert ti.alias_owner("alice") == "BOB"
    assert ti.aliasget("BOB") == [["alice"]]
    assert ti.aliasget("ALICE") == [["ALICE"]]      # ALICE no longer owns it -> address fallback

    # a non-owner transfer is a no-op
    p.on_block(6, [_row(6, "ALICE", "ALICE", "s4", "alias:transfer", "EVE:alice")])
    assert ti.alias_owner("alice") == "BOB"

    # free by the owner makes it claimable again
    p.on_block(7, [_row(7, "BOB", "BOB", "s5", "alias:free", "alice")])
    assert ti.alias_owner("alice") is None
    assert ti.addfromalias("alice") == "No alias"
    assert ti.aliasget("BOB") == [["BOB"]]

    # re-register after free
    p.on_block(8, [_row(8, "CAROL", "CAROL", "s6", "alias:register", "alice")])
    assert ti.alias_owner("alice") == "CAROL"
    p.teardown()


def test_alias_ops_rollback(tmp_path):
    # a register -> transfer -> free stack must unwind to the correct prior owner at each step
    p = _load_plugin()
    p.setup(_ctx(tmp_path))
    ti = p.services()["token_index"]
    p.on_block(3, [_row(3, "ALICE", "ALICE", "s1", "alias:register", "alice")])
    p.on_block(5, [_row(5, "ALICE", "ALICE", "s2", "alias:transfer", "BOB:alice")])
    p.on_block(7, [_row(7, "BOB", "BOB", "s3", "alias:free", "alice")])
    assert ti.alias_owner("alice") is None

    ti.aliases_rollback(7)                            # undo free -> BOB owns again
    assert ti.alias_owner("alice") == "BOB"
    assert ti.aliasget("BOB") == [["alice"]]

    ti.aliases_rollback(5)                            # undo transfer -> ALICE owns again
    assert ti.alias_owner("alice") == "ALICE"
    assert ti.aliasget("ALICE") == [["alice"]]

    ti.aliases_rollback(3)                            # undo register -> gone
    assert ti.alias_owner("alice") is None
    p.teardown()


def test_alias_register_no_legacy_double_claim(tmp_path):
    # audit L-5: a structured alias:register tx whose openfield also starts "alias=" must NOT ALSO claim
    # the legacy name — the old separate `if openfield.startswith("alias=")` let one tx claim two aliases.
    p = _load_plugin()
    p.setup(_ctx(tmp_path))
    ti = p.services()["token_index"]
    p.on_block(3, [_row(3, "ALICE", "ALICE", "s1", "alias:register", "alias=evil")])
    assert ti.alias_owner("alias=evil") == "ALICE"    # the structured register name is claimed
    assert ti.alias_owner("evil") is None             # ...and the legacy name is NOT also claimed
    p.teardown()


def test_alias_self_transfer_is_noop(tmp_path):
    # audit L-6: a self-transfer (recipient == sender) is rejected as a no-op, not recorded (it would only
    # churn the registration height).
    p = _load_plugin()
    p.setup(_ctx(tmp_path))
    ti = p.services()["token_index"]
    p.on_block(3, [_row(3, "ALICE", "ALICE", "s1", "alias:register", "alice")])
    assert ti.alias_owner("alice") == "ALICE"
    p.on_block(4, [_row(4, "ALICE", "ALICE", "s2", "alias:transfer", "ALICE:alice")])   # self-transfer
    assert ti.alias_owner("alice") == "ALICE"         # unchanged
    p.teardown()
