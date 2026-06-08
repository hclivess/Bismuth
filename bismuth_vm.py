"""
bismuth_vm.py — a minimal DETERMINISTIC virtual machine: the execution core for the decentralized-apps
v2 protocol (doc/17, doc/19).

Smart-contract execution that two honest nodes MUST compute byte-identically, or the network forks — so
determinism is the entire game. This engine guarantees it by construction:

  * **integer-only, 256-bit words with modular (wrap-around) arithmetic** — there are no floats anywhere,
    so there is no platform-dependent rounding to disagree on;
  * **a pure function of (bytecode, calldata, context, storage)** — no wall-clock, no randomness, no I/O,
    no host callbacks; the same inputs always yield the same outputs on every machine;
  * **gas metering on every opcode** — a runaway or adversarial program runs out of gas and halts instead
    of hanging the chain (gas-exhaustion DoS defence). Since every op costs ≥1 gas, the gas limit bounds
    the step count, so loops cannot spin forever;
  * **bounded stack** and **validated jump destinations** (EVM-style JUMPDEST) — no unbounded growth, and
    you cannot jump into the middle of PUSH data to smuggle in opcodes.

This is the ENGINE ONLY — deliberately behind a flag and NOT consensus-active. A committed contract-state
trie, a deploy/call transaction format (openfield, like the token layer), and fork activation are the
next steps (doc/19). Foundations first; the determinism is the part you cannot retrofit, so it is locked
by tests now.
"""

WORD = 2 ** 256
MASK = WORD - 1
MAX_STACK = 1024

# --- opcodes (EVM-inspired numbering where it maps cleanly) ----------------------------------------
STOP = 0x00
ADD = 0x01
MUL = 0x02
SUB = 0x03
DIV = 0x04          # integer division; division by zero yields 0 (defined, not a trap)
MOD = 0x06
LT = 0x10
GT = 0x11
EQ = 0x14
ISZERO = 0x15
AND = 0x16
OR = 0x17
SHA256 = 0x20       # pop a word, push sha256(word as 32 bytes) -- Bitcoin-compatible HTLC preimage check
CALLER = 0x33       # push the caller identity (an int)
NUMBER = 0x43       # push the current block height (deterministic) -- HTLC timeout path
CALLVALUE = 0x34    # push the value sent with the call
CALLDATALOAD = 0x35 # pop offset, push the 32-byte word of calldata at that offset (zero-padded)
POP = 0x50
SLOAD = 0x54        # pop key, push storage[key] (0 if unset)
SSTORE = 0x55       # pop key, pop value, storage[key] = value
JUMP = 0x56         # pop dest, jump (dest must be a JUMPDEST)
JUMPI = 0x57        # pop dest, pop cond, jump if cond != 0
PC = 0x58           # push the current program counter
JUMPDEST = 0x5b     # a valid jump target (no-op otherwise)
PUSH = 0x60         # PUSH <len:1 byte> <len bytes, big-endian>: push that constant
DUP = 0x80          # duplicate the top stack item
SWAP = 0x90         # swap the top two stack items
RETURN = 0xf3       # pop value, return it as 32-byte output, success
REVERT = 0xfd       # halt, success=False (caller must discard storage changes)

_GAS = {
    STOP: 0, ADD: 3, MUL: 5, SUB: 3, DIV: 5, MOD: 5, LT: 3, GT: 3, EQ: 3, ISZERO: 3, AND: 3, OR: 3,
    SHA256: 60, CALLER: 2, CALLVALUE: 2, NUMBER: 2, CALLDATALOAD: 3, POP: 2, SLOAD: 100, SSTORE: 200,
    JUMP: 8, JUMPI: 10, PC: 2, JUMPDEST: 1, PUSH: 3, DUP: 3, SWAP: 3, RETURN: 0, REVERT: 0,
}


class VMError(Exception):
    """Deterministic execution fault (bad opcode, stack under/overflow, invalid jump). Reverts the call."""


class OutOfGas(VMError):
    pass


class VMResult:
    __slots__ = ("success", "output", "gas_used", "storage")

    def __init__(self, success, output, gas_used, storage):
        self.success = success    # False on REVERT / fault -> the caller must NOT commit `storage`
        self.output = output      # bytes returned by RETURN (b"" otherwise)
        self.gas_used = gas_used
        self.storage = storage    # the mutated storage dict; commit ONLY if success


def _jumpdests(code):
    """Pre-scan valid jump targets, skipping PUSH operand bytes so a jump can't land inside PUSH data."""
    dests, i, n = set(), 0, len(code)
    while i < n:
        op = code[i]
        if op == JUMPDEST:
            dests.add(i)
            i += 1
        elif op == PUSH:
            size = code[i + 1] if i + 1 < n else 0
            i += 2 + size
        else:
            i += 1
    return dests


