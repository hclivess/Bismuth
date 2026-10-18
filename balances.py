"""
Authoritative, mempool-aware address balance for the ``balanceget*`` socket commands, lifted out of
``node.py``. It reproduces the exact legacy six-tuple the wire protocol returns
(balance, credit, debit, fees, rewards, balance_no_mempool) from the ledger's maintained per-address
balance entry (``BlockStore.balance_parts``, integer units, O(1)) and folds in pending mempool debits.
Takes ``node`` explicitly (only needs ``node.last_block`` for the mempool fee calc), so it is a clean leaf
with no import cycle.
"""
import block_store
from decimal import Decimal

import amounts
import mempool as mp
from essentials import fee_calculate
from quantizer import quantize_eight


def _dec(units):
    """Integer units -> Decimal BIS; zero renders as the legacy bare ``0`` (never ``0E-8``)."""
    return amounts.to_decimal(units) if units else Decimal("0")


def balanceget(node, balance_address, db_handler):
    base_mempool = mp.MEMPOOL.mp_get(balance_address)

    # include mempool fees
    debit_mempool = 0
    for x in base_mempool or ():
        debit_tx = Decimal(x[0])
        fee = fee_calculate(x[1], x[2], node.last_block)
        debit_mempool = quantize_eight(debit_mempool + debit_tx + fee)

    store = block_store.as_ledger(db_handler)
    credit_u, rewards_u, debit_u, fees_u = store.balance_parts(balance_address)

    credit_ledger = _dec(credit_u)
    debit_ledger = _dec(debit_u)
    fees = _dec(fees_u)
    rewards = _dec(rewards_u)

    debit = quantize_eight(debit_ledger + debit_mempool)

    balance = quantize_eight(credit_ledger - debit - fees + rewards)
    balance_no_mempool = float(credit_ledger) - float(debit_ledger) - float(fees) + float(rewards)
    return str(balance), str(credit_ledger), str(debit), str(fees), str(rewards), str(balance_no_mempool)
