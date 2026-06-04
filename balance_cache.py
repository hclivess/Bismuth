"""
Process-wide, height-stamped balance cache (read-side accelerator; see doc/16 phase 4).

``get_balance()`` returns the authoritative ledger balance (``essentials.ledger_balance3``) but
memoizes it per ``(address, chain-height)``: repeated balance reads between blocks — the dominant
explorer / wallet polling pattern — become O(1), while the returned value is always identical to the
authoritative computation. The whole cache is invalidated whenever the chain height changes (new
block or rollback), so it can never serve a stale balance, and it never touches consensus.

The deeper, true first-touch O(1) credit/debit index is intentionally deferred until amounts are
integers (doc/16 phases 2 & 4), so an incremental index can bit-match the legacy precision exactly.
"""
import threading

import essentials

_lock = threading.Lock()
_cache = {}
_height = object()  # sentinel meaning "height unknown"


def get_balance(db_handler, address):
    """Return the authoritative balance for `address`, cached per current chain height."""
    global _cache, _height
    height = db_handler.block_height_max()
    with _lock:
        if height != _height:
            _cache = {}
            _height = height
        cached = _cache.get(address)
    if cached is not None:
        return cached
    balance = essentials.ledger_balance3(address, {}, db_handler)
    with _lock:
        if height == _height:          # only cache if the height hasn't moved underneath us
            _cache[address] = balance
    return balance


def invalidate():
    """Drop all cached balances (e.g. after a rollback)."""
    global _cache, _height
    with _lock:
        _cache = {}
        _height = object()
