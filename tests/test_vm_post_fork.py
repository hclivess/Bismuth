"""
Decentralized-apps VM — live post-fork integration on regnet (vm=True, fork_signal=True).

Proves the end-to-end path: a vm:deploy transaction registers a contract, vm:call transactions execute it
against the persisted contract state, and it is all GATED behind the hf2 fork activation (the digest only
runs vm: txs once node.fork_height is set). Uses a counter contract: each call does storage[0] += 1.

Run with: python3 -m pytest tests/test_vm_post_fork.py -v
"""
import json
import urllib.error
import urllib.request
from time import sleep

import bismuth_vm as vm
from bismuth_vm import push

API = "http://127.0.0.1:3031"

# counter: storage[0] = storage[0] + 1 ; STOP
COUNTER = push(0) + bytes([vm.SLOAD]) + push(1) + bytes([vm.ADD]) + push(0) + bytes([vm.SSTORE, vm.STOP])


def _get(path):
    with urllib.request.urlopen(API + path, timeout=8) as r:
        return json.load(r)


def test_vm_deploy_and_call_post_fork(client):
    # the regnet chain signals hf2; mine until the tip has PASSED the cached activation height
    info = _get("/api/vm/contracts")
    assert info["enabled"], "vm not enabled on this node"
    for _ in range(25):
        info = _get("/api/vm/contracts")
        fh = info.get("fork_height")
        if fh is not None and client.block_height() > fh:
            break
        client.mine(3)
    assert info["fork_height"] is not None and client.block_height() > info["fork_height"], \
        "hf2 fork not active (tip %s, fork_height %s)" % (client.block_height(), info.get("fork_height"))

    before = set(info["contracts"])
    client.send(client.address, 1, "vm:deploy", COUNTER.hex())     # deploy the counter contract
    client.mine(2)
    sleep(0.4)
    # the deploy tx must actually be on chain (isolates "not mined" from "VM didn't run")
    ops = [t.get("operation") for t in
           _get("/api/address/%s/transactions?limit=50" % client.address).get("transactions", [])]
    assert "vm:deploy" in ops, "deploy tx not mined; recent ops=%s" % ops[:6]
    new = set(_get("/api/vm/contracts")["contracts"]) - before
    assert new, "deploy mined but the post-fork VM did not register the contract"
    addr = new.pop()

    for _ in range(2):                                             # call it twice
        client.send(client.address, 1, "vm:call", addr + ":")
        client.mine(1)
    sleep(0.4)

    detail = _get("/api/vm/contract/" + addr)
    slot0 = {s["key"]: s["value"] for s in detail["storage"]}.get("0")
    assert slot0 == "2", "counter should be 2 after two calls, got %s" % detail["storage"]


def test_unknown_contract_404(client):
    try:
        _get("/api/vm/contract/deadbeef" * 7)
        assert False, "expected 404"
    except urllib.error.HTTPError as e:
        assert e.code == 404