def execute(code, calldata=b"", caller=0, callvalue=0, storage=None, gas_limit=1_000_000, block_height=0):
    """Run `code` deterministically and return a VMResult.

    `storage` is an optional {int: int} mapping; a COPY is mutated and returned, and the caller commits it
    only when `result.success`. Every fault is deterministic and reverts (success=False) — never a Python
    exception leaking platform behaviour.
    """
    storage = dict(storage or {})
    stack = []
    pc = 0
    gas = int(gas_limit)
    dests = _jumpdests(code)
    n = len(code)

    def push(v):
        if len(stack) >= MAX_STACK:
            raise VMError("stack overflow")
        stack.append(v & MASK)

    def pop():
        if not stack:
            raise VMError("stack underflow")
        return stack.pop()

    try:
        while pc < n:
            op = code[pc]
            cost = _GAS.get(op)
            if cost is None:
                raise VMError("invalid opcode 0x%02x at %d" % (op, pc))
            if gas < cost:
                raise OutOfGas("out of gas")
            gas -= cost
            pc += 1

            if op == STOP:
                return VMResult(True, b"", gas_limit - gas, storage)
            elif op == PUSH:
                size = code[pc] if pc < n else 0
                if size > 32 or pc + 1 + size > n:
                    raise VMError("bad PUSH")
                push(int.from_bytes(code[pc + 1:pc + 1 + size], "big"))
                pc += 1 + size
            elif op == POP:
                pop()
            elif op == ADD:
                push(pop() + pop())
            elif op == MUL:
                push(pop() * pop())
            elif op == SUB:
                a = pop(); b = pop(); push(a - b)
            elif op == DIV:
                a = pop(); b = pop(); push(0 if b == 0 else a // b)
            elif op == MOD:
                a = pop(); b = pop(); push(0 if b == 0 else a % b)
            elif op == LT:
                a = pop(); b = pop(); push(1 if a < b else 0)
            elif op == GT:
                a = pop(); b = pop(); push(1 if a > b else 0)
            elif op == EQ:
                push(1 if pop() == pop() else 0)
            elif op == ISZERO:
                push(1 if pop() == 0 else 0)
            elif op == AND:
                push(pop() & pop())
            elif op == OR:
                push(pop() | pop())
            elif op == SHA256:
                import hashlib
                push(int.from_bytes(hashlib.sha256(pop().to_bytes(32, "big")).digest(), "big"))
            elif op == CALLER:
                push(caller)
            elif op == CALLVALUE:
                push(callvalue)
            elif op == NUMBER:
                push(block_height)
            elif op == CALLDATALOAD:
                off = pop()
                word = calldata[off:off + 32] if off < len(calldata) else b""
                push(int.from_bytes(word.ljust(32, b"\x00"), "big"))
            elif op == SLOAD:
                push(storage.get(pop(), 0))
            elif op == SSTORE:
                key = pop(); val = pop(); storage[key] = val
            elif op == JUMP:
                dest = pop()
                if dest not in dests:
                    raise VMError("invalid jump to %d" % dest)
                pc = dest
            elif op == JUMPI:
                dest = pop(); cond = pop()
                if cond != 0:
                    if dest not in dests:
                        raise VMError("invalid jump to %d" % dest)
                    pc = dest
            elif op == PC:
                push(pc - 1)
            elif op == JUMPDEST:
                pass
            elif op == DUP:
                if not stack:
                    raise VMError("stack underflow")
                push(stack[-1])
            elif op == SWAP:
                if len(stack) < 2:
                    raise VMError("stack underflow")
                stack[-1], stack[-2] = stack[-2], stack[-1]
            elif op == RETURN:
                return VMResult(True, (pop()).to_bytes(32, "big"), gas_limit - gas, storage)
            elif op == REVERT:
                return VMResult(False, b"", gas_limit - gas, storage)
            else:
                raise VMError("unhandled opcode 0x%02x" % op)
        # ran off the end == STOP
        return VMResult(True, b"", gas_limit - gas, storage)
    except OutOfGas:
        return VMResult(False, b"", gas_limit, storage)   # all gas consumed, no state change
    except VMError:
        return VMResult(False, b"", gas_limit - gas, storage)


# --- tiny assembler helper (for tests / tooling — NOT consensus) -----------------------------------
def push(value, size=None):
    """Encode a PUSH of `value` (a non-negative int) as bytecode bytes."""
    if size is None:
        size = max(1, (value.bit_length() + 7) // 8)
    return bytes([PUSH, size]) + int(value).to_bytes(size, "big")
