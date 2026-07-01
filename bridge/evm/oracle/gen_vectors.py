#!/usr/bin/env python3
"""
gen_vectors.py — the cross-chain ORACLE for the wBIS bridge (doc/45).

It produces the exact test vectors the Solidity side must reproduce byte-for-byte:

  1. blake2b-256 KATs            -> proves Blake2b.sol (via the EIP-152 0x09 precompile) == hashlib.blake2b.
  2. vm_merkle EMPTY_ROOT        -> the fixed empty-state root constant baked into BismuthMerkle.sol.
  3. A faithful peg-in scenario  -> a `bridge_vault` lock recorded in VM state EXACTLY as
     vm_state._state_entries / vm_merkle would commit it, with the 6 storage leaves of the lock,
     the committed Merkle root, and an O(log n) inclusion proof per slot. The Solidity verifier
     reconstructs the same leaves from (vault_addr, lock_id, amount, eth_recipient) and must
     verify every proof against the root, then mint that (amount -> recipient).

Pure stdlib + the repo's vm_merkle (hashlib only). Run:  python3 oracle/gen_vectors.py
Writes:  fixtures/vectors.json
"""
import hashlib, json, os, sys

# import the repo's canonical Merkle implementation (the consensus one the Solidity must match)
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, REPO_ROOT)
import vm_merkle  # noqa: E402

_KEY = 32  # vm_state._KEY: storage keys/values are 32-byte big-endian words


# ----- mirror vm_state's leaf-preimage encoding (the ONLY source of truth is vm_state._state_entries) -----
def storage_key(addr: str, slot: int) -> bytes:
    return (addr + ":").encode() + int(slot).to_bytes(_KEY, "big")


def storage_leaf(addr: str, slot: int, value: int) -> bytes:
    return b"S" + storage_key(addr, slot) + int(value).to_bytes(_KEY, "big")


def code_leaf(addr: str, code: bytes) -> bytes:
    return b"C" + addr.encode() + len(code).to_bytes(4, "big") + code


def balance_leaf(addr: str, bal: int) -> bytes:
    return b"B" + addr.encode() + int(bal).to_bytes(_KEY, "big")


# ----- mirror bridge_vault.py's per-lock storage layout (stride 16; recipient = 10 sentinel'd 16-bit chunks) -----
# lock id n (1-based): slot[n*16+0]=amount ; slot[n*16+1+j] = 0x10000 | big-endian uint16 of eth_recipient[2j:2j+2],
# j=0..9. The 0x10000 sentinel keeps every chunk non-zero so it is never dropped as a deletion (always provable).
SENTINEL = 0x10000


def vault_lock_slots(lock_id: int, amount: int, eth_recipient: bytes):
    assert len(eth_recipient) == 20
    assert 0 < amount <= 0xFFFFFFFF, "VM callvalue is 32-bit (vm_engine refunds > 0xFFFFFFFF)"
    base = lock_id * 16
    slots = [(base + 0, amount)]
    for j in range(10):
        chunk = int.from_bytes(eth_recipient[2 * j:2 * j + 2], "big")
        slots.append((base + 1 + j, SENTINEL | chunk))
    return slots  # [(slot, value)] of length 11


def proof_to_json(proof):
    # vm_merkle proof element = (sibling_hash_or_None, sibling_is_left)
    return [{"sibling": (None if sib is None else sib.hex()), "siblingIsLeft": bool(left)}
            for (sib, left) in proof]


