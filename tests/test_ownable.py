"""
Ownership module (contracts/ownable.py) — fast off-chain tests (no node).

Drives the standalone `owned_counter` demo through the REAL vm_engine.Host + engine over an in-memory
state, exercising the full ownership model on FULL 224-bit identity (SYS_CALLER_FULL):
  optional/owner-gated · two-step transfer · accept guards · renounce->immutable · revocable delegate ·
  and the headline security property: a 32-bit-fold collision is NOT enough to pass an owner check.

Run with: python3 -m pytest tests/test_ownable.py -v
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "contracts"))

import bismuth_riscv as rv
import vm_engine
import ownable as own

GAS = vm_engine.GAS_LIMIT


def H(n):
    return "%056x" % (n & ((1 << 224) - 1))


def be(n, w):
    return int(n).to_bytes(w, "big")


class MemState:
    def __init__(self):
        self.code, self.storage, self.balances = {}, {}, {}

    def get_code(self, a): return self.code.get(a)
    def load_storage(self, a): return dict(self.storage.get(a, {}))
    def get_balance(self, a): return int(self.balances.get(a, 0))
    def deploy(self, a, c): self.code[a] = bytes(c)

    def commit_storage(self, a, s):
        d = self.storage.setdefault(a, {})
        for k, v in s.items():
            d[k] = v if v else d.pop(k, 0) and 0
            if not v:
                d.pop(k, None)

    def set_balance(self, a, v):
        if int(v):
            self.balances[a] = int(v)
        else:
            self.balances.pop(a, None)

    def delete_code(self, a): self.code.pop(a, None)
    def clear_storage(self, a): self.storage.pop(a, None)


def call(state, addr, calldata, caller):
    """Run one vm:call through the real Host+engine; return (success, output_int)."""
    host = vm_engine.Host(state)
    r = rv.execute(host.get_code(addr), calldata=calldata, caller=int(caller, 16),
                   gas_limit=GAS, host=host, address=addr, depth=0)
    if not r.success:
        return False, None
    host.flush()
    return True, int.from_bytes(r.output, "big")


def sel(s):
    return be(s, 4)


def bump(state, addr, caller):
    return call(state, addr, sel(own.FN_BUMP), caller)


# distinct full addresses
OWNER = H(0xA11CE_0000 << 80 | 0x1234)
ALICE = H(0xB0B_0000 << 80 | 0x5678)
DELEG = H(0xDE1E_0000 << 80 | 0x9ABC)


def _deploy():
    st = MemState()
    addr = H(0xC0FFEE)
    st.deploy(addr, own.owned_counter(OWNER))
    return st, addr


def test_owner_can_act_nonowner_cannot():
    st, addr = _deploy()
    ok, out = bump(st, addr, OWNER)
    assert ok and out == 1
    ok, _ = bump(st, addr, ALICE)
    assert not ok                                          # not owner, not delegate -> rejected
    # GET is public
    ok, out = call(st, addr, sel(own.FN_GET), ALICE)
    assert ok and out == 1


def test_two_step_transfer():
    st, addr = _deploy()
    # propose ALICE (owner only)
    ok, _ = call(st, addr, sel(own.OWN_TRANSFER) + bytes.fromhex(ALICE), ALICE)
    assert not ok                                          # a non-owner can't propose
    ok, _ = call(st, addr, sel(own.OWN_TRANSFER) + bytes.fromhex(ALICE), OWNER)
    assert ok
    # not transferred yet: OWNER still in control, ALICE is not
    assert bump(st, addr, OWNER)[0]
    assert not bump(st, addr, ALICE)[0]
    # wrong account can't accept
    assert not call(st, addr, sel(own.OWN_ACCEPT), DELEG)[0]
    # ALICE accepts -> now ALICE is owner, OWNER is not
    assert call(st, addr, sel(own.OWN_ACCEPT), ALICE)[0]
    assert bump(st, addr, ALICE)[0]
    assert not bump(st, addr, OWNER)[0]


def test_accept_without_pending_reverts():
    st, addr = _deploy()
    assert not call(st, addr, sel(own.OWN_ACCEPT), ALICE)[0]


def test_transfer_to_zero_reverts():
    st, addr = _deploy()
    assert not call(st, addr, sel(own.OWN_TRANSFER) + b"\x00" * 28, OWNER)[0]


def test_renounce_makes_immutable():
    st, addr = _deploy()
    assert call(st, addr, sel(own.OWN_RENOUNCE), OWNER)[0]
    # nobody — not even the old owner — can act or administer anymore
    assert not bump(st, addr, OWNER)[0]
    assert not call(st, addr, sel(own.OWN_TRANSFER) + bytes.fromhex(ALICE), OWNER)[0]
    assert not call(st, addr, sel(own.OWN_SET_DELEGATE) + bytes.fromhex(ALICE), OWNER)[0]
    # GET still works (it's public) — the contract is frozen, not broken
    assert call(st, addr, sel(own.FN_GET), ALICE)[0]


def test_delegate_can_operate_but_not_administer():
    st, addr = _deploy()
    # only owner sets a delegate
    assert not call(st, addr, sel(own.OWN_SET_DELEGATE) + bytes.fromhex(DELEG), ALICE)[0]
    assert call(st, addr, sel(own.OWN_SET_DELEGATE) + bytes.fromhex(DELEG), OWNER)[0]
    # delegate can BUMP (operational) ...
    assert bump(st, addr, DELEG)[0]
    # ... but cannot transfer / renounce / re-delegate (owner-only)
    assert not call(st, addr, sel(own.OWN_TRANSFER) + bytes.fromhex(DELEG), DELEG)[0]
    assert not call(st, addr, sel(own.OWN_RENOUNCE), DELEG)[0]
    assert not call(st, addr, sel(own.OWN_SET_DELEGATE) + bytes.fromhex(ALICE), DELEG)[0]
    # owner revokes the delegate -> delegate can no longer operate
    assert call(st, addr, sel(own.OWN_SET_DELEGATE) + b"\x00" * 28, OWNER)[0]
    assert not bump(st, addr, DELEG)[0]


def _has_field(st, addr, s_base):
    return any((s_base + i) in st.storage.get(addr, {}) for i in range(7))


def test_no_stale_data_after_accept_renounce_or_clear():
    st, addr = _deploy()
    # transfer + accept -> S_PENDING data is dropped (not just the flag)
    assert call(st, addr, sel(own.OWN_TRANSFER) + bytes.fromhex(ALICE), OWNER)[0]
    assert _has_field(st, addr, own.S_PENDING)                  # staged
    assert call(st, addr, sel(own.OWN_ACCEPT), ALICE)[0]
    assert st.storage[addr].get(own.S_PEND_SET) in (None, 0)
    assert not _has_field(st, addr, own.S_PENDING)              # cleared, no stale address left
    # set then clear a delegate -> S_DELEGATE data dropped
    assert call(st, addr, sel(own.OWN_SET_DELEGATE) + bytes.fromhex(DELEG), ALICE)[0]
    assert _has_field(st, addr, own.S_DELEGATE)
    assert call(st, addr, sel(own.OWN_SET_DELEGATE) + b"\x00" * 28, ALICE)[0]
    assert not _has_field(st, addr, own.S_DELEGATE)
    # renounce -> pending + delegate fields fully cleared
    assert call(st, addr, sel(own.OWN_SET_DELEGATE) + bytes.fromhex(OWNER), ALICE)[0]
    assert call(st, addr, sel(own.OWN_RENOUNCE), ALICE)[0]
    assert not _has_field(st, addr, own.S_PENDING)
    assert not _has_field(st, addr, own.S_DELEGATE)
    assert st.storage[addr].get(own.S_RENOUNCED) == 1


def test_full_address_collision_is_rejected():
    """The whole point of SYS_CALLER_FULL: an attacker who matches only the 32-bit fold is NOT the owner."""
    # same low 32 bits as OWNER, different high bits
    attacker = H((0xDEADBEEF << 192) | (int(OWNER, 16) & 0xFFFFFFFF))
    assert (int(attacker, 16) & 0xFFFFFFFF) == (int(OWNER, 16) & 0xFFFFFFFF)   # folds collide
    assert attacker != OWNER
    st, addr = _deploy()
    assert not bump(st, addr, attacker)[0]                 # rejected despite the fold collision
    assert bump(st, addr, OWNER)[0]                        # real owner still works
