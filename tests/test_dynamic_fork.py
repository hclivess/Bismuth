"""
Deterministic signal-activated hard-fork scheduler (fork.dynamic_fork_height).

The activation height MUST be a pure function of the chain (the coinbase signal), identical on every
node and stable as the chain grows — otherwise nodes disagree and the network splits. These are pure
unit tests of that core (no node/DB): no-signal, partial window, lock-in, stability across tip growth,
and a broken run resetting.

Run with: python3 -m pytest tests/test_dynamic_fork.py -v
"""
from fork import has_fork_signal, next_fork_boundary, dynamic_fork_height


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
    # a single non-signalled block at 4990 breaks the run; only 4991..5000 (10) count
    sig = lambda h: h >= 4980 and h != 4990
    # run_start=4991, lock_in=5000, fork = next 1000 above 5000+5 = 6000
    assert dynamic_fork_height(sig, tip=5000, window=10, boundary=1000, bury=5) == 6000


def test_tip_below_window_is_none():
    assert dynamic_fork_height(lambda h: True, tip=5, window=10, boundary=1000, bury=5) is None
