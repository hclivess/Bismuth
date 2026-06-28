"""
Upgradeable proxy (contracts/upgradeable.py) — live post-fork integration on regnet (vm=True,
fork_signal=True). Proves the new SYS_DELEGATECALL syscall and the proxy → swappable-implementation
upgrade pattern over REAL vm:deploy / vm:call transactions mined past the hf2 fork (doc/19 — issue #384):

  deploy impl_v1 + impl_v2 + proxy(owner, impl_v1)  ->  INIT  ->  BUMP×3 (via v1, +1 each)
  ->  UPGRADE proxy to impl_v2 (owner-only)  ->  BUMP (via v2, +10)  — same storage, new logic.

Exhaustive off-chain edge cases (value forwarding, reentrancy, depth cap, SETCODE, SELFDESTRUCT, revert
isolation, state-root determinism) are in tests/test_vm_flex.py; this file proves the chain plumbing.

Run with: python3 -m pytest tests/test_vm_upgrade.py -v
"""
import json
import urllib.request
from time import sleep

import upgradeable as up

API = "http://127.0.0.1:3031"


def _get(path):
    with urllib.request.urlopen(API + path, timeout=8) as r:
        return json.load(r)


def _wait_fork_active(client):
    info = _get("/api/vm/contracts")
    assert info["enabled"], "vm not enabled on this node"
    for _ in range(25):
        info = _get("/api/vm/contracts")
        if info.get("fork_height") is not None and client.block_height() > info["fork_height"]:
            return info
        client.mine(3)
    raise AssertionError("hf2 fork not active")


def _deploy(client, code_hex):
    before = set(_get("/api/vm/contracts")["contracts"])
    client.send(client.address, 1, "vm:deploy", code_hex)
    client.mine(2)
    sleep(0.3)
    new = set(_get("/api/vm/contracts")["contracts"]) - before
    assert new, "deploy mined but the VM did not register the contract"
    return new.pop()


def _call(client, addr, calldata_hex, amount=0):
    import vm_engine
    recipient = vm_engine.VM_SINK if amount else client.address
    client.send(recipient, amount, "vm:call", "%s:%s" % (addr, calldata_hex))
    client.mine(1)
    sleep(0.2)


def _slots(addr):
    d = _get("/api/vm/contract/" + addr)
    return {int(s["key"]): int(s["value"]) for s in d["storage"]}


def _sel(n):
    return n.to_bytes(4, "big").hex()


def test_proxy_delegatecall_upgrade(client):
    _wait_fork_active(client)

    v1 = _deploy(client, up.impl_v1().hex())
    v2 = _deploy(client, up.impl_v2().hex())
    proxy = _deploy(client, up.build(client.address, v1).hex())   # owner = the deploying wallet (full address)

    # INIT (owner-gated): the proxy records impl = v1
    _call(client, proxy, _sel(up.ADMIN_INIT))
    assert _slots(proxy).get(up.S_INIT) == 1, "proxy should be initialised"

    # BUMP three times THROUGH the proxy -> delegatecalls v1 (+1 each); state lands in the PROXY
    for expect in (1, 2, 3):
        _call(client, proxy, _sel(up.FN_BUMP))
        assert _slots(proxy).get(up.APP_BASE) == expect, "v1 bump should write the proxy's storage"
    # the implementation's OWN storage stays empty (the code is borrowed, the storage is the proxy's)
    assert _slots(v1).get(up.APP_BASE) in (None, 0), "impl_v1 must not hold the counter"

    # UPGRADE to v2 (owner-gated)
    _call(client, proxy, _sel(up.ADMIN_UPGRADE) + v2)

    # BUMP again -> now delegatecalls v2 (+10); the SAME counter continues 3 -> 13 (storage survived)
    _call(client, proxy, _sel(up.FN_BUMP))
    assert _slots(proxy).get(up.APP_BASE) == 13, "after upgrade the counter steps by 10 on the same storage"
    assert _slots(v2).get(up.APP_BASE) in (None, 0), "impl_v2 must not hold the counter either"
