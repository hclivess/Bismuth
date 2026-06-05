"""
Authoritative, mempool-aware address balance for the ``balanceget*`` socket commands, lifted out of
``node.py``. Unlike ``balance_cache``/``ledger_queries`` (height-keyed authoritative cache and integer
quick-checks), this reproduces the exact legacy six-tuple the wire protocol returns
(balance, credit, debit, fees, rewards, balance_no_mempool) and folds in pending mempool debits. Amounts
are read mode-aware via ``amounts.ledger_value``. Takes ``node`` explicitly (only needs
``node.last_block`` for the mempool fee calc), so it is a clean leaf with no import cycle.
"""
from decimal import Decimal

import amounts
import mempool as mp
from essentials import fee_calculate
from quantizer import quantize_eight


def balanceget(node, balance_address, db_handler):
    # TODO: To move in db_handler, call by db_handler.balance_get(address)
    # verify balance

    # node.logger.app_log.info("Mempool: Verifying balance")
    # node.logger.app_log.info("Mempool: Received address: " + str(balance_address))

    base_mempool = mp.MEMPOOL.mp_get(balance_address)

    # include mempool fees

    debit_mempool = 0
    if base_mempool:
        for x in base_mempool:
            debit_tx = Decimal(x[0])
            fee = fee_calculate(x[1], x[2], node.last_block)
            debit_mempool = quantize_eight(debit_mempool + debit_tx + fee)
    else:
        debit_mempool = 0
    # include mempool fees

    credit_ledger = Decimal("0")

    # HARDFORK / cleanup (doc/16): this O(history) balance reads each amount/fee/reward and converts it
    # with amounts.ledger_value (mode-aware: integer units -> Decimal, else legacy quantize). The bare
    # `except: <value> = 0` guards below are a code smell — a conversion error silently ZEROES a balance
    # instead of failing loudly. Replace with the maintained integer balance index (phase 4) and narrow
    # the exception handling once amounts are integer end-to-end.
    try:
        db_handler.execute_param(db_handler.h, "SELECT amount FROM transactions WHERE recipient = ?;", (balance_address,))
        entries = db_handler.h.fetchall()
    except:
        entries = []

    try:
        for entry in entries:
            credit_ledger = quantize_eight(credit_ledger) + amounts.ledger_value(entry[0])
            credit_ledger = 0 if credit_ledger is None else credit_ledger
    except:
        credit_ledger = 0

    fees = Decimal("0")
    debit_ledger = Decimal("0")

    try:
        db_handler.execute_param(db_handler.h, "SELECT fee, amount FROM transactions WHERE address = ?;", (balance_address,))
        entries = db_handler.h.fetchall()
    except:
        entries = []

    try:
        for entry in entries:
            fees = quantize_eight(fees) + amounts.ledger_value(entry[0])
            fees = 0 if fees is None else fees
    except:
        fees = 0

    try:
        for entry in entries:
            debit_ledger = debit_ledger + amounts.ledger_value(entry[1])
            debit_ledger = 0 if debit_ledger is None else debit_ledger
    except:
        debit_ledger = 0

    debit = quantize_eight(debit_ledger + debit_mempool)

    rewards = Decimal("0")

    try:
        db_handler.execute_param(db_handler.h, "SELECT reward FROM transactions WHERE recipient = ?;", (balance_address,))
        entries = db_handler.h.fetchall()
    except:
        entries = []

    try:
        for entry in entries:
            rewards = quantize_eight(rewards) + amounts.ledger_value(entry[0])
            rewards = 0 if str(rewards) == "0E-8" else rewards
            rewards = 0 if rewards is None else rewards
    except:
        rewards = 0

    balance = quantize_eight(credit_ledger - debit - fees + rewards)
    balance_no_mempool = float(credit_ledger) - float(debit_ledger) - float(fees) + float(rewards)
    # node.logger.app_log.info("Mempool: Projected transction address balance: " + str(balance))
    return str(balance), str(credit_ledger), str(debit), str(fees), str(rewards), str(balance_no_mempool)
