"""
Prediction-market contract — live post-fork integration on regnet (vm=True, fork_signal=True).

Drives the Polymarket-style binary market (contracts/prediction_market.py) end-to-end through real
vm:deploy / vm:call transactions, mined into the regnet chain past the hf2 fork:

  * deploy the market with the test wallet as resolver,
  * BUY YES and BUY NO by sending BIS to the VM custody sink (the deposits credit the contract),
  * assert the on-chain pots and the contract's custody balance,
  * RESOLVE (resolver-only; a wrong-resolver market proves access control on-chain),
  * CLAIM the pro-rata winnings and assert the consensus payout settled FROM the sink back to the wallet,
  * assert no-double-resolve and claim-once on chain.

The fine-grained per-user / edge-case matrix is covered exhaustively off-chain in
tests/test_contracts_offchain.py; this file proves the chain plumbing (custody in, TRANSFER out, gating).

Run with: python3 -m pytest tests/test_vm_prediction_market.py -v
"""
import json
import urllib.request
from time import sleep

import prediction_market as pm

API = "http://127.0.0.1:3031"
UNIT = 100_000_000          # 1 BIS in atomic units (regnet runs ledger_integer_amounts=True)


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
    raise AssertionError("hf2 fork not active (tip %s, fork_height %s)"
                         % (client.block_height(), info.get("fork_height")))


def _deploy(client, code_hex):
    """Deploy a contract; return its derived address (diff of the contract set before/after)."""
    before = set(_get("/api/vm/contracts")["contracts"])
    client.send(client.address, 1, "vm:deploy", code_hex)
    client.mine(2)
    sleep(0.3)
    new = set(_get("/api/vm/contracts")["contracts"]) - before
    assert new, "deploy mined but the VM did not register the contract"
    return new.pop()


def _call(client, addr, calldata_hex, amount=0):
    """Send a vm:call. To deposit, `amount` (BIS) is carried to the VM custody sink so it credits the
    contract; non-deposit calls send amount=0 to the wallet itself (no custody change)."""
    import vm_engine
    recipient = vm_engine.VM_SINK if amount else client.address
    client.send(recipient, amount, "vm:call", "%s:%s" % (addr, calldata_hex))
    client.mine(1)
    sleep(0.2)


def _detail(addr):
    d = _get("/api/vm/contract/" + addr)
    d["_slots"] = {int(s["key"]): int(s["value"]) for s in d["storage"]}
    return d


def _sel(n):
    return n.to_bytes(4, "big").hex()


def test_prediction_market_full_lifecycle(client):
    _wait_fork_active(client)
    wallet = client.address                                   # 56-hex; its 32-bit fold is the caller id
    resolver_id = pm.resolver_id_of(wallet)

    addr = _deploy(client, pm.build(resolver_id).hex())
    detail = _detail(addr)
    assert detail["engine"] == "riscv"

    # BUY YES with 2 BIS and BUY NO with 1 BIS (same wallet backs both sides — fine for the plumbing test).
    _call(client, addr, _sel(pm.FN_BUY_YES), amount=2)
    _call(client, addr, _sel(pm.FN_BUY_NO), amount=1)

    detail = _detail(addr)
    assert detail["_slots"].get(pm.S_YESPOT) == 2 * UNIT, detail["_slots"]
    assert detail["_slots"].get(pm.S_NOPOT) == 1 * UNIT, detail["_slots"]
    assert int(detail["balance"]) == 3 * UNIT, "contract custody should hold the 3 BIS deposited"

    # RESOLVE YES (selector | outcome=1). Resolver is the wallet, so this is authorised.
    _call(client, addr, _sel(pm.FN_RESOLVE) + (1).to_bytes(4, "big").hex())
    detail = _detail(addr)
    assert detail["_slots"].get(pm.S_RESOLVED) == 1, "market should be resolved"
    assert detail["_slots"].get(pm.S_OUTCOME) == 1, "winning outcome should be YES"

    # No double-resolve: a second RESOLVE reverts -> outcome unchanged.
    _call(client, addr, _sel(pm.FN_RESOLVE) + (2).to_bytes(4, "big").hex())
    assert _detail(addr)["_slots"].get(pm.S_OUTCOME) == 1, "double-resolve must not change the outcome"

    # CLAIM: winning side = YES, the wallet's YES stake is the entire YES pot, so payout = whole pot
    # (2+1 BIS) settled FROM the custody sink back to the wallet. recipient = wallet (28 bytes).
    bal_before = client.balance()
    _call(client, addr, _sel(pm.FN_CLAIM) + wallet)            # wallet is exactly 56 hex = 28 bytes
    detail = _detail(addr)
    assert int(detail["balance"]) == 0, "custody must be drained after the only winner claims"
    # claimed flag set for this account
    claimed_slot = pm.TAG_CLAIMED | (resolver_id & pm.USER_MASK)
    assert detail["_slots"].get(claimed_slot) == 1, "claim-once flag must be set"

    # The payout is a consensus negative-height ledger row from VM_SINK -> wallet; the wallet balance rose
    # by ~3 BIS (minus the tiny mining/tx accounting noise of the claim tx itself).
    bal_after = client.balance()
    assert bal_after - bal_before > 2.5, "winner should have received the ~3 BIS pot (got +%.4f)" % (
        bal_after - bal_before)

    # Claim-once: a second CLAIM reverts (custody stays 0, no new payout).
    _call(client, addr, _sel(pm.FN_CLAIM) + wallet)
    assert int(_detail(addr)["balance"]) == 0


def test_prediction_market_resolver_access_control(client):
    """A market whose resolver id is NOT the wallet's fold cannot be resolved by the wallet (on-chain)."""
    _wait_fork_active(client)
    not_me = (pm.resolver_id_of(client.address) ^ 0xFFFFFFFF) & 0xFFFFFFFF   # a different 32-bit id
    addr = _deploy(client, pm.build(not_me).hex())

    _call(client, addr, _sel(pm.FN_BUY_YES), amount=1)        # fund so resolve is the only thing missing
    _call(client, addr, _sel(pm.FN_RESOLVE) + (1).to_bytes(4, "big").hex())   # wallet != resolver
    assert _detail(addr)["_slots"].get(pm.S_RESOLVED) in (None, 0), \
        "the wallet is not this market's resolver and must not be able to resolve it"
