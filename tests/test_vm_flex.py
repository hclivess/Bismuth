"""
Contract-flexibility VM ops (doc/19 — issue #384) — fast off-chain tests (no node).

Exercises the four new syscalls + the journaled multi-contract host END-TO-END through the REAL
production classes (``bismuth_riscv.execute`` + ``vm_engine.Host`` + ``vm_engine._call``), over an
in-memory stand-in for vm_state's surface:

  * SYS_CALL          — contract-to-contract call (callee storage/custody, value forwarding, return data,
                        caller identity, revert isolation, reentrancy, depth cap, shared gas)
  * SYS_DELEGATECALL  — borrowed code in the caller's storage/identity; the upgradeable-proxy primitive
  * SYS_SETCODE       — in-place code replacement (self-upgrade), owner-gated
  * SYS_SELFDESTRUCT  — pay out custody + delete code/storage, revert-isolated
  * SYS_ADDRESS       — a contract reading its own address

The regnet test (tests/test_vm_upgrade.py) proves the same ops over real on-chain transactions; the LMDB
state-root/determinism case at the bottom proves replay reproduces the exact contract state.

Run with: python3 -m pytest tests/test_vm_flex.py -v
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "contracts"))

import bismuth_riscv as rv
import vm_engine
from asmtools import (Asm, A0, A1, A2, A3, A4, A5, S0, S1, S2, T0, T1, T2, T3, T4, T5, T6, ZERO,
                      SYS_ADDRESS, SYS_SELFDESTRUCT, SYS_CALLER_FULL)

GAS = vm_engine.GAS_LIMIT


# ---------------------------------------------------------------- helpers
def H(n):
    """A 56-hex (28-byte) contract address from a small int."""
    return "%056x" % n


def be(n, w):
    return int(n).to_bytes(w, "big")


def _ld_be32(a, rd, base, off, scr=T6):
    """rd = big-endian u32 at [base+off]; clobbers scr."""
    a.lbu(rd, base, off)
    a.slli(rd, rd, 8); a.lbu(scr, base, off + 1); a.or_(rd, rd, scr)
    a.slli(rd, rd, 8); a.lbu(scr, base, off + 2); a.or_(rd, rd, scr)
    a.slli(rd, rd, 8); a.lbu(scr, base, off + 3); a.or_(rd, rd, scr)


def _st_be32(a, val, base, off, scr=T6):
    """Store the u32 in `val` as 4 big-endian bytes at [base+off]; clobbers scr."""
    for i in range(4):
        a.srli(scr, val, (3 - i) * 8)
        a.sb(scr, base, off + i)


class MemState:
    """In-memory stand-in for vm_state.VMState's surface, so the REAL vm_engine.Host runs without LMDB.
    code: addr->bytes; storage: addr->{key:val} (val 0 == unset); balances: addr->int."""

    def __init__(self):
        self.code = {}
        self.storage = {}
        self.balances = {}

    # reads
    def get_code(self, addr):
        return self.code.get(addr)

    def load_storage(self, addr):
        return dict(self.storage.get(addr, {}))

    def get_balance(self, addr):
        return int(self.balances.get(addr, 0))

    # writes (flush targets)
    def deploy(self, addr, code):
        self.code[addr] = bytes(code)

    def commit_storage(self, addr, storage):
        s = self.storage.setdefault(addr, {})
        for k, v in storage.items():
            if v:
                s[k] = v
            else:
                s.pop(k, None)

    def set_balance(self, addr, value):
        value = int(value)
        if value:
            self.balances[addr] = value
        else:
            self.balances.pop(addr, None)

    def delete_code(self, addr):
        self.code.pop(addr, None)

    def clear_storage(self, addr):
        self.storage.pop(addr, None)


def vmexec(state, addr, calldata, caller_hex=H(0xCA11E5), value=0, height=0):
    """Mirror vm_engine._call's SUCCESS path over the real Host, returning (success, payouts, result) so a
    test can assert both control flow and final state. `value` is pre-credited like a sink deposit."""
    host = vm_engine.Host(state)
    code = host.get_code(addr)
    assert code is not None, "no contract at %s" % addr
    if value:
        host.add_balance(addr, value)
    # pass the FULL caller int (the chain does too via _caller_int); SYS_CALLER truncates to 32 bits on read,
    # SYS_CALLER_FULL exposes all 224 bits.
    r = rv.execute(code, calldata=calldata, caller=int(caller_hex, 16), callvalue=value,
                   gas_limit=GAS, block_height=height, host=host, address=addr, depth=0)
    if not r.success:
        return False, [], r
    host.flush()
    payouts = [("%056x" % (to & rv.ADDR_MASK), amt) for to, amt in host.payouts]
    return True, payouts, r


# ---------------------------------------------------------------- contracts (hand-authored RV32I)
def c_kv():
    """KV cell + bookkeeping. calldata = key(4 BE) | val(4 BE). storage[key]=val; records CALLVALUE at
    0xF000 and CALLER at 0xF001; RETURNs val. Used as a CALL callee AND a DELEGATECALL implementation."""
    a = Asm()
    a.mv(S0, A0)
    _ld_be32(a, T0, S0, 0)                                  # key
    _ld_be32(a, T1, S0, 4)                                  # val
    a.sstore(T0, T1)
    a.callvalue(T2); a.li(T3, 0xF000); a.sstore(T3, T2)     # record value seen
    a.caller(T4);    a.li(T5, 0xF001); a.sstore(T5, T4)     # record caller seen
    a.mv(A0, T1); a.ret()
    return a.assemble()


def c_const(word):
    """Ignores calldata, RETURNs a baked-in constant. Used as the SETCODE 'v2' upgrade target."""
    a = Asm()
    a.li(A0, word); a.ret()
    return a.assemble()


def c_caller():
    """Forwarder. calldata = target(28) | value(8) | key(4) | val(4). SYS_CALLs target forwarding `value`
    and inner calldata = key|val; RETURNs the callee's returned word (0 if the call failed)."""
    a = Asm()
    a.mv(S0, A0)
    a.mv(T1, S0)                                            # callee ptr (target, 28 bytes)
    a.addi(T2, S0, 28)                                     # value ptr (8 bytes)
    a.addi(T3, S0, 36)                                     # inner calldata ptr (key|val, 8 bytes)
    a.li(T4, 8)                                            # inner calldata len
    a.li(T5, 5000)                                         # return buffer
    a.li(S1, 4)
    a.call(T1, T2, T3, T4, T5, S1)                          # a0 = success
    a.beq(A0, ZERO, "fail")
    a.li(T0, 5000); _ld_be32(a, A0, T0, 0)                 # return what the callee returned
    a.ret()
    a.label("fail"); a.li(A0, 0); a.ret()
    return a.assemble()


