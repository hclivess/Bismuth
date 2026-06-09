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


# --- Second fork: the Heavy3 PoW modernisation (doc/18-D). It gets its OWN signalled activation so the
# single most security-sensitive change (a PoW transition) is isolated from hf2. Same machinery as hf2
# (dynamic_fork_height is signal-agnostic); only the coinbase marker differs.
FORK_POW_SIGNAL = "pow2"     # miners stamp this once they can mine the modernised (blake2b) Heavy3
FORK_POW_WINDOW = 1000
FORK_POW_BOUNDARY = 1000
FORK_POW_BURY = 30


def has_pow_signal(openfield):
    """Does a coinbase openfield signal readiness for the modernised PoW (doc/18-D)?"""
    return bool(openfield) and FORK_POW_SIGNAL in str(openfield)


def next_fork_boundary(height, boundary=FORK2_BOUNDARY):
    """The next multiple of ``boundary`` STRICTLY above ``height``."""
    return ((int(height) // boundary) + 1) * boundary


def dynamic_fork_height(read_signal, tip,
                        window=FORK2_WINDOW, boundary=FORK2_BOUNDARY, bury=FORK2_BURY):
    """Deterministically derive the activation height from the on-chain signal, or None if not locked.

    ``read_signal(height) -> bool``  : did the coinbase at ``height`` carry the marker?
    Lock-in is the earliest height H that closes a run of ``window`` consecutive signalled blocks; the
    fork activates at the next ``boundary`` multiple strictly above ``H + bury``. Because the run start
    is a fixed point in chain history, the result is stable as the chain grows and identical on every
    node. Cheap until signalling begins (the tip not signalling returns None after one read), and the
    caller caches the locked-in height so the back-scan runs only until lock-in.
    """
    if tip is None or tip < window:
        return None
    if not read_signal(tip):                       # not signalling at the tip -> not locked (O(1))
        return None
    run_start = tip                                 # walk back over the current consecutive-signal run
    h = tip - 1
    while h >= 1 and read_signal(h):
        run_start = h
        h -= 1
    if (tip - run_start + 1) < window:              # no full window yet
        return None
    lock_in = run_start + window - 1                # earliest window completion in this run (stable)
    return next_fork_boundary(lock_in + bury, boundary)


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


def db_pow_signal_reader(db_handler):
    """A read_signal(height) for the PoW fork (doc/18-D), backed by the coinbase openfield marker."""
    def read(height):
        db_handler.execute_param(
            db_handler.c,
            "SELECT openfield FROM transactions WHERE block_height = ? ORDER BY rowid DESC LIMIT 1",
            (height,))
        row = db_handler.c.fetchone()
        return has_pow_signal(row[0]) if row else False
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
