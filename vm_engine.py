"""
vm_engine.py — wire the deterministic VM (bismuth_vm) to the chain: parse ``vm:`` transactions, execute
contracts against the contract state store (vm_state), and (re)build that store from the ledger.

Transaction format (openfield, like the token layer):
  * deploy:  operation = "vm:deploy",  openfield = <bytecode hex>
             -> the contract address is deterministically derived from the deploy tx signature.
  * call:    operation = "vm:call",    openfield = "<contract addr>:<calldata hex>"
             -> execute with caller = sender, callvalue = tx amount, the given calldata; storage changes
                commit ONLY on success (REVERT / out-of-gas commit nothing).

DETERMINISM: the address, the caller int, and execution are pure functions of on-chain data, so every
node derives identical state. Gated post-fork by the caller (digest).
"""
import hashlib

import bismuth_vm as vm

GAS_LIMIT = 1_000_000          # fixed per-call gas budget (gas economics are a later refinement)

# transactions table column indices (consensus row layout)
_BH, _TS, _ADDR, _RECIP, _AMOUNT, _SIG, _PUB, _HASH, _FEE, _REWARD, _OP, _OF = range(12)


def contract_address(signature):
    """Deterministic 56-hex contract address from the deploy tx signature (unique per deploy)."""
    return hashlib.blake2b(str(signature).encode(), digest_size=28).hexdigest()


def _caller_int(address):
    """Fold a Bismuth address (56 hex chars) to a 256-bit int deterministically."""
    try:
        return int(str(address), 16) & vm.MASK
    except Exception:
        return int.from_bytes(hashlib.blake2b(str(address).encode(), digest_size=32).digest(), "big")


def process(state, operation, openfield, signature, sender, amount_units):
    """Process one vm: transaction against `state`. Returns (kind, addr, success). Never raises on bad
    user input — a malformed contract/call is a failed no-op, like any reverting tx."""
    try:
        if operation == "vm:deploy":
            addr = contract_address(signature)
            state.deploy(addr, bytes.fromhex((openfield or "").strip()))
            return ("deploy", addr, True)
        if operation == "vm:call":
            parts = (openfield or "").split(":", 1)
            addr = parts[0].strip()
            calldata = bytes.fromhex(parts[1].strip()) if len(parts) > 1 and parts[1].strip() else b""
            code = state.get_code(addr)
            if not code:
                return ("call", addr, False)
            storage = state.load_storage(addr)
            result = vm.execute(code, calldata=calldata, caller=_caller_int(sender),
                                callvalue=int(amount_units or 0), storage=storage, gas_limit=GAS_LIMIT)
            if result.success:
                state.commit_storage(addr, result.storage)
            return ("call", addr, result.success)
    except Exception:
        return (None, None, False)
    return (None, None, False)


def apply_block_rows(state, rows):
    """Execute every vm: tx in a block's rows, in order. `rows` are full 12-col transaction tuples."""
    for r in rows:
        op = r[_OP] or ""
        if op.startswith("vm:"):
            process(state, op, r[_OF] or "", r[_SIG], r[_ADDR], r[_AMOUNT])


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