def c_delegator():
    """Bare proxy. calldata = impl(28) | key(4) | val(4). DELEGATECALLs impl with inner calldata = key|val;
    RETURNs the delegatecall success flag. The impl's writes land in THIS proxy's storage."""
    a = Asm()
    a.mv(S0, A0)
    a.mv(T1, S0)                                            # impl ptr
    a.addi(T2, S0, 28)                                     # inner calldata ptr (key|val)
    a.li(T3, 8)
    a.li(T4, 6000); a.li(T5, 4)
    a.delegatecall(T1, T2, T3, T4, T5)                      # a0 = success
    a.ret()
    return a.assemble()


def c_recur():
    """Self-recursive via SYS_CALL to its OWN address (SYS_ADDRESS). Each frame increments the SHARED
    counter at slot 1 (proving live cross-frame storage = reentrancy). calldata = remaining(4 BE).
    RETURNs the final counter."""
    a = Asm()
    a.mv(S0, A0)
    a.li(T0, 1); a.sload_to(T1, T0); a.addi(T1, T1, 1); a.sstore(T0, T1)   # slot1 += 1
    _ld_be32(a, T2, S0, 0)                                 # remaining
    a.beq(T2, ZERO, "ret")
    a.li(A0, 2000); a.syscall(SYS_ADDRESS)                 # write self addr (28 bytes) at mem[2000]
    a.addi(T3, T2, -1)
    a.li(T4, 2036); _st_be32(a, T3, T4, 0)                 # inner calldata = remaining-1 at mem[2036]
    a.li(T0, 2000)                                         # callee ptr (self)
    a.li(T1, 2028)                                         # value ptr (mem is zero -> value 0)
    a.li(T2, 2036)                                         # calldata ptr
    a.li(T3, 4)                                            # calldata len
    a.li(T4, 2040)                                         # ret ptr
    a.li(T5, 4)                                            # ret len
    a.call(T0, T1, T2, T3, T4, T5)
    a.label("ret")
    a.li(T0, 1); a.sload_to(A0, T0); a.ret()              # return the (shared) counter
    return a.assemble()


