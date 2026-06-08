"""
fee_dynamics.py — post-fork DYNAMIC base fee + VM execution surcharge (gated in essentials.fee_calculate).

The fee analogue of the LWMA difficulty: a smooth, demand-responsive, *calculable* base fee. It scales
with recent block fullness over a window and is clamped, so it rises gently under load and falls when the
chain is idle — and you can predict the next block's fee from the recent chain, no surprise spikes.

DETERMINISTIC + STATELESS: computed directly from the chain (recent per-block tx counts) with a
NON-recursive formula, so every node derives the same value and a restart needs no saved fee state
(unlike EIP-1559's recursive base fee). Inert until hf2 — pre-fork, fee_calculate uses the static BASE_FEE.
"""
from decimal import Decimal

from quantizer import quantize_eight

WINDOW = 20                       # blocks of recent demand to average
TARGET_TXS = 30                   # target txs/block; above -> fee up, below -> fee down
MIN_MULT = Decimal("0.5")         # base fee floor multiplier (cheaper when idle)
MAX_MULT = Decimal("10")          # base fee ceiling multiplier (clamped -> no runaway spike)
VM_SURCHARGE = Decimal("0.01000000")   # vm: txs pay extra for execution (gas economics)


def base_fee(static_base_fee, recent_tx_counts, target=TARGET_TXS):
    """Demand-responsive base fee from recent per-block tx counts.

    factor = clamp(avg(recent_tx_counts) / target, MIN_MULT, MAX_MULT); fee = static_base_fee * factor.
    Smooth (window-averaged), bounded (clamped), and a pure function of the inputs.
    """
    base = Decimal(static_base_fee)
    counts = [c for c in (recent_tx_counts or []) if c is not None]
    if not counts:
        return quantize_eight(base)
    avg = Decimal(sum(counts)) / Decimal(len(counts))
    factor = avg / Decimal(target)
    if factor < MIN_MULT:
        factor = MIN_MULT
    elif factor > MAX_MULT:
        factor = MAX_MULT
    return quantize_eight(base * factor)
