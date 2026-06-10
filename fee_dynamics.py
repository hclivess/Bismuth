"""
fee_dynamics.py — post-fork DYNAMIC base fee + VM execution surcharge (gated in essentials.fee_calculate).

The fee analogue of the LWMA difficulty: a smooth, demand-responsive, *calculable* base fee. It scales
with recent block CONGESTION over a window and is clamped, so it rises gently under load and falls when
the chain is idle — and you can predict the next block's fee from the recent chain, no surprise spikes.

CONGESTION = WEIGHT, not just count. The signal the digester averages is per-block WEIGHT
(``block_store.BlockStore.recent_block_weights`` = tx count + openfield-bytes // W_UNIT), a gas/vbyte-style
measure, so a block of large RingCT/VM txs registers as more congested than the same number of tiny
transfers — the fee reflects how FULL blocks are, not merely how many txs they hold. (``base_fee`` is
generic over any per-block load list.)

NO SQLITE POST-FORK: the weight is read from the post-fork BLOCK STORE (LMDB), never a SQLite query — there
is no SQLite on any post-fork path. The store is rolled back with the chain, so the window is canonical.

DETERMINISTIC + STATELESS: computed directly from the stored blocks with a NON-recursive formula, so every
node derives the same value and a restart needs no saved fee state (unlike EIP-1559's recursive base fee).
Inert until hf2 — pre-fork, fee_calculate uses the static BASE_FEE.

MANIPULATION-RESISTANT: window-averaged (a single block barely moves it), clamped to [MIN_MULT, MAX_MULT]
of the static fee (no runaway spike), and stuffing blocks to inflate the fee costs the attacker the very
fees they raise — so gaming is bounded and self-taxing, the same shape as EIP-1559's bounded adjustment.
"""
from decimal import Decimal

from quantizer import quantize_eight

WINDOW = 20                       # blocks of recent congestion to average
TARGET_TXS = 30                   # count-only target (legacy signal): txs/block at the comfortable load
W_UNIT = 1000                     # bytes of openfield per 1 weight unit (the gas/vbyte granularity)
TARGET_WEIGHT = 30                # WEIGHT/block at the comfortable load (== TARGET_TXS for all-tiny blocks,
#                                   so the baseline is unchanged; large txs add weight above it)
MIN_MULT = Decimal("0.5")         # base fee floor multiplier (cheaper when idle)
MAX_MULT = Decimal("10")          # base fee ceiling multiplier (clamped -> no runaway spike)
VM_SURCHARGE = Decimal("0.01000000")   # vm: txs pay extra for execution (gas economics)


def base_fee(static_base_fee, recent_loads, target=TARGET_WEIGHT):
    """Demand-responsive base fee from a list of recent per-block LOADS (block weights from the block store's
    ``recent_block_weights`` post-fork — the formula is generic over any per-block load list).

    factor = clamp(avg(recent_loads) / target, MIN_MULT, MAX_MULT); fee = static_base_fee * factor.
    Smooth (window-averaged), bounded (clamped), and a pure function of the inputs.
    """
    base = Decimal(static_base_fee)
    loads = [c for c in (recent_loads or []) if c is not None]
    if not loads:
        return quantize_eight(base)
    avg = Decimal(sum(loads)) / Decimal(len(loads))
    factor = avg / Decimal(target)
    if factor < MIN_MULT:
        factor = MIN_MULT
    elif factor > MAX_MULT:
        factor = MAX_MULT
    return quantize_eight(base * factor)
