"""
vm_engine.py — wire the deterministic RISC-V VM (bismuth_riscv) to the chain: parse ``vm:`` transactions,
execute contracts against the contract state store (vm_state), and (re)build that store from the ledger.

Transaction format (openfield, like the token layer):
  * deploy:  operation = "vm:deploy",  openfield = <RV32I code hex>
             -> the contract address is deterministically derived from the deploy tx CONTENT txid
                (blake2b of the 6-field pre-image), NOT the malleable post-fork signature.
  * call:    operation = "vm:call",    openfield = "<contract addr>:<calldata hex>"
             -> execute with caller = sender, callvalue = tx amount, the given calldata; storage changes
                commit ONLY on success (REVERT / out-of-gas commit nothing).

DETERMINISM: the address, the caller int, and execution are pure functions of on-chain data, so every
node derives identical state. Gated post-fork by the caller (digest).
"""
import hashlib

import amounts
import bismuth_riscv as rv
import bismuth_serialize

GAS_LIMIT = 1_000_000          # fixed per-call gas budget (gas economics are a later refinement)
ENGINE_NAME = "riscv"          # the single execution engine: deterministic RV32I (see bismuth_riscv)

# transactions table column indices (consensus row layout)
_BH, _TS, _ADDR, _RECIP, _AMOUNT, _SIG, _PUB, _HASH, _FEE, _REWARD, _OP, _OF = range(12)


def contract_address(seed):
    """Deterministic 56-hex contract address = blake2b-28 of `seed`. The deploy path passes the deploy
    tx's CONTENT txid (blake2b of the 6-field pre-image), NOT the signature: post-fork a single-sig
    recoverable signature is MALLEABLE, so a signature-derived address could be ground to different values
    for the same deploy (doc/18 §A.1 'final decision'). The content txid is immutable, so the address is a
    stable commitment to (deployer, recipient, amount, timestamp, code) and can't be ground."""
    return hashlib.blake2b(str(seed).encode(), digest_size=28).hexdigest()


def _tx_id_of(row):
    """Canonical content txid of a 12-field ledger row. IDENTICAL on the digest execution path
    (processor.block_transactions) and the rebuild path (SELECT * FROM transactions): both carry amount in
    the same storage form (integer units when LEDGER_INTEGER, else decimal), and amounts.ledger_value
    normalises both back to the signed '%.8f' string — matching essentials.format_raw_tx, so the deploy
    address derived here equals the tx's canonical txid the explorer/API surfaces."""
    amount_str = '%.8f' % amounts.ledger_value(row[_AMOUNT])
    # Stage 2 (doc/29): the VM is post-fork-only, so the canonical txid is the BINARY-pre-image tx_id_v2_s.
    return bismuth_serialize.tx_id_v2_s(str(row[_TS]), str(row[_ADDR]), str(row[_RECIP]),
                                        amount_str, str(row[_OP]), str(row[_OF]))


def _caller_int(address):
    """Fold a Bismuth address to an int for the VM's CALLER (RV32I truncates it to 32 bits)."""
    try:
        return int(str(address), 16)
    except Exception:
        return int.from_bytes(hashlib.blake2b(str(address).encode(), digest_size=32).digest(), "big")


# The ledger pseudo-address that holds all VM custody. A vm:call SENT to it deposits its BIS into the
# called contract's balance; payouts are settled FROM it. Its ledger balance mirrors the sum of all
# contract balances (double-entry), so the BIS supply stays exact and rolls back with the ledger.
VM_SINK = hashlib.sha224(b"bismuth-vm-custody").hexdigest()   # 56-hex sink address; no private key -> unspendable


def _deploy(state, openfield, deploy_id):
    try:
        of = (openfield or "").strip()
        if of.startswith("riscv:"):                  # tolerate an explicit prefix; RV32I is the only engine
            of = of[6:].strip()
        state.deploy(contract_address(deploy_id), bytes.fromhex(of))
    except Exception:
        pass


