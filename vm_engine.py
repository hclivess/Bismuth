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
import bismuth_riscv as rv

GAS_LIMIT = 1_000_000          # fixed per-call gas budget (gas economics are a later refinement)

# Pluggable execution engines (the framework is engine-agnostic). A contract is deployed with one engine
# and always runs under it. A 1-byte tag is stored ahead of the code; the openfield "riscv:<hex>" selects
# RISC-V, otherwise the compact bytecode VM. Both satisfy the same execute()/Result contract.
ENGINE_BYTECODE, ENGINE_RISCV = 0, 1
_ENGINES = {ENGINE_BYTECODE: vm, ENGINE_RISCV: rv}
ENGINE_NAME = {ENGINE_BYTECODE: "bytecode", ENGINE_RISCV: "riscv"}

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


def process(state, operation, openfield, signature, sender, amount_units, block_height=0):
    """Process one vm: transaction against `state`. Returns (kind, addr, success). Never raises on bad
    user input — a malformed contract/call is a failed no-op, like any reverting tx."""
    try:
        if operation == "vm:deploy":
            of = (openfield or "").strip()
            engine = ENGINE_BYTECODE
            if of.startswith("riscv:"):
                engine, of = ENGINE_RISCV, of[6:].strip()
            addr = contract_address(signature)
            state.deploy(addr, bytes([engine]) + bytes.fromhex(of))   # 1-byte engine tag + code
            return ("deploy", addr, True)
        if operation == "vm:call":
            parts = (openfield or "").split(":", 1)
            addr = parts[0].strip()
            calldata = bytes.fromhex(parts[1].strip()) if len(parts) > 1 and parts[1].strip() else b""
            stored = state.get_code(addr)
            if not stored:
                return ("call", addr, False)
            engine = _ENGINES.get(stored[0], vm)                       # dispatch on the stored engine tag
            storage = state.load_storage(addr)
            result = engine.execute(stored[1:], calldata=calldata, caller=_caller_int(sender),
                                    callvalue=int(amount_units or 0), storage=storage, gas_limit=GAS_LIMIT,
                                    block_height=int(block_height or 0))
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
            process(state, op, r[_OF] or "", r[_SIG], r[_ADDR], r[_AMOUNT], int(r[_BH] or 0))


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
