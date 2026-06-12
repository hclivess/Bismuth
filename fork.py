# --- Automatic (signal-activated) hard-fork scheduling (doc/17) ------------------------------------
# A version-bits / miner-activated scheme. Upgraded miners stamp FORK2_SIGNAL into their coinbase
# openfield; every node counts it from the SAME chain, so the activation height is computed identically
# network-wide (deterministic — NOT an off-chain peer survey, which would let nodes disagree and split).
# When the trailing window of blocks is all-signalled the fork LOCKS IN, and activates at the next round
# boundary, with a burial margin so a near-tip reorg can't move it. This is INERT: it only READS the
# chain and schedules; no consensus rule changes here. The rule switch (block_height >= fork_height) is
# added later, one optimization at a time, replay-validated.

"""Hard-fork scheduling and activation-height bookkeeping.

Holds the signal-activated (version-bits / miner-activated) hard-fork logic
plus the ``Fork`` object that tracks the known fork heights for the active
network (mainnet/testnet/regnet). It is deliberately inert with respect to
consensus: the helpers here only READ the chain to count fork signals in
coinbase openfields and to compute -- deterministically and identically on
every node -- when a fork locks in and activates. The actual rule changes are
gated elsewhere by ``block_height >= fork_height`` comparisons that consult
these heights. ``difficulty`` and other modules import this to know which rule
era a given block falls under.
"""

FORK2_SIGNAL = "hf2"      # marker upgraded miners place in their coinbase openfield
FORK2_WINDOW = 1000       # consecutive all-signalled blocks required to lock in
FORK2_BOUNDARY = 1000     # activate on the next multiple of this (the "next round-1000 block")
FORK2_BURY = 30           # >= rollback_depth: blocks between lock-in and activation (reorg safety)


def has_fork_signal(openfield):
    """Does a coinbase openfield carry the fork-readiness marker?"""
    return bool(openfield) and FORK2_SIGNAL in str(openfield)


# NOTE: the Heavy3 PoW modernisation (sha224 -> blake2b, doc/18-D) is BUNDLED INTO hf2: one signal,
# one activation height. Stamping "hf2" therefore asserts readiness for the WHOLE bundle, including
# mining/validating the blake2b Heavy3 from the activation height. (It briefly had its own "pow2"
# signal; that was folded into hf2 on 2026-06-12 — stale "pow2" sidecar keys are simply ignored.)


