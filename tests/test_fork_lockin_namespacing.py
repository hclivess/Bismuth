"""Regression tests for the fork lock-in sidecar namespacing (the 2026-06-09 incident).

Mainnet (static/ledger.db) and regnet (static/regmode.db) share one state directory. The lock-in
sidecar used to be a single shared static/fork_lockin.json: a regnet test run persisted its
{"hf2": 10} lock-in there, and the next MAINNET restart loaded it — activating LWMA difficulty and
dynamic fees against the live chain. The sidecar is now namespaced per ledger filename
(fork_lockin-<ledger file>.json) so one network's lock-in can never leak into another's.
Pure file-level tests; no node required.
"""
import json
import os

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fork


def test_lockin_sidecars_are_per_ledger(tmp_path):
    """THE incident regression: a regnet lock-in must be invisible to a mainnet load."""
    mainnet_ledger = str(tmp_path / "ledger.db")
    regnet_ledger = str(tmp_path / "regmode.db")

    fork.save_locked_height(regnet_ledger, "hf2", 10)

    assert fork.load_locked_height(regnet_ledger, "hf2") == 10   # regnet still replays its own
    assert fork.load_locked_height(mainnet_ledger, "hf2") is None  # mainnet sees nothing
    assert os.path.exists(str(tmp_path / "fork_lockin-regmode.db.json"))
    assert not os.path.exists(str(tmp_path / "fork_lockin-ledger.db.json"))


def test_legacy_shared_sidecar_is_ignored(tmp_path):
    """A leftover pre-namespacing fork_lockin.json (e.g. the polluted one) is dead data."""
    mainnet_ledger = str(tmp_path / "ledger.db")
    with open(str(tmp_path / "fork_lockin.json"), "w") as fh:
        json.dump({"hf2": 10, "pow2": 10}, fh)

    assert fork.load_locked_height(mainnet_ledger, "hf2") is None
    assert fork.load_locked_height(mainnet_ledger, "pow2") is None


def test_lockin_first_writer_wins_within_a_network(tmp_path):
    """Immutability within one network is unchanged by the namespacing."""
    ledger = str(tmp_path / "ledger.db")
    fork.save_locked_height(ledger, "hf2", 5000)
    fork.save_locked_height(ledger, "hf2", 9999)   # must not overwrite
    assert fork.load_locked_height(ledger, "hf2") == 5000

    # keys are independent within the same per-ledger sidecar ("hf3" = any future fork key; the
    # one-time "pow2" key was retired when the PoW swap was folded into hf2)
    fork.save_locked_height(ledger, "hf3", 6000)
    assert fork.load_locked_height(ledger, "hf2") == 5000
    assert fork.load_locked_height(ledger, "hf3") == 6000
