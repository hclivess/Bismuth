"""doc/30 — historical validation exceptions for from-genesis sync.

A from-genesis VERIFYING replay re-checks every signature, balance, duplicate and
proof-of-work as it walks the chain up from block 1. A handful of historical
MAINNET blocks cannot pass those checks: they were produced by manual ledger
interventions — coin rescues, hard-fork-edge fixes — that bypassed the normal
rules at the time they happened. Without a waiver, the very first such block
halts the replay, and a fresh node can never reach the tip from genesis.

This module makes a from-genesis sync practical with two complementary,
opt-in/curated mechanisms:

  1. ``assume_valid_height`` (a trusted checkpoint, like Bitcoin's -assumevalid):
     for blocks at or below this height, skip the EXPENSIVE per-transaction
     signature re-verification. The block is still bound to the real chain by
     proof-of-work, the block-hash linkage, the difficulty retarget and the
     timestamp ordering — none of which are skipped. This is purely a speed
     optimisation for history the network has already buried under millions of
     PoW blocks. Default 0/None == OFF == every signature is verified.

  2. A targeted EXCEPTION REGISTRY (``MAINNET_EXCEPTIONS``): per-height waivers
     for the specific manual-intervention blocks that fail a STRUCTURAL check
     (overspend / duplicate / pow / timestamp), or a signature even above the
     assume-valid height. Each entry names the exact height, the exact check(s)
     to waive, and — for signature waivers — optionally the exact transaction
     signatures it applies to. Anything not listed is validated in full.

Guarantees:
  * MAINNET ONLY. Inert on testnet / regnet (overridable per-node for tests via
    ``node.validation_exceptions``).
  * INERT BY DEFAULT. With an empty registry and assume_valid off, the node
    behaves byte-identically to having no mechanism at all. Populating the
    registry only ever LOOSENS one specific historical block.
  * HISTORICAL-ONLY SCOPE. Every registry key is a fixed past height; nothing
    here can affect a newly mined block at the tip.
  * LOUD & AUDITABLE. Every applied waiver logs a warning naming the height,
    the check and the reason.
"""

# ---- the checks a waiver can name (one per RAISE site in digest.py) --------------------
SIGNATURE = "signature"   # Transaction.validate (per-tx sig + field checks), digest_tx.py
OVERSPEND = "overspend"   # BlockProcessor._validate_balance, digest.py
DUPLICATE = "duplicate"   # BlockProcessor.check_duplicate_signatures, digest.py
POW = "pow"               # BlockProcessor.verify_proof_of_work, digest.py
TIMESTAMP = "timestamp"   # block-timestamp ordering, digest.py
ALL_CHECKS = frozenset({SIGNATURE, OVERSPEND, DUPLICATE, POW, TIMESTAMP})


# ---- the curated mainnet registry -----------------------------------------------------
# height -> {"checks": set[str], "reason": str, "signatures": set[str] | None}
#   checks      : which checks to waive at this height.
#   reason      : human-readable note, logged whenever the waiver fires.
#   signatures  : ONLY consulted for the SIGNATURE check. When a set of signature
#                 PREFIXES, the signature waiver applies only to txs whose
#                 signature starts with one of them (pins the waiver to the exact
#                 rescue tx). None => the signature waiver applies to every tx in
#                 the block. Structural checks (overspend/duplicate/pow/timestamp)
#                 ignore this field — they are height-scoped.
#
# Populate from the confirmed coin-rescue / hard-fork-edge heights. Use
# tools/find_validation_exceptions.py against a ledger COPY or a peer's REST API
# to enumerate candidates — NEVER scan the live static/ledger.db. Example shape:
#
#   700000: {"checks": {OVERSPEND}, "reason": "hard-fork @700000 reward edge",
#            "signatures": None},
#   812345: {"checks": {SIGNATURE}, "reason": "coin rescue (lost-key recovery)",
#            "signatures": {"abc123def456"}},
#
MAINNET_EXCEPTIONS: dict = {
}