def _call(state, openfield, signature, sender, recipient, amount_units, block_height):
    """Execute one vm:call. Returns the payouts [(to_addr, amount_units)] it queued."""
    deposit = 0
    addr = None
    try:
        parts = (openfield or "").split(":", 1)
        addr = parts[0].strip()
        calldata = bytes.fromhex(parts[1].strip()) if len(parts) > 1 and parts[1].strip() else b""
        stored = state.get_code(addr)
        if not stored:
            # No such contract -> nothing executes. A value-bearing call must NOT keep the BIS the ledger
            # already moved to the sink (it would be unowned), so refund the sender.
            dep = int(amount_units or 0) if recipient == VM_SINK else 0
            return [(str(sender), dep)] if dep else []
        # DEPOSIT: value carried to the custody sink funds this contract (the ledger already moved it). Credit
        # custody BEFORE execution so the contract sees it in callvalue AND self_balance (EVM-style).
        deposit = int(amount_units or 0) if recipient == VM_SINK else 0
        if deposit > 0xFFFFFFFF:
            # The VM exposes callvalue as a 32-bit word (rv SYS_CALLVALUE returns callvalue & 0xFFFFFFFF), so a
            # deposit that does not fit 32 bits cannot be honestly accounted by ANY contract: it would see a
            # truncated callvalue and strand the high bits in custody, breaking the custody == sum(contract
            # balances) double-entry. Refund the sender and do not execute (the ledger already moved it to the
            # sink) — same shape as the no-contract / revert refund paths. Found by the AMM audit (doc/24 §2).
            return [(str(sender), deposit)]
        if deposit:
            state.add_balance(addr, deposit)
        result = rv.execute(stored, calldata=calldata, caller=_caller_int(sender),
                            callvalue=deposit, storage=state.load_storage(addr), gas_limit=GAS_LIMIT,
                            block_height=int(block_height or 0), self_balance=state.get_balance(addr))
        if not result.success:
            # REVERT: storage is untouched, so the contract has NO record of the deposit — leaving it in
            # custody would strand it (no slot owns it). Undo the custody credit and refund the sender, so
            # the BIS supply stays exact and the sink's balance still mirrors the sum of contract balances.
            if deposit:
                state.add_balance(addr, -deposit)
                return [(str(sender), deposit)]
            return []
        state.commit_storage(addr, result.storage)
        payouts = []
        for to_int, amount in result.transfers:
            to_addr = "%056x" % (to_int & ((1 << 224) - 1))             # 56-hex address from the VM word
            state.add_balance(addr, -int(amount))                       # debit custody (VM already checked)
            payouts.append((to_addr, int(amount)))
        return payouts
    except Exception:
        # Any failure after the deposit was credited is an implicit revert: undo the credit and refund, so
        # a malformed call can't strand attached value either.
        if deposit and addr is not None:
            try:
                state.add_balance(addr, -deposit)
            except Exception:
                pass
            return [(str(sender), deposit)]
        return []


def apply_block_rows(state, rows):
    """Execute the block's vm: txs in order against `state`, with value custody. A vm:call to VM_SINK
    deposits its amount into the called contract; a TRANSFER debits that custody and queues a payout.
    Returns the payouts [(to_addr, amount_units)] for the digester to settle FROM the sink. Custody lives
    in vm_state (committed to the state root), so re-executing these txs replays the same balances — the
    rollback rebuild is deterministic."""
    payouts = []
    for r in rows:
        op = r[_OP] or ""
        if op == "vm:deploy":
            _deploy(state, r[_OF] or "", _tx_id_of(r))   # content txid, not the malleable signature
        elif op == "vm:call":
            payouts.extend(_call(state, r[_OF] or "", r[_SIG], r[_ADDR], r[_RECIP],
                                 r[_AMOUNT], int(r[_BH] or 0)))
    return payouts


# --- state-root enforcement in the coinbase (doc/19) -----------------------------------------------
# Post-fork, the miner COMMITS the pre-state VM root in the coinbase openfield, ahead of the PoW nonce,
# so a peer can RE-COMPUTE its own root and REJECT the block on a mismatch — turning a silent state
# divergence into a caught block-rejection. The marker keeps it distinguishable from the hf2 signal.
COINBASE_ROOT_MARKER = "vmsr"
_ROOT_HEX = 64   # blake2b(digest_size=32) -> 64 hex chars


def embed_state_root(root, rand_hex=""):
    """Coinbase-openfield seed committing `root` (the pre-state VM root), then PoW-nonce entropy."""
    return COINBASE_ROOT_MARKER + str(root) + str(rand_hex)


def extract_state_root(openfield):
    """The committed VM state root from a coinbase openfield, or None if absent/malformed. The marker is
    located by search, so the root can ride AFTER the hf2 signal in the same openfield."""
    of = openfield or ""
    i = of.find(COINBASE_ROOT_MARKER)
    if i >= 0:
        start = i + len(COINBASE_ROOT_MARKER)
        if len(of) >= start + _ROOT_HEX:
            return of[start:start + _ROOT_HEX]
    return None


def rebuild(state, cursor, fork_height, max_height):
    """Rebuild the whole contract state by re-executing every vm: tx from fork activation up to
    `max_height`, in chain order (startup + after a reorg). Deterministic by construction."""
    state.clear()
    rows = cursor.execute(
        "SELECT * FROM transactions WHERE operation LIKE 'vm:%' AND block_height >= ? "
        "AND block_height <= ? ORDER BY block_height ASC, rowid ASC",
        (int(fork_height), int(max_height))).fetchall()
    apply_block_rows(state, rows)
    return len(rows)
