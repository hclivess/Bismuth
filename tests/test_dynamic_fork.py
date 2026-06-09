"""
Deterministic signal-activated hard-fork scheduler (fork.dynamic_fork_height).

The activation height MUST be a pure function of the chain (the coinbase signal), identical on every
node and stable as the chain grows — otherwise nodes disagree and the network splits. These are pure
unit tests of that core (no node/DB): no-signal, partial window, lock-in, stability across tip growth,
and a broken run resetting.

Run with: python3 -m pytest tests/test_dynamic_fork.py -v
"""
from fork import has_fork_signal, next_fork_boundary, dynamic_fork_height, lockin_at_tip


def test_has_fork_signal():
    assert has_fork_signal("hf2")
    assert has_fork_signal("poolname/hf2")          # may be appended to existing openfield content
    assert not has_fork_signal("")
    assert not has_fork_signal(None)
    assert not has_fork_signal("nope")


def test_next_boundary_is_strictly_above():
    assert next_fork_boundary(4500, 1000) == 5000
    assert next_fork_boundary(4999, 1000) == 5000
    assert next_fork_boundary(5000, 1000) == 6000   # strictly above, never equal


def test_no_signal_never_locks():
    assert dynamic_fork_height(lambda h: False, tip=5000, window=10, boundary=1000, bury=5) is None


def test_partial_window_does_not_lock():
    sig = lambda h: h > 4995                          # only the last 5 signal
    assert dynamic_fork_height(sig, tip=5000, window=10, boundary=1000, bury=5) is None


def test_full_window_locks_in_at_next_boundary():
    # 4980..5000 all signal (21), window=10 -> lock_in=4989, fork = next 1000 above 4989+5 = 5000
    sig = lambda h: h >= 4980
    assert dynamic_fork_height(sig, tip=5000, window=10, boundary=1000, bury=5) == 5000


def test_deterministic_as_chain_grows():
    # same run, later tip -> SAME activation height (the run start is a fixed point)
    sig = lambda h: h >= 4980
    a = dynamic_fork_height(sig, tip=5000, window=10, boundary=1000, bury=5)
    b = dynamic_fork_height(sig, tip=5200, window=10, boundary=1000, bury=5)
    assert a == b == 5000


def test_broken_run_resets_lock_in():
    # a gap BEFORE any full window resets the count; lock-in is the FIRST window that completes after it.
    # signal 4980..4989 (9 blocks, gap at 4985 breaks them: max run 5) then 4990..5000 (11 consecutive).
    sig = lambda h: 4980 <= h <= 5000 and h != 4985
    # forward scan: run resets at 4985; 4990..4999 is the first 10-in-a-row -> lock_in=4999,
    # fork = next 1000 strictly above 4999+5=5004 -> 6000.
    assert dynamic_fork_height(sig, tip=5000, window=10, boundary=1000, bury=5) == 6000


def test_lockin_is_first_window_completion_forward():
    # the lock-in is the EARLIEST window completion in history, found by scanning forward (not the run
    # that touches the tip). 4980..4989 already completes a window -> lock_in=4989 -> fork 5000, even
    # though a later gap at 4995 exists.
    sig = lambda h: 4980 <= h <= 5000 and h != 4995
    assert dynamic_fork_height(sig, tip=5000, window=10, boundary=1000, bury=5) == 5000


def test_tip_below_window_is_none():
    assert dynamic_fork_height(lambda h: True, tip=5, window=10, boundary=1000, bury=5) is None


def test_C1_tip_independent_across_post_activation_gap():
    """C1 regression: the locked activation height MUST be identical at tip=H and tip=H+500 even with a
    one-block signalling gap AFTER activation. (The review's exact repro: signal 1-50, gap at 51,
    52-200 -> the same height at tip=50 and tip=200.) The old back-scan keyed off the tip's run start,
    so tip=50 saw run 1-50 while tip=200 saw run 52-200 and they DISAGREED -> consensus split."""
    sig = lambda h: (1 <= h <= 50) or (52 <= h <= 200)        # gap at 51, after the first window closes
    w, b, bury = 40, 10, 2
    at_50 = dynamic_fork_height(sig, tip=50, window=w, boundary=b, bury=bury)
    at_200 = dynamic_fork_height(sig, tip=200, window=w, boundary=b, bury=bury)
    assert at_50 is not None
    assert at_50 == at_200, (at_50, at_200)                   # tip-independent: no split


def test_lockin_at_tip_matches_forward_scan_on_live_growth():
    """The O(window) digest hot-path check (lockin_at_tip) must agree with the full forward scan at the
    FIRST tip where a window closes — that's how the digest detects lock-in cheaply per block."""
    sig = lambda h: h >= 4991                          # window of 10 first closes at 5000
    w, b, bury = 10, 1000, 5
    # before completion: neither fires
    assert lockin_at_tip(sig, tip=4999, window=w, boundary=b, bury=bury) is None
    assert dynamic_fork_height(sig, tip=4999, window=w, boundary=b, bury=bury) is None
    # at the completing tip: both agree
    assert lockin_at_tip(sig, tip=5000, window=w, boundary=b, bury=bury) \
        == dynamic_fork_height(sig, tip=5000, window=w, boundary=b, bury=bury)


def test_lockin_at_tip_requires_full_trailing_window():
    sig = lambda h: h >= 4991 and h != 4995            # gap inside the trailing window
    assert lockin_at_tip(sig, tip=5000, window=10, boundary=1000, bury=5) is None


def test_persist_lockin_is_immutable_and_replays(tmp_path):
    """C1 persistence: the first-locked height is written once, survives a 'restart' (reload), and a
    later attempt to store a DIFFERENT value is ignored — so a resync can't recompute a different one."""
    import fork
    ledger = str(tmp_path / "ledger.db")             # only the dirname is used; file need not exist
    assert fork.load_locked_height(ledger, "hf2") is None     # nothing persisted yet
    fork.save_locked_height(ledger, "hf2", 5000)
    assert fork.load_locked_height(ledger, "hf2") == 5000     # replays after a restart
    fork.save_locked_height(ledger, "hf2", 6000)              # immutable: first writer wins
    assert fork.load_locked_height(ledger, "hf2") == 5000
    fork.save_locked_height(ledger, "pow2", 7000)             # independent key
    assert fork.load_locked_height(ledger, "pow2") == 7000
    assert fork.load_locked_height(ledger, "hf2") == 5000