def main():
    # A synthetic-but-faithful VM state: the bridge vault + an unrelated contract, so the storage
    # sub-list interleaves by LMDB key order and the tree is a realistic odd size (exercises promotion).
    VAULT = "a1" * 28          # 56-hex vault address (blake2b-28), like vm_engine.contract_address
    OTHER = "c3" * 28          # an unrelated contract sharing the global state tree
    VM_SINK = hashlib.sha224(b"bismuth-vm-custody").hexdigest()

    # Two real peg-in locks recorded by FN_LOCK.
    locks = [
        {"id": 1, "amount": 4_2000_0000 & 0xFFFFFFFF, "recipient": bytes.fromhex("f4f82f8d84c529987201609cecee8ab136a50c8c")},
        {"id": 2, "amount": 1234_5678,                 "recipient": bytes.fromhex("00112233445566778899aabbccddeeff00112233")},
        # leading-zero / vanity recipient: chunks 0 and 1 are 0x0000 -> exercises the sentinel fix end-to-end
        {"id": 3, "amount": 9_9999,                    "recipient": bytes.fromhex("00000000aabbccddeeff00112233445566778899")},
    ]
    # cap amounts into the VM's 32-bit window
    for lk in locks:
        lk["amount"] &= 0xFFFFFFFF
        if lk["amount"] == 0:
            lk["amount"] = 1

    # Build the storage map for the vault (all locks) + a couple of unrelated slots on OTHER.
    storage = {}  # (addr, slot) -> value
    for lk in locks:
        for slot, val in vault_lock_slots(lk["id"], lk["amount"], lk["recipient"]):
            storage[(VAULT, slot)] = val
    # the vault also persists its lock counter at slot 0 (S_COUNTER), realistic interleave
    storage[(VAULT, 0)] = len(locks)
    storage[(OTHER, 7)] = 99
    storage[(OTHER, 0x1234)] = 1

    # Assemble _state_entries in the SAME order vm_state does:
    #   code entries (sorted by addr bytes), then storage (sorted by full key bytes), then balances (by addr).
    code = {VAULT: bytes.fromhex("deadbeef"), OTHER: bytes.fromhex("0102030405")}
    balances = {VAULT: 4_2000_0000 + 1234_5678, VM_SINK: 4_2000_0000 + 1234_5678}

    entries = []
    for addr in sorted(code, key=lambda a: a.encode()):
        entries.append(code_leaf(addr, code[addr]))
    for (addr, slot) in sorted(storage, key=lambda kv: storage_key(kv[0], kv[1])):
        entries.append(storage_leaf(addr, slot, storage[(addr, slot)]))
    for addr in sorted(balances, key=lambda a: a.encode()):
        entries.append(balance_leaf(addr, balances[addr]))

    root = vm_merkle.merkle_root(entries)

    # For each lock, emit the 6 slot leaves with their inclusion proofs against `root`.
    lock_vectors = []
    for lk in locks:
        slot_vecs = []
        for slot, val in vault_lock_slots(lk["id"], lk["amount"], lk["recipient"]):
            preimage = storage_leaf(VAULT, slot, val)
            idx = entries.index(preimage)
            proof = vm_merkle.merkle_proof(entries, idx)
            assert vm_merkle.verify_proof(root, preimage, proof), "oracle self-check failed"
            slot_vecs.append({
                "slot": slot,
                "value": val,
                "leafPreimage": preimage.hex(),
                "leafHash": vm_merkle.leaf_hash(preimage).hex(),
                "index": idx,
                "proof": proof_to_json(proof),
            })
        lock_vectors.append({
            "lockId": lk["id"],
            "amount": lk["amount"],
            "ethRecipient": "0x" + lk["recipient"].hex(),
            "slots": slot_vecs,
        })

    # Standalone blake2b-256 KATs for Blake2b.sol (independent of Merkle).
    def b2(x):
        return hashlib.blake2b(x, digest_size=32).hexdigest()
    kats = []
    for label, msg in [
        ("empty", b""),
        ("abc", b"abc"),
        ("leaf-tag-only", vm_merkle._LEAF),
        ("128B", bytes(range(128))),
        ("129B-two-blocks", bytes((i * 7) & 0xFF for i in range(129))),
        ("256B", bytes((i * 3) & 0xFF for i in range(256))),
        ("empty-state-domain", b"bismuth-vm-merkle/empty-state"),
    ]:
        kats.append({"label": label, "input": msg.hex(), "blake2b256": b2(msg)})

    out = {
        "_comment": "Oracle vectors for the wBIS bridge. Solidity must reproduce every hash/root/proof here. "
                    "Generated by bridge/evm/oracle/gen_vectors.py from vm_merkle.py (the consensus impl).",
        "vaultAddress": VAULT,
        "emptyRoot": vm_merkle.EMPTY_ROOT.hex(),
        "leafTag": vm_merkle._LEAF.hex(),
        "nodeTag": vm_merkle._NODE.hex(),
        "stateRoot": root.hex(),
        "numEntries": len(entries),
        "blake2bKats": kats,
        "locks": lock_vectors,
    }
    os.makedirs(os.path.join(os.path.dirname(__file__), "..", "fixtures"), exist_ok=True)
    dst = os.path.join(os.path.dirname(__file__), "..", "fixtures", "vectors.json")
    with open(dst, "w") as f:
        json.dump(out, f, indent=2)
    print(f"wrote {dst}")
    print(f"  stateRoot   = {root.hex()}")
    print(f"  emptyRoot   = {vm_merkle.EMPTY_ROOT.hex()}")
    print(f"  numEntries  = {len(entries)}  locks={len(lock_vectors)}  kats={len(kats)}")


if __name__ == "__main__":
    main()