def c_setcode(owner_id):
    """Owner-gated in-place self-upgrade. caller must == owner_id; the WHOLE calldata becomes the new code.
    RETURNs 1 (the new code is effective on the NEXT call)."""
    a = Asm()
    a.mv(S0, A0); a.mv(S1, A1)                              # save calldata ptr/len before clobbering A0
    a.caller(T0); a.li(T1, owner_id); a.bne(T0, T1, "revert")
    a.setcode(S0, S1)
    a.li(A0, 1); a.ret()
    a.label("revert"); a.raw(0)
    return a.assemble()


def c_selfdestruct():
    """calldata = beneficiary(28). Pays remaining custody to the beneficiary and deletes itself."""
    a = Asm()
    a.mv(S0, A0)
    a.selfdestruct(S0)
    return a.assemble()


def c_call_fwd_then_revert():
    """Parent that CALLs `target` forwarding inner calldata, then REVERTS. calldata = target(28)|inner(28).
    Proves an inner call's effects (even a SELFDESTRUCT) roll back when the OUTER frame reverts."""
    a = Asm()
    a.mv(S0, A0)
    a.mv(T1, S0)                                            # callee = target
    a.li(T2, 9000)                                         # value ptr (zeros)
    a.addi(T3, S0, 28)                                     # inner calldata ptr
    a.li(T4, 28)                                           # inner calldata len
    a.li(T5, 9100); a.li(S2, 4)
    a.call(T1, T2, T3, T4, T5, S2)
    a.raw(0)                                               # REVERT after the inner call returns
    return a.assemble()


def c_call_then_check():
    """CALLs `target` with inner calldata key|val; returns the call's success flag. calldata =
    target(28)|key(4)|val(4), no value. Used to prove a reverting callee doesn't taint the caller."""
    a = Asm()
    a.mv(S0, A0)
    a.mv(T1, S0)
    a.li(T2, 9000)                                         # value ptr (zeros)
    a.addi(T3, S0, 28)
    a.li(T4, 8)
    a.li(T5, 9100); a.li(S1, 4)
    a.call(T1, T2, T3, T4, T5, S1)
    a.ret()                                                # a0 = success flag
    return a.assemble()


# ================================ SYS_ADDRESS ================================
def test_address_returns_own_address():
    st = MemState()
    addr = H(0x1234)
    a = Asm(); a.li(A0, 1000); a.syscall(SYS_ADDRESS); a.ret()    # write addr at mem[1000], return its fold
    st.deploy(addr, a.assemble())
    ok, _, r = vmexec(st, addr, b"")
    assert ok
    assert int.from_bytes(r.output, "big") == int(addr, 16) & rv.WMASK
    # the full 28-byte address was written to memory (verified via a contract that returns a high word)
    b = Asm(); b.li(A0, 1000); b.syscall(SYS_ADDRESS); b.li(T0, 1000)
    _ld_be32(b, A0, T0, 0); b.ret()                              # return the TOP 4 bytes of the address
    st.deploy(addr, b.assemble())
    _, _, r2 = vmexec(st, addr, b"")
    assert int.from_bytes(r2.output, "big") == (int(addr, 16) >> 192) & rv.WMASK


