# doc/44 — Writing Bismuth smart contracts (complete reference)

The complete, authoritative guide to authoring contracts for the Bismuth decentralized-apps VM: the
execution model, the full syscall ABI, storage, value custody, **contract composition and upgradeability**
(`CALL` / `DELEGATECALL` / `SETCODE` / `SELFDESTRUCT`, issue #384), the security guardrails, and how to
build, test and deploy. doc/19 is the consensus-level *map of what exists*; **this is the developer manual**.

> **Status / gating.** Everything here is **post-fork-only** (hf2) and behind the `vm` config flag (default
> **off**; on for regnet tests). hf2 has not activated on mainnet, so there are no live contracts yet — the
> whole layer is inert until then and adds no behaviour to the current chain (doc/19 §Fork gating).

---

## 1. The model in one paragraph

A contract is a blob of **RV32I (32-bit RISC-V) machine code** deployed at a deterministic address. Calling
it runs that code in a sandboxed, gas-metered, **deterministic** interpreter (`bismuth_riscv.py`): integer
math only, no clock, no randomness, no host I/O. The contract talks to the chain **only** through `ECALL`
syscalls — read/write its key/value storage, read the caller / attached value / block height, hash bytes,
move BIS it holds, call other contracts, replace its own code, or self-destruct. State (code, storage,
custody balances) lives in `vm_state` and is committed to a consensus `state_root`, so every node computes
identical results and a divergence is a caught block-rejection (doc/19).

Contracts are written by hand in RV32I today (a C/Rust → RISC-V toolchain is future DX, doc/19 §Next steps).
The in-tree assembler `contracts/asmtools.py` (labels, `li`, multiply/divide macros, syscall helpers) makes
this tractable; the shipped demos in `contracts/` are the worked examples.

---

## 2. Lifecycle: deploy, address, call

**Deploy** — one transaction:

| field | value |
|---|---|
| `operation` | `vm:deploy` |
| `openfield` | the contract bytecode as **hex** (an optional `riscv:` prefix is tolerated) |

The **contract address** is `blake2b-28` of the deploy tx's **content txid** (the 6-field pre-image), *not*
the malleable signature — so it is a stable, un-grindable commitment to `(deployer, recipient, amount,
timestamp, code)` (`vm_engine.contract_address`). Two deploys can never collide (different txids → different
addresses), which is why a self-destructed address can never be resurrected.

**Call** — one transaction:

| field | value |
|---|---|
| `operation` | `vm:call` |
| `openfield` | `"<contract_addr>:<calldata_hex>"` |
| `recipient` / `amount` | to **attach value**, send `amount` to `VM_SINK` (see §6); otherwise any recipient, `amount = 0` |

Storage changes **commit only on success**; a `REVERT` / out-of-gas / fault commits nothing (and refunds any
attached value). Reads of contract state are surfaced by the node API: `/api/vm/contracts` and
`/api/vm/contract/{addr}` (code, every storage slot, custody balance).

---

## 3. Execution model

* **ISA**: RV32I base integer — `ADD/SUB/SLL/SLT/SLTU/XOR/SRL/SRA/OR/AND` (+ immediates), loads/stores
  (`LB/LH/LW/LBU/LHU/SB/SH/SW`), branches (`BEQ/BNE/BLT/BGE/BLTU/BGEU`), `LUI/AUIPC/JAL/JALR`, `ECALL`.
  **No `M` extension** (no hardware multiply/divide) — use `asmtools` `mulu`/`divu`/`mulu64`/`divu64` macros.
* **Words are 32-bit two's-complement.** Values wider than 32 bits (BIS amounts, 224-bit addresses) live in
  memory as big-endian byte fields and are passed by pointer.
* **Memory**: a fresh, zeroed `1 << 16` (64 KiB) byte array per call. Code is loaded at address `0`; the
  calldata is copied in right after (4-byte aligned). Every guest access is bounds-checked (an out-of-bounds
  load/store is a deterministic revert).
* **Calldata ABI**: on entry `a0` = calldata pointer, `a1` = calldata length. By convention the first 4
  bytes are a **big-endian selector**; arguments follow as big-endian fields (4-byte words; 28-byte
  addresses; 8-byte amounts). Return one 32-bit word big-endian via `SYS_RETURN` (`a0`).
* **Registers** (standard RISC-V names, `asmtools` exports them): `zero=x0`, `ra=x1`, `sp=x2`,
  `a0..a7 = x10..x17`, `t0..t6 = x5,x6,x7,x28,x29,x30,x31`, `s0..s3 = x8,x9,x18,x19`. The syscall number goes
  in `a7`; arguments in `a0..`; the result comes back in `a0`.
* **Gas**: a flat `GAS_LIMIT = 1_000_000` budget **per top-level call**, **shared** across the whole
  call-tree (a `CALL`/`DELEGATECALL` spends from the same budget). 1 gas per instruction, plus op surcharges
  (`SHA256` 60, `CALL`/`DELEGATECALL` 100, `SETCODE` 50 + 1/byte, `SELFDESTRUCT` 50). Running out is a
  revert, never a hang. A flat `vm:` fee surcharge prices calls economically today; a per-op gas market is
  future work (doc/19).
* **Determinism (the consensus contract)**: execution is a pure function of `(code, calldata, caller,
  callvalue, block_height, the host's committed state)`. No time, no randomness, no host state. Any fault is
  a clean revert, never a leaked exception.

---

## 4. Syscall ABI — the complete reference

Syscall number in `a7`; the host mediates all of these (`bismuth_riscv.execute`). “ptr” means a memory
offset; multi-byte fields are **big-endian**.

| # | name | args | returns / effect |
|---|---|---|---|
| 0 | `HALT` | — | end successfully, no return data |
| 1 | `RETURN` | `a0=word` | end successfully; output = `a0` as 4 bytes BE |
| 2 | `SSTORE` | `a0=key, a1=val` | `storage[key] = val` (32-bit each; `0` == unset) |
| 3 | `SLOAD` | `a0=key` → `a0` | `a0 = storage[key]` (0 if unset) |
| 4 | `CALLER` | → `a0` | the caller's 32-bit fold (a contract address fold under `CALL`; the original sender under `DELEGATECALL`) |
| 5 | `CALLVALUE` | → `a0` | BIS attached to this call (32-bit; in base units) |
| 6 | `NUMBER` | → `a0` | current block height |
| 7 | `SHA256` | `a0=ptr, a1=len, a2=out` | `mem[out:out+32] = sha256(mem[ptr:ptr+len])` |
| 8 | `TRANSFER` | `a0=ptr 28-byte recipient, a1=ptr 8-byte amount` | pay BIS the contract holds **out** to an external address → `a0 = 1` if affordable else `0` |
| 9 | `CALL` | `a0=ptr 28-byte callee, a1=ptr 8-byte value, a2=cd ptr, a3=cd len, a4=ret ptr, a5=ret len` | call another contract in **its** storage/custody; forwards `value`; copies up to `ret len` bytes of the callee's RETURN word to `ret ptr` → `a0 = 1`/`0` |
| 10 | `DELEGATECALL` | `a0=ptr 28-byte impl, a1=cd ptr, a2=cd len, a3=ret ptr, a4=ret len` | run the impl's **code** in **our** storage/custody/identity (`CALLER`+`CALLVALUE` preserved, no value moves) → `a0 = 1`/`0` |
| 11 | `SETCODE` | `a0=ptr new code, a1=code len` | replace **our own** code (effective next call) → `a0 = 1`/`0` |
| 12 | `SELFDESTRUCT` | `a0=ptr 28-byte beneficiary` | pay our remaining custody to the beneficiary; delete our code + storage; **halts (success)** |
| 13 | `ADDRESS` | `a0=out ptr` | write our own 28-byte address to `out ptr`; also `a0 = `its 32-bit fold |
| 14 | `CALLER_FULL` | `a0=out ptr` | write the **full 28-byte caller** to `out ptr` — the collision-resistant identity (`CALLER` only gives the grindable low 32 bits) |

`asmtools.Asm` has a helper for each: `.halt() .ret() .sstore(kr,vr) .sload_to(dr,kr) .caller(dr)
.callvalue(dr) .number(dr) .transfer(addr_ptr,amt_ptr) .call(callee,val,cd,cdl,ret,rl)
.delegatecall(impl,cd,cdl,ret,rl) .setcode(ptr,len) .selfdestruct(benef_ptr) .address(out_ptr)
.caller_full(out_ptr)`.

---

## 5. Storage

A per-contract map of **32-bit key → 32-bit value**. `SSTORE`/`SLOAD` are the only access. A value of `0`
is identical to *unset* (the store keeps itself compact by deleting `0` slots) — so a slot you have never
written and a slot you set to `0` read the same. Conventions the demos use:

* **Fixed slots** for scalars: `S_INIT = 1`, `S_AMOUNT = 1`, `S_OWNER = …`.
* **Tagged slots** for maps: `key = TAG | (id & MASK)`, e.g. the token's `TAG_BAL = 0x40000000 |
  (account_id & 0x0FFFFFFF)`.
* **Multi-word fields**: a 28-byte address is 7 consecutive word slots, big-endian (e.g. the proxy's
  `S_IMPL0..S_IMPL0+6`).

The layout is **fixed at deploy** — there is no dynamic allocation. Choose your slot map up front and keep
it stable across upgrades (see §8).

---

## 6. Value & custody (real BIS)

Contracts hold and move real BIS, and a reorg can't corrupt the supply. Custody is held **inside `vm_state`**
(committed to `state_root`), *not* the ledger, so it is rollback-deterministic:

* **Deposit** — a `vm:call` whose recipient is `VM_SINK` (a keyless 56-hex sink) moves `amount` from the
  sender to the sink at the ledger level, and credits the called contract's `vm_state` custody **before**
  execution, so the contract sees it in both `CALLVALUE` and its self-balance. The sink's ledger balance
  always mirrors `sum(contract custody)` — double-entry, so the BIS supply stays exact.
* **Withdraw (external)** — `SYS_TRANSFER` debits the contract's custody and queues a **payout**; the
  digester settles it as a `vm:payout` negative-height ledger row from the sink to the recipient (the
  reward-mirror pattern). Amount is a 64-bit big-endian field (registers are 32-bit).
* **Internal move** — value forwarded by `SYS_CALL` moves custody caller→callee **inside `vm_state`**; the
  sink total is unchanged, so it is *not* a ledger payout.
* **Refund on revert** — if the call reverts (or the contract doesn't exist, or the attached value exceeds
  32 bits and can't be honestly represented as `CALLVALUE`), the deposit is refunded to the sender and
  nothing is stranded.

`CALLVALUE` is a 32-bit word, so a single call can attach at most `0xFFFFFFFF` base units (~42.9 BIS) of
*visible* value; larger deposits are refunded. `SYS_TRANSFER` and `SYS_CALL` value fields are 64-bit, so a
contract can *hold* and pay out more than that across many deposits.

---

## 7. Composition: `CALL` and `DELEGATECALL`

A single top-level `vm:call` and everything it calls execute against **one journaled host**
(`vm_engine.Host`): an in-memory working set seeded lazily from `vm_state`. This is what makes cross-contract
calls atomic and re-entrancy-correct.

* **`SYS_CALL`** runs the **callee** in the **callee's** storage and custody. The callee sees `CALLER` = the
  calling contract, `CALLVALUE` = the forwarded value. The callee's RETURN word is copied back into your
  return buffer; `a0` is `1` on success, `0` on any failure (revert / out-of-gas / no such contract / depth
  cap / insufficient balance). **A failed inner call reverts only the inner frame** — its storage writes and
  value move are rolled back, your frame keeps running and decides what to do with the `0`.
* **`SYS_DELEGATECALL`** runs the **impl's code** in **your** storage, custody and identity: `CALLER` and
  `CALLVALUE` are the values *you* were called with (preserved through delegation), and the impl's
  `SSTORE`/`TRANSFER`/`SETCODE`/`SELFDESTRUCT` all act on **your** address. No value moves (it's already
  yours). This is the proxy primitive (§8).

**Re-entrancy** — storage is keyed by address and shared **live** across frames, so if A calls B and B
re-enters A, A's later `SLOAD`s see B's writes — exactly as on the EVM, **including the re-entrancy bug
class**. Apply the same discipline: checks-effects-interactions, or a re-entrancy guard slot.

**Depth & gas** — the call-tree is capped at `MAX_CALL_DEPTH = 16` (a deeper `CALL`/`DELEGATECALL` returns
`0` without recursing; the cap fires before any Python recursion limit, so it is the consensus constant). The
whole tree spends one `GAS_LIMIT` budget; a sub-call charges its gas to the caller, so an expensive callee
can exhaust your budget.

**Calling convention in practice** (forwarder excerpt, `contracts/` style):

```python
a.mv(T1, S0)             # callee ptr   = calldata base (a 28-byte address sits there)
a.addi(T2, S0, 28)       # value ptr    (8 bytes)
a.addi(T3, S0, 36)       # inner cd ptr
a.li(T4, 8)              # inner cd len
a.li(T5, 0x1400)         # return buffer
a.li(S1, 4)              # return len
a.call(T1, T2, T3, T4, T5, S1)   # a0 = 1 on success
```

---

## 8. Upgradeability — two paths

Deployed code does **not** change unless the contract opts in. Two mechanisms:

### 8a. Proxy + swappable implementation (`DELEGATECALL`)

A thin **proxy** at a fixed address owns the storage and an `impl_addr`; every ordinary call is
`DELEGATECALL`ed into the current implementation, which reads/writes the proxy's slots. You "upgrade" by an
owner-gated write of a new `impl_addr` — **address, storage and custody stay; only the logic moves.** This is
the EVM proxy pattern. Reference implementation: **`contracts/upgradeable.py`** (proxy + `impl_v1` + `impl_v2`;
`tests/test_vm_upgrade.py` upgrades it live on-chain). Its control selectors are reserved high values
(`ADMIN_INIT = 0xF0000000`, `ADMIN_UPGRADE = 0xF0000001`); everything else forwards to the impl.

```
proxy storage:  0 = init flag · 1..7 = impl address (7 words) · >=0x100 = application state
upgrade:        owner sends ADMIN_UPGRADE | new_impl(28)  ->  proxy rewrites slots 1..7
ordinary call:  any other selector  ->  DELEGATECALL impl with the full calldata
```

**Storage-layout discipline (mandatory):** the implementation must not touch the proxy's control slots
(`0..7` above) and **v2 must not reinterpret v1's slots** — the classic proxy storage-collision hazard. Keep
application state in a disjoint, stable slot range (the demo uses `>= 0x100`).

### 8b. In-place self-upgrade (`SETCODE`)

A contract replaces its **own** bytecode with `SYS_SETCODE` — no proxy, no second contract. The new code is
effective on the **next** call (the current frame finishes on the old code). Simplest when one contract
upgrades itself; gate it with an owner check:

```python
a.mv(S0, A0); a.mv(S1, A1)                 # save calldata ptr/len (the new code) before clobbering a0
a.caller(T0); a.li(T1, owner_id); a.bne(T0, T1, "revert")   # owner only
a.setcode(S0, S1)                          # replace own code with the calldata
a.li(A0, 1); a.ret()
```

A contract can only set **its own** code — never another contract's. Under a `DELEGATECALL` the executing
identity is the *proxy*, so a `SETCODE` (or `SELFDESTRUCT`) inside a delegated impl rewrites/destroys the
**proxy** — powerful, and the Parity-wallet hazard: only delegate into code you trust.

---

## 8c. Ownership — optional, transferable, delegable (`contracts/ownable.py`)

Like the EVM, the VM has **no built-in owner** — ownership is an application convention. `contracts/ownable.py`
packages the standard one so any contract can splice it in. It is **optional and immutable by default**: a
contract that doesn't use it has no owner and no admin surface at all.

* **Bound at inception** — `build(owner_addr, …)` bakes the initial owner into the bytecode, so the contract
  is owned by a chosen account from block 0 (no init call needed). Pass a *different* address to bind it to
  someone else.
* **Transferable (two-step)** — the owner sends `OWN_TRANSFER | new_owner(28)` to propose; the new owner must
  send `OWN_ACCEPT` to claim it. Two-step is deliberate: on an irreversible chain a one-step transfer to a
  typo'd or keyless address permanently bricks admin. (OpenZeppelin's `Ownable2Step`.)
* **Delegable** — the owner sets a revocable `OWN_SET_DELEGATE | delegate(28)` (all-zero clears). The delegate
  may exercise whatever the contract gates with `require_owner_or_delegate` (operational powers — e.g. a proxy
  upgrade) but **cannot** transfer, renounce, or change the delegate (those are owner-only). This is the
  "bind operational control to another account while keeping ownership" case.
* **Renounceable → immutable** — the owner sends `OWN_RENOUNCE` to make the contract permanently ownerless;
  the explicit "freeze it now" switch.

**Identity is the full 224-bit address.** Owner/delegate checks read the caller via **`SYS_CALLER_FULL`** (28
bytes), *not* the 32-bit `SYS_CALLER` fold — the fold is only ~2³²-strong and an attacker can grind keypairs
to match it, which is unacceptable for ownership that gates upgrades or value. (This also means existing
contracts that gate on `SYS_CALLER` alone are only 32-bit-strong; fine for low-value admin, weak for
high-value — prefer the full-address check for anything that controls funds or code.)

**Using it** — emit the helpers in your dispatch:

```python
import ownable as own
a.mv(S0, A0); _ld_be32(a, T0, S0, 0)                     # save calldata base; load selector
own.emit_admin_dispatch(a, owner_int, T0, S0, "app")     # OWN_* handled here; else fall through to "app"
a.label("app")
# ... your selectors ...
a.label("privileged")
own.emit_require_owner_or_delegate(a, owner_int, "revert")   # or emit_require_owner for owner-only
# ... privileged logic ...
```

**Reserved ranges (don't collide):** storage slots `0xFFFF0000..0xFFFF001F` (owner/pending/delegate/renounced)
and call selectors `0xF0010000..0xF0010003`. A `DELEGATECALL` implementation shares the proxy's storage, so an
impl must **not** independently use ownable's reserved slots unless it intends to share the proxy's owner.

The upgradeable proxy (§8a) uses this: `INIT` is owner-only, `UPGRADE` is owner-or-delegate, and the proxy is
transferable/renounceable like any owned contract. Tests: `tests/test_ownable.py` (transfer, accept guards,
renounce→immutable, delegate scope, and a **fold-collision-rejected** case proving the full-address check).

## 9. Self-destruct

`SYS_SELFDESTRUCT(beneficiary)` pays the contract's **remaining custody** to the beneficiary (as a normal
external payout) and **deletes its code and storage**, then halts successfully. After it, a call to that
address finds no contract (a value-bearing call is refunded). Because addresses are content-derived, the
address can never be re-deployed — no resurrection. If the enclosing top-level call reverts, the destruction
is rolled back with everything else (`tests/test_vm_flex.py::test_selfdestruct_rolls_back_when_outer_frame_reverts`).
Gate it with an owner/role check just like any irreversible action.

> **Beneficiary must be a payable (key-holding) address.** The payout is a ledger credit to the beneficiary's
> *ledger* balance — exactly like `SYS_TRANSFER`. Sending it to a **contract** address (or any keyless
> address) is supply-exact but strands the BIS there unspendably, and does **not** credit that contract's
> *custody*. To move value *into* another contract, use `SYS_CALL` with a `value` (which credits custody);
> reserve `TRANSFER`/`SELFDESTRUCT` beneficiaries for real, key-holding accounts.

---

## 10. Security guardrails (read before shipping)

The flexibility ops bring the EVM's power **and its footguns**. The engine enforces determinism, bounds,
gas, depth and supply-exactness; **everything else is the contract author's job.**

1. **Access-control every irreversible/privileged path** — `SETCODE`, `SELFDESTRUCT`, proxy `UPGRADE`, fund
   withdrawals. Gate them with an owner check (use `contracts/ownable.py`). An ungated `SETCODE` lets
   *anyone* brick or replace your contract.
2. **Use full-address identity for high-stakes access control.** `SYS_CALLER` exposes only the low 32 bits of
   the caller — grindable (~2³²) for a determined attacker. For ownership / upgrade / fund control, check the
   full 28 bytes via `SYS_CALLER_FULL` (which `ownable.py` does). The 32-bit fold is fine only for low-value
   bookkeeping.
3. **Re-entrancy** — storage is live across frames. Apply checks-effects-interactions (update storage
   *before* you `CALL`/`TRANSFER` out) or a guard slot. Treat every `CALL` to an unknown contract as
   potentially re-entering you.
4. **Delegate only into trusted code** — a `DELEGATECALL` impl runs with *your* identity and can
   `SSTORE`/`SETCODE`/`SELFDESTRUCT` *your* contract. The impl address is as trusted as your own code.
5. **Proxy storage discipline** — disjoint, stable slot ranges for control vs application state; never let
   v2 reinterpret v1's slots (§8a), and keep clear of ownable's reserved `0xFFFF0000..0xFFFF001F`.
6. **Check call results** — `CALL`/`DELEGATECALL`/`TRANSFER` return `0` on failure and do **not** auto-revert
   you; branch on the result or you'll silently continue past a failed payment.
7. **Value width** — `CALLVALUE` is 32-bit; deposits above `0xFFFFFFFF` units are refunded, not truncated.
   For larger holdings accumulate across deposits and use the 64-bit `TRANSFER`/`CALL` amount fields.
8. **Gas griefing** — a sub-call spends your shared budget; don't forward calls to untrusted code in a path
   that must complete.
9. **Determinism** — never assume wall-clock, randomness, or external state. The only "now" is `SYS_NUMBER`
   (block height); the only entropy is what's committed on-chain (and a committed hash is *not* post-hoc
   entropy — see the raffle's documented demo-grade caveat).

---

## 11. Determinism & rollback rules for authors

Your contract is re-executed verbatim on a reorg (the node rebuilds `vm_state` by replaying every `vm:` tx
from the fork height in `(block_height, rowid)` order). For that to reproduce the exact `state_root`:

* Use only the syscalls — never smuggle in host state. Every output must be a function of code + calldata +
  on-chain context + committed state.
* Don't rely on transient memory across calls — memory is fresh-zeroed each call; persistence is storage.
* `SETCODE`/`SELFDESTRUCT` are deterministic mutations of committed state and replay identically; you don't
  need to do anything special, just keep their *triggers* on-chain-deterministic (e.g. an owner check, a
  height gate), never a clock or RNG.

---

## 12. Building, testing, deploying

* **Author** with `contracts/asmtools.py` (`Asm`: `.label/.li/.beq/.../.sstore/.call/...`, plus `mulu`,
  `divu`, `mulu64`, `divu64`, `store_u64_be`). Two-pass `assemble()` resolves labels and relaxes
  out-of-range branches. `Asm` is *tooling* — nodes execute the assembled **bytecode**, never the Python.
* **Test off-chain (fast, no node)** — `tests/test_contracts_offchain.py` has a one-contract `Chain` harness
  (deposit-on-call, `TRANSFER`-out, revert keeps deposit). For multi-contract calls,
  `tests/test_vm_flex.py` drives the **real** `vm_engine.Host` + engine over an in-memory `MemState` (so the
  production call/journal/flush code is exercised without LMDB), and includes an LMDB `state_root`
  determinism-on-replay case.
* **Test on-chain (regnet)** — `tests/test_vm_demo_contracts.py`, `test_vm_value.py`, `test_vm_upgrade.py`
  deploy and call through real transactions mined past the hf2 fork (the `client` fixture; `vm=True`,
  `fork_signal=True`). Run: `python3 -m pytest tests/test_vm_flex.py tests/test_vm_upgrade.py -v`.
* **Deploy** — send `vm:deploy` with the bytecode hex; read the resulting address from `/api/vm/contracts`;
  call with `vm:call` `"<addr>:<calldata_hex>"`, attaching value by sending it to `VM_SINK`. A relay/UI
  pattern is in `web/` (e.g. `web/raffle/`, `web/predictionmarket/`).

---

## 13. Demo contract catalog (worked examples)

| contract | file | shows |
|---|---|---|
| Token (ERC-20-mini) | `contracts/token_contract.py` | selector dispatch, tagged-slot maps, owner-once mint |
| Escrow | `contracts/escrow.py` | role-based access control + custody release |
| Vesting | `contracts/vesting.py` | height-gated release via `SYS_NUMBER` |
| Raffle | `contracts/raffle.py` | `SYS_SHA256` commit-reveal + value custody |
| Prediction market | `contracts/prediction_market.py` | pro-rata payouts (`mulu64`/`divu64`) |
| Multisig vault | `contracts/multisig.py` | M-of-N approval, multi-word recipient storage |
| DEX / AMM / router | `contracts/dex.py`, `amm.py`, `router.py` | order book / constant-product / multi-pool routing |
| Poker | `contracts/poker*.py`, `tournament.py` | a full on-chain referee dApp |
| **Upgradeable proxy** | **`contracts/upgradeable.py`** | **`DELEGATECALL` proxy + live impl upgrade (#384)** |
| **Ownership module** | **`contracts/ownable.py`** | **optional/transferable/delegable/renounceable owner on full-address identity** |

---

## 14. Source & test map

| concern | source | tests |
|---|---|---|
| Interpreter + syscalls + host | `bismuth_riscv.py` | `tests/test_riscv.py`, `tests/test_vm_flex.py` |
| Chain wiring, custody, journaled `Host`, `_call` | `vm_engine.py` | `tests/test_vm_value.py`, `tests/test_vm_flex.py` |
| State store (code/storage/balances, `state_root`, deletion) | `vm_state.py` | `tests/test_vm_state.py`, `tests/test_vm_flex.py` |
| Assembler | `contracts/asmtools.py` | (exercised by every contract test) |
| Ownership module | `contracts/ownable.py` | `tests/test_ownable.py` |
| Fork gate / activation | `digest.py`, `fork.py` | `tests/test_vm_post_fork.py` |
| Consensus map / rationale | **doc/19-vm.md** | — |
