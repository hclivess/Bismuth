"""
Derived LMDB stores must be NAMESPACED PER LEDGER — no network ever shares a store directory.

They used to be bare siblings of the ledger (``static/blockstore``), so mainnet (``static/ledger.db``),
testnet and regnet (``static/regmode.db``) all pointed at the SAME directory: a regnet run would hand
its blocks/balances to a mainnet node (the doc/18 pollution class), and each network's startup
wipe/rebuild silently trashed the other's store. These tests lock the separation in.

Run with: python3 -m pytest tests/test_store_paths.py -v
"""
import os

import kvstore

STORES = ("blockstore", "balanceindex", "txidindex", "pkregistry")

MAINNET = "static/ledger.db"
TESTNET = "static/test.db"
REGNET = "static/regmode.db"


def test_store_path_is_namespaced_by_ledger_filename():
    for name in STORES:
        p = kvstore.store_path(MAINNET, name)
        assert os.path.dirname(p) == "static", p          # still a sibling of the ledger
        assert os.path.basename(p) == "%s-ledger.db" % name, p


def test_networks_never_share_a_store_directory():
    """THE regression: every store path must differ across mainnet / testnet / regnet."""
    for name in STORES:
        paths = {kvstore.store_path(led, name) for led in (MAINNET, TESTNET, REGNET)}
        assert len(paths) == 3, "%s collides across networks: %s" % (name, sorted(paths))


def test_no_store_collides_with_another_store_on_the_same_ledger():
    paths = {kvstore.store_path(REGNET, name) for name in STORES}
    assert len(paths) == len(STORES), sorted(paths)


def test_matches_the_token_index_convention():
    """token_index.open_for already namespaced this way; the helper must agree with it."""
    import token_index
    expected = token_index.open_for.__doc__ and True     # import guard only
    assert expected
    assert os.path.basename(kvstore.store_path(REGNET, "tokenindex")) == "tokenindex-regmode.db"


def test_bare_filename_and_empty_dir_are_handled():
    # a ledger path with no directory component stays relative, never absolute
    p = kvstore.store_path("ledger.db", "blockstore")
    assert p == os.path.join(".", "blockstore-ledger.db")


def test_legacy_shared_path_is_not_reused():
    """No store may resolve to the old shared name — adopting it is exactly the pollution risk."""
    for name in STORES:
        for led in (MAINNET, TESTNET, REGNET):
            assert os.path.basename(kvstore.store_path(led, name)) != name