def test_caller_full_exposes_all_224_bits():
    st = MemState()
    addr = H(0x1235)
    # write full caller to mem[1000], return the TOP word (which SYS_CALLER's 32-bit fold cannot see)
    a = Asm(); a.li(A0, 1000); a.syscall(SYS_CALLER_FULL); a.li(T0, 1000)
    a.lbu(A0, T0, 0); a.slli(A0, A0, 8); a.lbu(T1, T0, 1); a.or_(A0, A0, T1)
    a.slli(A0, A0, 8); a.lbu(T1, T0, 2); a.or_(A0, A0, T1)
    a.slli(A0, A0, 8); a.lbu(T1, T0, 3); a.or_(A0, A0, T1)
    a.ret()
    st.deploy(addr, a.assemble())
    caller = H(0xDEADBEEF00000000000000000000000000000000000000000000AA)
    _, _, r = vmexec(st, addr, b"", caller_hex=caller)
    assert int.from_bytes(r.output, "big") == (int(caller, 16) >> 192) & rv.WMASK   # high word, not the fold


# ================================ SYS_CALL ================================
def test_call_runs_callee_in_its_own_storage_with_value_and_return():
    st = MemState()
    callee, caller = H(0xC0DE), H(0xCA11)
    st.deploy(callee, c_kv())
    st.deploy(caller, c_caller())
    # caller is funded with 70 (deposit), forwards 50 to callee, inner key=9 val=123
    cd = bytes.fromhex(callee) + be(50, 8) + be(9, 4) + be(123, 4)
    ok, payouts, r = vmexec(st, caller, cd, caller_hex=H(0xAB), value=70)
    assert ok
    assert int.from_bytes(r.output, "big") == 123          # callee's return propagated back
    assert payouts == []                                   # value moved internally, no external payout
    # callee storage: key 9 -> 123, and it SAW value 50 and caller = the forwarder contract
    assert st.storage[callee][9] == 123
    assert st.storage[callee][0xF000] == 50
    assert st.storage[callee][0xF001] == int(caller, 16) & rv.WMASK
    # custody conserved: caller 70-50=20, callee 50 (sink total unchanged)
    assert st.get_balance(caller) == 20
    assert st.get_balance(callee) == 50
    assert caller not in st.storage or 9 not in st.storage.get(caller, {})   # caller storage untouched


def test_call_to_missing_contract_fails_cleanly():
    st = MemState()
    caller = H(0xCA11)
    st.deploy(caller, c_caller())
    cd = bytes.fromhex(H(0xDEAD)) + be(0, 8) + be(1, 4) + be(2, 4)   # no contract at 0xDEAD
    ok, _, r = vmexec(st, caller, cd)
    assert ok and int.from_bytes(r.output, "big") == 0     # forwarder returns 0 on failed call


def test_call_reverting_callee_does_not_taint_caller():
    st = MemState()
    target, parent = H(0xBAD), H(0xCA11)
    st.deploy(target, c_const(0))           # placeholder; overwrite with a reverting contract
    rev = Asm(); rev.raw(0)
    st.deploy(target, rev.assemble())       # always reverts (illegal instruction)
    st.deploy(parent, c_call_then_check())
    cd = bytes.fromhex(target) + be(7, 4) + be(8, 4)
    ok, _, r = vmexec(st, parent, cd)
    assert ok and int.from_bytes(r.output, "big") == 0     # parent survived; call reported failure
    assert target not in st.storage                        # reverting callee wrote nothing


def test_call_reentrancy_shares_live_storage():
    st = MemState()
    addr = H(0x5151)
    st.deploy(addr, c_recur())
    ok, _, r = vmexec(st, addr, be(3, 4))                  # 4 frames (depth 0..3)
    assert ok
    assert int.from_bytes(r.output, "big") == 4            # shared counter == frames; proves reentrancy
    assert st.storage[addr][1] == 4