def load(node):
    """Optionally load an external JSON exceptions file and merge it OVER the
    in-source ``MAINNET_EXCEPTIONS``. Returns a registry dict, or None when no
    file is configured (callers then fall back to the built-in mainnet gate so
    behaviour is byte-identical to having no file).

    This lets the rescue / fork-edge heights be curated as DATA (no code edit) —
    tools/find_validation_exceptions.py emits exactly this shape:

        { "<height>": {"checks": ["overspend", ...],
                        "reason": "...",
                        "signatures": ["<sig-prefix>", ...] | null }, ... }
    """
    import os, json
    path = getattr(node, "validation_exceptions_file", "") or ""
    if not path or not os.path.exists(path):
        return None
    with open(path, "r") as f:
        raw = json.load(f)
    parsed = {}
    for k, v in raw.items():
        checks = set(v.get("checks", []))
        unknown = checks - ALL_CHECKS
        if unknown:
            raise ValueError("validation_exceptions file: unknown checks %s at height %s" % (unknown, k))
        sigs = v.get("signatures")
        parsed[int(k)] = {"checks": checks, "reason": v.get("reason", ""),
                          "signatures": set(sigs) if sigs is not None else None}
    # mainnet: the curated in-source set always applies; the file augments it.
    if not (getattr(node, "is_testnet", False) or getattr(node, "is_regnet", False)) \
            and getattr(node, "is_mainnet", True) is not False:
        merged = dict(MAINNET_EXCEPTIONS)
        merged.update(parsed)
        return merged
    return parsed


def _network_registry(node):
    """The active registry for this node, or {} when the mechanism is inert.

    Order of precedence:
      1. an explicit per-node override (``node.validation_exceptions``) — used by
         tests and private chains; lets regnet exercise the mechanism.
      2. MAINNET only — the curated ``MAINNET_EXCEPTIONS``.
      3. testnet / regnet (without an override) — empty: those chains replay
         their own history with no manual edits.
    """
    override = getattr(node, "validation_exceptions", None)
    if override is not None:
        return override
    if getattr(node, "is_testnet", False) or getattr(node, "is_regnet", False):
        return {}
    if getattr(node, "is_mainnet", True) is False:
        return {}
    return MAINNET_EXCEPTIONS


def _sig_match(signatures, signature) -> bool:
    """A signature waiver matches when it is unscoped (signatures is None) or the
    tx signature starts with one of the registered prefixes."""
    if signatures is None:
        return True
    if signature is None:
        return False
    s = str(signature)
    return any(s.startswith(str(p)) for p in signatures)


def is_exempt(node, height, check, signature=None) -> bool:
    """True iff block ``height`` is registered to waive ``check``.

    For the SIGNATURE check the waiver may be scoped to specific tx signatures.
    All other (block-level) checks are waived purely by height + check name.
    Safe to call on every block: O(1) dict lookup, empty/false by default."""
    try:
        reg = _network_registry(node)
        if not reg:
            return False
        entry = reg.get(int(height))
        if not entry:
            return False
        if check not in entry.get("checks", ()):
            return False
        if check == SIGNATURE:
            return _sig_match(entry.get("signatures"), signature)
        return True
    except Exception:
        # a malformed registry must never crash consensus — fail CLOSED (validate)
        return False


