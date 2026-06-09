"""
Focused tests for the persistent circulating-supply cache (rest_api.py).

The cold /api/supply compute is a multi-minute full SUM(reward)-SUM(fee) scan of a multi-GB mainnet
ledger. Its result is memoized on node._supply_cache and topped up incrementally as the tip advances,
but an in-memory-only cache is wiped on every restart -> the next request would scan the whole ledger
again (the explorer shows "computing" for minutes). The fix mirrors the cache to a small JSON file
next to the ledger so a restart reloads it and only the cheap incremental top-up runs.

These tests run fully in-process with a lightweight fake node (no node subprocess, no real ledger), so
they are fast and deterministic. They assert: the on-disk round-trip, that a storage-mode mismatch is
ignored, that _supply serves a preloaded cache WITHOUT touching the DB (no full scan), and that a
background compute writes the file and the value reloads.
"""
import json
import os
import sys
import time
from decimal import Decimal
from types import SimpleNamespace

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import rest_api  # noqa: E402


class _Log:
    def warning(self, *a, **k):
        pass


def _fake_node(tmp_path, intmode=True):
    """A minimal stand-in for the real node, with a ledger_path inside tmp_path so the supply cache
    file (supply_cache.json) is written alongside it, exactly as in production."""
    ledger_path = os.path.join(str(tmp_path), "ledger.db")
    return SimpleNamespace(
        ledger_path=ledger_path,
        ledger_integer_amounts=intmode,
        hdd_block=10,
        # attributes a real compute (DbHandler) would read; harmless here:
        index_db=os.path.join(str(tmp_path), "index.db"),
        hyper_path=ledger_path,
        ram=False,
        ledger_ram_file="file:ledger_ram?mode=memory&cache=shared",
        trace_db_calls=False,
        logger=SimpleNamespace(app_log=_Log()),
    )


def test_supply_cache_path_is_next_to_ledger(tmp_path):
    # Namespaced per ledger file so regnet (regmode.db) and mainnet (ledger.db) sharing one
    # static/ dir can never read each other's cache.
    node = _fake_node(tmp_path)
    assert rest_api.supply_cache_path(node) == os.path.join(str(tmp_path), "supply_cache-ledger.db.json")


def test_save_then_load_roundtrips_as_decimal(tmp_path):
    node = _fake_node(tmp_path, intmode=True)
    cache = {"height": 42, "circ": Decimal("32486232.62370550"), "intmode": True}
    rest_api.save_supply_cache(node, cache)

    path = rest_api.supply_cache_path(node)
    assert os.path.exists(path), "supply cache file should be written"
    raw = json.load(open(path))
    # circ persisted as a STRING (no float drift), height/intmode preserved.
    assert raw == {"height": 42, "circ": "32486232.62370550", "intmode": True}
    assert isinstance(raw["circ"], str)

    # Fresh node (cache not yet in memory) reloads the exact Decimal value.
    node2 = _fake_node(tmp_path, intmode=True)
    assert not hasattr(node2, "_supply_cache")
    loaded = rest_api.load_supply_cache(node2, intmode=True)
    assert loaded is not None
    assert loaded["height"] == 42
    assert loaded["circ"] == Decimal("32486232.62370550")
    assert isinstance(loaded["circ"], Decimal)
    assert loaded["intmode"] is True
    # load also sets it on the node, so the next request finds it immediately.
    assert node2._supply_cache == loaded


def test_load_missing_or_corrupt_file_returns_none(tmp_path):
    node = _fake_node(tmp_path, intmode=True)
    # missing file
    assert rest_api.load_supply_cache(node, intmode=True) is None
    # empty / corrupt file
    open(rest_api.supply_cache_path(node), "w").write("")
    assert rest_api.load_supply_cache(node, intmode=True) is None
    open(rest_api.supply_cache_path(node), "w").write("{not json")
    assert rest_api.load_supply_cache(node, intmode=True) is None
    assert not hasattr(node, "_supply_cache")  # never set from a bad file


def test_load_ignores_storage_mode_mismatch(tmp_path):
    # A file written under integer-amounts mode must NOT be trusted by a node running in the other
    # (decimal) basis: the figure would be in the wrong unit basis.
    node = _fake_node(tmp_path, intmode=True)
    rest_api.save_supply_cache(node, {"height": 7, "circ": Decimal("1.0"), "intmode": True})
    assert rest_api.load_supply_cache(node, intmode=False) is None


class _BoomDB:
    """A DB sentinel whose use throws: if _supply touches it, a full scan was (wrongly) attempted."""
    def __getattr__(self, name):
        raise AssertionError("DB was queried -> a full scan ran when the cache should have served it")


def _bare_handler(node):
    """A Handler instance without running BaseHTTPRequestHandler.__init__ (which would need a socket).
    We only call _supply, which uses the captured `node`, the passed `db`, and module globals."""
    Handler = rest_api._make_handler(node)
    return Handler.__new__(Handler)


def test_supply_serves_preloaded_cache_without_full_scan(tmp_path):
    # Simulate a just-restarted node: nothing in memory, but a cache file on disk at/above the tip.
    node = _fake_node(tmp_path, intmode=True)
    node.hdd_block = 100
    rest_api.save_supply_cache(node, {"height": 100, "circ": Decimal("32486232.62370550"),
                                      "intmode": True})
    assert not hasattr(node, "_supply_cache")

    handler = _bare_handler(node)
    # _supply must lazily load the file and answer "ok" from it WITHOUT querying the DB.
    out = handler._supply(_BoomDB())
    assert out["status"] == "ok"
    assert out["height"] == 100
    assert out["circulating"] == "32486232.62370550"
    # and it populated the in-memory cache from disk.
    assert node._supply_cache["circ"] == Decimal("32486232.62370550")


def test_supply_compute_persists_and_reloads(tmp_path, monkeypatch):
    # Drive the background compute with a fake DbHandler returning deterministic sums, so we exercise
    # the real persist path (write supply_cache.json) and then reload without coupling to DB internals.
    node = _fake_node(tmp_path, intmode=True)
    node.hdd_block = 55

    class _FakeDB:
        def __init__(self, *a, **k):
            self.h = object()

        def fetchall(self, cursor, sql, *a):
            if "block_height >= 0" in sql:
                return [[3_248_623_262_370_550]]   # base: SUM(reward)-SUM(fee), integer units
            return [[0]]                           # mirror: SUM(amount) over negative heights

        def close(self):
            pass

    monkeypatch.setattr(rest_api.dbhandler, "DbHandler", _FakeDB)

    rest_api.start_supply_compute(node)
    # Wait for the daemon thread to finish (regnet-style: near-instant).
    deadline = time.time() + 10
    while getattr(node, "_supply_computing", False) and time.time() < deadline:
        time.sleep(0.02)
    assert not getattr(node, "_supply_computing", False), "compute thread did not finish"

    # In-memory cache set...
    assert node._supply_cache["height"] == 55
    expected = Decimal("32486232.62370550")  # 3_248_623_262_370_550 units / 1e8
    assert node._supply_cache["circ"] == expected

    # ...and persisted to disk so a future restart skips the scan.
    path = rest_api.supply_cache_path(node)
    assert os.path.exists(path)
    reloaded = rest_api.load_supply_cache(_fake_node(tmp_path, intmode=True), intmode=True)
    assert reloaded["height"] == 55
    assert reloaded["circ"] == expected