def test_call_depth_is_capped():
    st = MemState()
    addr = H(0x5252)
    st.deploy(addr, c_recur())
    ok, _, r = vmexec(st, addr, be(100, 4))                # asks for 101 frames; capped at MAX_CALL_DEPTH+1
    assert ok
    assert int.from_bytes(r.output, "big") == rv.MAX_CALL_DEPTH + 1


def test_call_shares_the_gas_budget():
    st = MemState()
    callee, caller = H(0xC0DE), H(0xCA11)
    st.deploy(callee, c_kv())
    st.deploy(caller, c_caller())
    # run the callee standalone vs through a CALL; the CALL's gas_used must exceed the callee's alone
    solo = rv.execute(c_kv(), calldata=be(9, 4) + be(1, 4), gas_limit=GAS)
    cd = bytes.fromhex(callee) + be(0, 8) + be(9, 4) + be(1, 4)
    _, _, r = vmexec(st, caller, cd)
    assert r.gas_used > solo.gas_used                      # caller paid for the callee's gas too


# ================================ SYS_DELEGATECALL (proxy/upgrade) ================================
def test_delegatecall_writes_land_in_the_proxy_storage():
    st = MemState()
    impl, proxy = H(0x119), H(0x9305)
    st.deploy(impl, c_kv())
    st.deploy(proxy, c_delegator())
    cd = bytes.fromhex(impl) + be(5, 4) + be(42, 4)
    ok, _, r = vmexec(st, proxy, cd, caller_hex=H(0xBEEF), value=0)
    assert ok and int.from_bytes(r.output, "big") == 1
    # the impl's SSTORE landed in the PROXY, not the impl
    assert st.storage[proxy][5] == 42
    assert impl not in st.storage                          # the borrowed code's own storage is untouched
    # CALLER is preserved through delegation (the proxy's caller, not the proxy)
    assert st.storage[proxy][0xF001] == int(H(0xBEEF), 16) & rv.WMASK


def test_delegatecall_proxy_upgrade_swaps_logic_keeps_storage():
    """The headline: a proxy DELEGATECALLs into impl_v1, then is repointed at impl_v2 — storage persists,
    behaviour changes. Here we model 'repoint' by passing the new impl in calldata (a real proxy stores
    impl_addr; see contracts/upgradeable.py + tests/test_vm_upgrade.py for the storage-backed version)."""
    st = MemState()
    v1, v2, proxy = H(0x1), H(0x2), H(0x9306)
    st.deploy(v1, c_kv())                                  # v1: stores key->val
    # v2: a different KV that DOUBLES the value before storing
    a = Asm(); a.mv(S0, A0)
    _ld_be32(a, T0, S0, 0); _ld_be32(a, T1, S0, 4); a.add(T1, T1, T1)   # val *= 2
    a.sstore(T0, T1); a.mv(A0, T1); a.ret()
    st.deploy(v2, a.assemble())
    st.deploy(proxy, c_delegator())

    vmexec(st, proxy, bytes.fromhex(v1) + be(7, 4) + be(10, 4))         # via v1: slot7 = 10
    assert st.storage[proxy][7] == 10
    vmexec(st, proxy, bytes.fromhex(v2) + be(7, 4) + be(10, 4))         # via v2: slot7 = 20 (doubled)
    assert st.storage[proxy][7] == 20                                   # same storage, new logic


# ================================ SYS_SETCODE (in-place upgrade) ================================
def test_setcode_replaces_own_code_next_call_and_is_owner_gated():
    st = MemState()
    owner = 0xA11CE
    addr = H(0x57C0DE)
    st.deploy(addr, c_setcode(owner))
    v2 = c_const(999)

    # non-owner cannot upgrade
    ok, _, _ = vmexec(st, addr, v2, caller_hex=H(0xBAD))
    assert not ok
    assert st.get_code(addr) == c_setcode(owner)                       # unchanged

    # owner upgrades; the SAME call still runs the old code (returns 1)
    ok, _, r = vmexec(st, addr, v2, caller_hex=H(owner))
    assert ok and int.from_bytes(r.output, "big") == 1
    assert st.get_code(addr) == v2                                     # code replaced in state
    # the NEXT call runs the new code
    ok, _, r = vmexec(st, addr, b"")
    assert ok and int.from_bytes(r.output, "big") == 999


