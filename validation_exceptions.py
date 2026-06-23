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


def assume_valid_skip_signature(node, height) -> bool:
    """True iff ``height`` is at or below the node's trusted ``assume_valid_height``,
    so the expensive per-tx signature re-verification can be skipped. Off by
    default (``assume_valid_height`` is 0/None) -> always returns False, so every
    signature is verified exactly as before. Never applies on regnet/testnet
    unless the operator set the height explicitly there too."""
    try:
        h = getattr(node, "assume_valid_height", 0) or 0
        return int(h) > 0 and int(height) <= int(h)
    except Exception:
        return False


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