def in_trusted_prefix(node, height) -> bool:
    """True for blocks AT OR BELOW the trust horizon ``assume_valid_height``.

    In the trusted prefix the per-item validation is skipped — signature, block
    timestamp ordering, proof-of-work, duplicate-signature and overspend — because
    re-checking it is expensive (millions of memory-hard PoW + signature ops) and,
    for the earliest history, impossible under today's stricter rules (early RSA
    signing-buffer / sub-0.01s timestamp granularity). Integrity is instead
    guaranteed in aggregate by the CHECKPOINT hashes (below): a from-genesis node
    still computes every block's hash and, on reaching a checkpoint height, must
    match the hardcoded canonical hash — which (because Bismuth's block hash chains
    the previous one) cryptographically pins the ENTIRE prefix. Any tampering in
    the skipped region changes the chained hash and halts the sync.

    SAFETY — the prefix is skipped ONLY when it is actually checkpoint-anchored:
    the node must have checkpoints AND the horizon must not exceed the highest one
    (so the entire skipped region is committed by a hash that will be checked).
    A network without checkpoints (regnet / testnet without an override) therefore
    NEVER enters the trusted prefix, even if a horizon is configured — so a global
    default horizon is safe there (tests still run full validation)."""
    try:
        h = int(getattr(node, "assume_valid_height", 0) or 0)
        if h <= 0 or int(height) > h:
            return False
        cps = _checkpoints(node)
        if not cps or h > max(cps):     # no anchor / horizon past the last checkpoint -> don't skip
            return False
        return True
    except Exception:
        return False


# back-compat alias (signatures are skipped throughout the trusted prefix)
def assume_valid_skip_signature(node, height) -> bool:
    return in_trusted_prefix(node, height)


# ---- trusted-history checkpoints (the "CRC at height N" the prefix is anchored to) ----
# Bismuth's block hash chains the previous block hash, so the canonical block hash at
# height H recursively commits the WHOLE chain 1..H. A single hardcoded value per
# checkpoint height therefore vouches for everything below it: a from-genesis node that
# skipped per-item validation in the trusted prefix recomputes each block hash and, at a
# checkpoint, must reproduce the canonical value — else the (skipped) prefix is not the
# real chain and the sync halts. Verification is ALWAYS on; it is inert for a synced node
# (which never re-digests these historical heights) and only fires during a from-genesis
# catch-up as it passes a checkpoint height. These are the legacy sha224 block hashes
# (mainnet is pre-fork; block_hash_at uses the frozen legacy codec below fork_height).
MAINNET_CHECKPOINTS = {
    4000000: "6f4794262f38f5de41379fd860bb58332f1825c1c6e04885ca11db7f",
    4500000: "f717f8368c668408c18f34acaa2ede445e90174366ddb0f666756f8c",
    4800000: "88d6292c08e0371a20ea630af491933547f1aed71be519f0de591cb4",
}


def _checkpoints(node) -> dict:
    override = getattr(node, "checkpoints", None)
    if override is not None:
        return override
    if getattr(node, "is_testnet", False) or getattr(node, "is_regnet", False):
        return {}
    if getattr(node, "is_mainnet", True) is False:
        return {}
    return MAINNET_CHECKPOINTS


def checkpoint_hash(node, height):
    """The hardcoded canonical block hash at ``height``, or None if not a checkpoint."""
    try:
        return _checkpoints(node).get(int(height))
    except Exception:
        return None


def verify_checkpoint(node, height, block_hash) -> bool:
    """If ``height`` is a checkpoint, the node's COMPUTED ``block_hash`` must equal the
    canonical value — this anchors any skip-validated prefix below it. Raises ValueError
    on mismatch; returns True if a checkpoint matched, False if there is none here."""
    want = checkpoint_hash(node, height)
    if want is None:
        return False
    if str(block_hash) != str(want):
        raise ValueError(
            "checkpoint MISMATCH at height %s: computed block hash %s != canonical %s — the "
            "chain below this height is NOT the canonical chain; refusing it" % (height, block_hash, want))
    try:
        node.logger.app_log.warning(
            "checkpoint OK at height %s (%s…) — trusted prefix anchored to the canonical chain"
            % (height, str(block_hash)[:16]))
    except Exception:
        pass
    return True


def note(node, height, check, detail="") -> None:
    """Loud, auditable log line whenever a waiver is applied."""
    try:
        reg = _network_registry(node)
        reason = reg.get(int(height), {}).get("reason", "")
    except Exception:
        reason = ""
    msg = ("VALIDATION EXCEPTION at height %s: waived '%s' check" % (height, check)
           + ((" (%s)" % reason) if reason else "")
           + ((" — %s" % detail) if detail else ""))
    try:
        node.logger.app_log.warning(msg)
    except Exception:
        pass