# ================================ SYS_SELFDESTRUCT ================================
def test_selfdestruct_pays_out_and_deletes():
    st = MemState()
    addr = H(0xDEAD)
    st.deploy(addr, c_selfdestruct())
    st.storage[addr] = {1: 5, 2: 9}                                   # some existing storage
    benef = 0xBEEF
    ok, payouts, _ = vmexec(st, addr, be(benef, 28), value=400)        # funded with 400
    assert ok
    assert payouts == [(H(benef), 400)]                               # remaining custody paid out
    assert st.get_code(addr) is None                                  # code deleted
    assert addr not in st.storage                                     # storage cleared
    assert st.get_balance(addr) == 0                                  # balance drained


def test_selfdestruct_rolls_back_when_outer_frame_reverts():
    st = MemState()
    target, parent = H(0xD1E), H(0xCA11)
    st.deploy(target, c_selfdestruct())
    st.balances[target] = 100
    st.deploy(parent, c_call_fwd_then_revert())
    cd = bytes.fromhex(target) + be(0xBEEF, 28)                        # parent CALLs target then reverts
    ok, payouts, _ = vmexec(st, parent, cd)
    assert not ok                                                     # the outer revert kills everything
    assert st.get_code(target) == c_selfdestruct()                    # target NOT destroyed
    assert st.get_balance(target) == 100                              # nor drained
    assert payouts == []


# ================================ vm_engine._call value-custody parity ================================
def test_call_refunds_value_to_missing_contract():
    st = MemState()
    sender = H(0x5E4DE5)
    payouts = vm_engine._call(st, H(0x404) + ":", "sig", sender, vm_engine.VM_SINK, 250, 1)
    assert payouts == [(sender, 250)]                                 # refunded, supply exact


def test_call_revert_refunds_deposit():
    st = MemState()
    rev = Asm(); rev.raw(0)
    addr = H(0x404E)
    st.deploy(addr, rev.assemble())
    sender = H(0x5E4DE5)
    payouts = vm_engine._call(st, addr + ":", "sig", sender, vm_engine.VM_SINK, 250, 1)
    assert payouts == [(sender, 250)]
    assert st.get_balance(addr) == 0                                  # nothing persisted


def test_call_success_keeps_custody_no_payout():
    st = MemState()
    addr = H(0x404F)
    st.deploy(addr, c_kv())
    sender = H(0x5E4DE5)
    payouts = vm_engine._call(st, addr + ":" + (be(9, 4) + be(1, 4)).hex(), "sig", sender,
                              vm_engine.VM_SINK, 250, 1)
    assert payouts == []                                             # no TRANSFER -> custody retained
    assert st.get_balance(addr) == 250
    assert st.storage[addr][9] == 1


# ================================ determinism + state_root (LMDB) ================================
def _vmstate(tmp_path, name):
    vm_state = pytest.importorskip("vm_state")
    return vm_state.VMState(str(tmp_path / name))


def _apply(state, ops):
    """Run a list of (addr, calldata, value) vm:calls through the real _call against a real VMState."""
    for addr, cd, value in ops:
        of = addr + ":" + cd.hex()
        recip = vm_engine.VM_SINK if value else "x"
        vm_engine._call(state, of, "sig", "f" * 56, recip, value, 1)