def next_fork_boundary(height, boundary=FORK2_BOUNDARY):
    """The next multiple of ``boundary`` STRICTLY above ``height``."""
    return ((int(height) // boundary) + 1) * boundary


def dynamic_fork_height(read_signal, tip,
                        window=FORK2_WINDOW, boundary=FORK2_BOUNDARY, bury=FORK2_BURY,
                        floor=1):
    """Deterministically derive the activation height from the on-chain signal, or None if not locked.

    ``read_signal(height) -> bool``  : did the coinbase at ``height`` carry the marker?
    Lock-in is the EARLIEST height ``H`` in confirmed history that closes a run of ``window`` consecutive
    signalled blocks (scanning FORWARD from ``floor``); the fork then activates at the next ``boundary``
    multiple strictly above ``H + bury``.

    Scanning forward — rather than back-walking the run that happens to touch the tip — makes the result a
    pure function of confirmed history that is INDEPENDENT of the current tip: a signalling gap that
    appears AFTER the first completed window cannot move the lock-in, so a node evaluating at tip ``H``
    and another (or a resync) at tip ``H+500`` compute the IDENTICAL activation height. (The old
    back-scan keyed off the tip's run start, so a post-activation gap shifted the answer and split the
    chain.) The caller caches+persists the locked height, so this only runs until lock-in.
    """
    if tip is None or tip < window:
        return None
    run = 0
    start = max(1, int(floor))
    for h in range(start, int(tip) + 1):            # forward scan: first window completion is the lock-in
        if read_signal(h):
            run += 1
            if run >= window:
                lock_in = h                         # earliest height closing a full window (tip-independent)
                return next_fork_boundary(lock_in + bury, boundary)
        else:
            run = 0                                 # a gap resets the consecutive count
    return None


# --- Persisted lock-in (consensus determinism) -----------------------------------------------------
# Once locked in, the activation height is a FACT about confirmed history, not a per-run recomputation.
# We write it to a tiny sidecar (next to the ledger) the moment it's first determined so a restart or a
# resync REPLAYS the same height instead of re-deriving one that a later signalling gap could perturb.
# The reader is forward-scanning (tip-independent) so the persisted value should always agree, but the
# sidecar makes it authoritative and removes any dependence on which heights are still on disk.
import json as _json
import os as _os

def lockin_path(ledger_path):
    """The sidecar path: alongside the ledger DB and NAMESPACED BY LEDGER FILENAME
    (``fork_lockin-<ledger file>.json``), holding {"hf2": <height>}; an absent key = not yet
    locked. Unknown/legacy keys (e.g. the retired "pow2") are ignored by the reader.

    The namespacing is load-bearing: mainnet (ledger.db) and regnet (regmode.db) share one
    static/ directory, and the earlier shared ``fork_lockin.json`` let a regnet test run hand a
    restarting MAINNET node its regnet lock-in ({"hf2": 10}) — silently activating LWMA difficulty
    and dynamic fees against the live chain (2026-06-09 incident). The legacy shared file is now
    ignored entirely."""
    base = _os.path.basename(ledger_path) or "ledger.db"
    return _os.path.join(_os.path.dirname(ledger_path) or ".", "fork_lockin-%s.json" % base)


def load_locked_height(ledger_path, key):
    """Return the persisted activation height for ``key`` ('hf2'), or None if never locked.
    Tolerant: a missing/corrupt sidecar simply means "not locked yet" (the chain re-derives it)."""
    try:
        with open(lockin_path(ledger_path), "r") as fh:
            v = _json.load(fh).get(key)
        return int(v) if v is not None else None
    except Exception:
        return None


def save_locked_height(ledger_path, key, height):
    """Persist ``height`` for ``key`` ONCE (first writer wins): never overwrite an already-stored value,
    so the locked height can't drift across restarts. Best-effort + atomic (temp file + replace)."""
    if height is None:
        return
    path = lockin_path(ledger_path)
    try:
        data = {}
        try:
            with open(path, "r") as fh:
                data = _json.load(fh) or {}
        except Exception:
            data = {}
        if data.get(key) is not None:               # already locked -> immutable, leave it
            return
        data[key] = int(height)
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            _json.dump(data, fh)
            fh.flush()
            _os.fsync(fh.fileno())
        _os.replace(tmp, path)                       # atomic swap so a crash can't leave a half-written file
    except Exception:
        pass                                         # persistence is best-effort; the chain still re-derives


def lockin_at_tip(read_signal, tip, window=FORK2_WINDOW, boundary=FORK2_BOUNDARY, bury=FORK2_BURY):
    """O(window) hot-path check: does a full window CLOSE exactly at ``tip``? Returns the activation
    height if the trailing ``window`` blocks [tip-window+1 .. tip] are ALL signalled, else None.

    For a chain digested block-by-block this fires at the FIRST tip whose trailing window is full, which
    is exactly the first window completion in history — so it agrees with ``dynamic_fork_height`` but
    costs a constant ``window`` reads instead of rescanning the whole chain every block. (A node that
    resyncs PAST a completion relies on the persisted sidecar / a one-time full ``dynamic_fork_height``
    catch-up, not this.)"""
    if tip is None or tip < window:
        return None
    for h in range(int(tip) - int(window) + 1, int(tip) + 1):
        if not read_signal(h):
            return None
    return next_fork_boundary(int(tip) + bury, boundary)     # lock_in == tip (window closes here)


def db_fork_signal_reader(db_handler):
    """A read_signal(height) backed by the ledger: the coinbase (the reward!=0 row) openfield."""
    def read(height):
        # the coinbase (miner tx) is the last tx of the block, so the highest rowid at this height;
        # its openfield carries the nonce + any signal marker.
        db_handler.execute_param(
            db_handler.c,
            "SELECT openfield FROM transactions WHERE block_height = ? ORDER BY rowid DESC LIMIT 1",
            (height,))
        row = db_handler.c.fetchone()
        return has_fork_signal(row[0]) if row else False
    return read


def fork_status(read_signal, tip, window=FORK2_WINDOW, boundary=FORK2_BOUNDARY, bury=FORK2_BURY):
    """Readiness view: the current consecutive-signal run, lock-in, activation height, and whether the
    tip is already past it. Pure (``read_signal`` injected); this is what ``/api/fork`` reports."""
    fork_height = dynamic_fork_height(read_signal, tip, window, boundary, bury)
    run = 0
    if tip:
        h = tip
        while h >= 1 and read_signal(h):
            run += 1
            h -= 1
    return {"tip": tip, "window": window, "signalling_run": run,
            "locked_in": fork_height is not None, "fork_height": fork_height,
            "active": fork_height is not None and tip is not None and tip >= fork_height}


class Fork():
    def __init__(self):
        self.POW_FORK = 1450000
        self.POW_FORK_TESTNET = 894170
        self.FORK_AHEAD = 5
        self.versions_remove = ['mainnet0020', 'mainnet0019', 'mainnet0018', 'mainnet0017']
        self.FORK_REWARD = None
        self.FORK_REWARD_TESTNET = None
        self.PASSED = False
        self.PASSED_TESTNET = False
        self.REWARD_MAX = 6

        #self.POW_FORK = 1168860 #HACK
        #self.versions_remove = [] #HACK
        #self.REWARD_MAX = 5 #HACK

    def limit_version(self, node):
        for allowed_version in node.version_allow:
            if allowed_version in self.versions_remove:
                node.logger.app_log.warning(f"Beginning to reject old protocol versions - block {node.last_block}")
                node.version_allow.remove(allowed_version)

    def check_postfork_reward(self, db_handler):
        try:
            # Check RAM first
            db_handler.execute_param(db_handler.c, "SELECT reward FROM transactions WHERE block_height = ? AND reward != 0", (self.POW_FORK + 1,))
            result = db_handler.c.fetchone()
            if result:
                self.FORK_REWARD = result[0]
                self.PASSED = self.FORK_REWARD < self.REWARD_MAX
            else:
                # Block not found, assume valid fork
                self.PASSED = True
        except Exception as e:
            try:
                # Fallback to HDD
                db_handler.execute_param(db_handler.h, "SELECT reward FROM transactions WHERE block_height = ? AND reward != 0", (self.POW_FORK + 1,))
                result = db_handler.h.fetchone()
                if result:
                    self.FORK_REWARD = result[0]
                    self.PASSED = self.FORK_REWARD < self.REWARD_MAX
                else:
                    self.PASSED = True  # Block pruned, assume valid
            except:
                self.PASSED = True  # Database error, assume valid
        return self.PASSED

    def check_postfork_reward_testnet(self, db_handler):
        try:
            db_handler.execute_param(db_handler.c, "SELECT reward FROM transactions WHERE block_height = ? AND reward != 0", (self.POW_FORK_TESTNET + 1,))
            result = db_handler.c.fetchone()
            if result:
                self.FORK_REWARD_TESTNET = result[0]
                self.PASSED_TESTNET = self.FORK_REWARD_TESTNET < self.REWARD_MAX
            else:
                self.PASSED_TESTNET = True
        except Exception as e:
            try:
                db_handler.execute_param(db_handler.h, "SELECT reward FROM transactions WHERE block_height = ? AND reward != 0", (self.POW_FORK_TESTNET + 1,))
                result = db_handler.h.fetchone()
                if result:
                    self.FORK_REWARD_TESTNET = result[0]
                    self.PASSED_TESTNET = self.FORK_REWARD_TESTNET < self.REWARD_MAX
                else:
                    self.PASSED_TESTNET = True
            except:
                self.PASSED_TESTNET = True
        return self.PASSED_TESTNET
