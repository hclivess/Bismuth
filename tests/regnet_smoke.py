"""
Self-contained regnet smoke / characterization check.

Unlike the pytest suite, this has NO external dependencies (no bismuthclient / bismuthcore /
tornado / ed25519): it speaks the node protocol with the in-tree ``connections`` module and
exercises the reintegrated in-tree ``polysign`` directly. It is meant as a fast, dependency-light
gate that runs on any modern Python where the node itself runs.

Usage:
    # terminal 1
    python3 node.py regnet2
    # terminal 2
    python3 tests/regnet_smoke.py

Exits 0 if every check passes, 1 otherwise.
"""
import base64
import hashlib
import os
import socket
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import connections  # noqa: E402  (in-tree wire protocol)

PORT = 3030
_failures = 0


def call(cmd, *args, timeout=60):
    s = socket.socket()
    s.settimeout(timeout)
    s.connect(("127.0.0.1", PORT))
    connections.send(s, cmd)
    for a in args:
        connections.send(s, a)
    res = connections.receive(s)
    s.close()
    return res


def check(name, condition, detail=""):
    global _failures
    status = "PASS" if condition else "FAIL"
    if not condition:
        _failures += 1
    print(f"  [{status}] {name}" + (f"  ({detail})" if detail else ""))


def check_node_consensus():
    """Generate blocks and confirm the chain advances and the miner is rewarded."""
    before = call("statusjson")["blocks"]
    reply = call("regtest_generate", 12)
    after = call("statusjson")["blocks"]
    check("regtest_generate returns OK", reply == "OK", repr(reply))
    check("chain advances by exactly 12 blocks", after == before + 12, f"{before} -> {after}")

    address = call("statusget")[0]
    balance = call("balanceget", address)            # (balance, credit, debit, fees, rewards, ...)
    check("miner balance is positive after rewards", float(balance[0]) > 0, f"balance={balance[0]}")
    check("reward column accrued to miner", float(balance[4]) > 0, f"rewards={balance[4]}")

    last = call("blocklastjson")
    bh = last.get("block_hash", "")
    check("last block hash is well-formed (56 hex)",
          len(bh) == 56 and all(c in "0123456789abcdef" for c in bh), bh)

    # dbhandler.tokens_user SQL fix: must not raise, returns empty for a token-less address
    check("tokensget does not crash and is empty", call("tokensget", address) == [])
    check("mempool is empty", call("mpget") == [])
    return address


def check_signature_reintegration():
    """RSA sign + verify through the node's own essentials, backed by the in-tree polysign."""
    import essentials
    from polysign.signerfactory import SignerFactory
    from Cryptodome.PublicKey import RSA

    key = RSA.generate(1024)  # small key = fast; the node accepts 1024 and 4096
    pub_pem = key.publickey().exportKey().decode("utf-8")
    pub_b64 = base64.b64encode(pub_pem.encode("utf-8"))
    address = hashlib.sha224(pub_pem.encode("utf-8")).hexdigest()

    check("address_is_valid / address_is_rsa agree",
          SignerFactory.address_is_valid(address) and SignerFactory.address_is_rsa(address))

    ts = "%.2f" % time.time()
    signed = essentials.sign_rsa(ts, address, address, 1.0, "", "", key, pub_b64)
    check("essentials.sign_rsa returns a signed 8-tuple",
          isinstance(signed, tuple) and len(signed) == 8)

    buffer = str((ts, address, address, "%.8f" % 1.0, "", "")).encode("utf-8")
    try:
        SignerFactory.verify_bis_signature(signed[4], pub_b64.decode(), buffer, address)
        verified = True
    except Exception:
        verified = False
    check("verify_bis_signature accepts the produced signature", verified)


def check_fee_characterization():
    """Pin the consensus fee formula: 0.01 + len(openfield)/100000, +1 for alias=, +10 for token:issue."""
    from essentials import fee_calculate
    check("fee of empty tx == 0.01", str(fee_calculate("")) == "0.01000000", str(fee_calculate("")))
    check("fee of 50-char openfield == 0.0105",
          str(fee_calculate("x" * 50)) == "0.01050000", str(fee_calculate("x" * 50)))
    check("alias= surcharge is +1", str(fee_calculate("alias=bob")) == "1.01009000",
          str(fee_calculate("alias=bob")))
    check("token:issue surcharge is +10",
          str(fee_calculate("", "token:issue")) == "10.01000000", str(fee_calculate("", "token:issue")))


def main():
    print("Bismuth regnet smoke / characterization")
    print("- consensus / digest / rewards:")
    check_node_consensus()
    print("- signature reintegration (in-tree polysign):")
    check_signature_reintegration()
    print("- fee formula characterization:")
    check_fee_characterization()
    print(f"\n{'ALL CHECKS PASSED' if _failures == 0 else str(_failures) + ' CHECK(S) FAILED'}")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