def test_state_root_deterministic_across_replay(tmp_path):
    pytest.importorskip("lmdb")
    kv, sd = H(0xC0DE), H(0xD1E)
    code_kv, code_sd = c_kv(), c_selfdestruct()

    def build(state):
        state.deploy(kv, code_kv)
        state.deploy(sd, code_sd)
        state.set_balance(sd, 0)
        _apply(state, [
            (kv, be(1, 4) + be(11, 4), 100),         # kv: slot1=11, +100 custody
            (kv, be(2, 4) + be(22, 4), 0),           # kv: slot2=22
            (sd, be(0xBEEF, 28), 50),                # sd: funded 50 then self-destructs to 0xBEEF
        ])

    a = _vmstate(tmp_path, "a"); build(a); root_a = a.state_root(); a.close()
    b = _vmstate(tmp_path, "b"); build(b); root_b = b.state_root(); b.close()
    assert root_a == root_b                                          # identical replay -> identical root

    # and the self-destruct really removed the contract from the committed state
    c = _vmstate(tmp_path, "a")
    assert c.get_code(kv) is not None
    assert c.get_code(sd) is None
    assert c.get_balance(sd) == 0
    assert c.storage_items(sd) == []
    assert c.get_balance(kv) == 100
    c.close()


# ================================ upgradeable proxy (real, storage-backed) ================================
def test_upgradeable_proxy_full_lifecycle():
    import upgradeable as up
    st = MemState()
    owner = H(0xC0FFEE)                                         # full 224-bit owner address
    v1, v2, proxy = H(0x1111), H(0x2222), H(0x9999)
    st.deploy(v1, up.impl_v1())
    st.deploy(v2, up.impl_v2())
    st.deploy(proxy, up.build(owner, v1))

    # INIT (owner-only)
    ok, _, _ = vmexec(st, proxy, be(up.ADMIN_INIT, 4), caller_hex=owner)
    assert ok and st.storage[proxy][up.S_INIT] == 1
    # impl slots hold v1 (high-zero words aren't stored — 0 == unset — but reassemble to v1)
    impl_words = [st.storage[proxy].get(up.S_IMPL0 + w, 0) for w in range(7)]
    assert int.from_bytes(b"".join(be(w, 4) for w in impl_words), "big") == int(v1, 16)

    # BUMP three times via v1 (step +1) -> counter 1,2,3, stored in the PROXY at APP_BASE
    for expect in (1, 2, 3):
        ok, _, r = vmexec(st, proxy, be(up.FN_BUMP, 4))
        assert ok and int.from_bytes(r.output, "big") == expect
    assert st.storage[proxy][up.APP_BASE] == 3
    assert v1 not in st.storage                                 # impl's OWN storage stays empty (delegated)

    # UPGRADE to v2 (owner-only); non-owner is rejected first
    ok, _, _ = vmexec(st, proxy, be(up.ADMIN_UPGRADE, 4) + bytes.fromhex(v2), caller_hex=H(0xBAD))
    assert not ok
    ok, _, _ = vmexec(st, proxy, be(up.ADMIN_UPGRADE, 4) + bytes.fromhex(v2), caller_hex=owner)
    assert ok

    # BUMP again -> now v2 (step +10); SAME counter continues from 3 -> 13 (storage survived the upgrade)
    ok, _, r = vmexec(st, proxy, be(up.FN_BUMP, 4))
    assert ok and int.from_bytes(r.output, "big") == 13
    assert st.storage[proxy][up.APP_BASE] == 13
    assert v2 not in st.storage


def test_setcode_changes_state_root(tmp_path):
    pytest.importorskip("lmdb")
    st = _vmstate(tmp_path, "s")
    owner = 0xA11CE
    addr = H(0x57C0DE)
    st.deploy(addr, c_setcode(owner))
    root0 = st.state_root()
    _apply(st, [(addr, c_const(999), 0)])            # owner is "f"*56 != owner_id -> revert, no change
    assert st.state_root() == root0
    # now actually upgrade (caller == owner) via a direct _call with the owner as sender
    vm_engine._call(st, addr + ":" + c_const(999).hex(), "sig", "%056x" % owner, "x", 0, 1)
    assert st.get_code(addr) == c_const(999)
    assert st.state_root() != root0
    st.close()
