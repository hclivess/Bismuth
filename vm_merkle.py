"""
vm_merkle.py — a deterministic binary Merkle commitment over the VM state, with compact O(log n) inclusion
proofs (doc/45 bridge, Stage 2). The flat ``vm_state.state_root`` proves "here is a hash of ALL state"; this
proves "entry E is in the committed state" with a log-sized path — what a remote light-client / zk verifier
needs to confirm a single locked-BIS entry without re-hashing the whole store.

Pure + dependency-free (stdlib ``hashlib`` only) so it is trivially re-implementable inside a zk circuit and
in a Solidity verifier. Defined over the SAME ordered leaf-preimage sequence as ``vm_state._state_entries``
(code, then storage, then balances, each in LMDB key order), so the Merkle root commits to byte-identical
state as the flat root.

Hashing (blake2b-256, matching the rest of Bismuth's consensus hashing):
  * domain-separated leaf  : H(0x00 || preimage)
  * domain-separated node  : H(0x01 || left || right)   (the 0x00/0x01 tags give second-preimage resistance,
                             RFC 6962 style — a leaf can never be reinterpreted as an internal node)
  * a lone node at a level is PROMOTED unchanged (carried up), NOT duplicated — avoiding the
    CVE-2012-2459 duplicate-leaf root collision. A proof carries one element per level (``None`` on a
    promoted level), so ``verify_proof`` needs only (root, leaf, proof) — no tree size.
"""
import hashlib

_LEAF = b"\x00"
_NODE = b"\x01"
# Fixed root of the empty state (no entries). Distinct from any real leaf/node hash.
EMPTY_ROOT = hashlib.blake2b(b"bismuth-vm-merkle/empty-state", digest_size=32).digest()


def _h(*parts):
    x = hashlib.blake2b(digest_size=32)
    for p in parts:
        x.update(p)
    return x.digest()


def leaf_hash(preimage):
    return _h(_LEAF, bytes(preimage))


def _node(left, right):
    return _h(_NODE, left, right)


def _levels(preimages):
    """All tree levels bottom-up: levels[0] = leaf hashes, levels[-1] = [root]."""
    level = [leaf_hash(p) for p in preimages]
    levels = [level]
    while len(level) > 1:
        nxt = []
        for i in range(0, len(level), 2):
            nxt.append(_node(level[i], level[i + 1]) if i + 1 < len(level) else level[i])
        level = nxt
        levels.append(level)
    return levels


def merkle_root(preimages):
    """32-byte root over the ordered leaf preimages. Empty -> EMPTY_ROOT; single leaf -> its leaf hash."""
    if not preimages:
        return EMPTY_ROOT
    return _levels(preimages)[-1][0]


def merkle_proof(preimages, index):
    """Inclusion path for ``preimages[index]``: a list of (sibling_hash_or_None, sibling_is_left), one entry
    per tree level (None where the node was promoted). Pair with ``verify_proof``."""
    if index < 0 or index >= len(preimages):
        raise IndexError(index)
    levels = _levels(preimages)
    proof, idx = [], index
    for level in levels[:-1]:                       # every level except the root
        sib = idx ^ 1
        if sib < len(level):
            proof.append((level[sib], (idx & 1) == 1))   # sibling sits on the left iff idx is odd
        else:
            proof.append((None, False))             # lone node promoted this level — no sibling
        idx //= 2
    return proof


def verify_proof(root, preimage, proof):
    """True iff ``preimage`` hashes up through ``proof`` to ``root``. Self-contained — needs no tree size."""
    h = leaf_hash(preimage)
    for sib, sib_is_left in proof:
        if sib is None:
            continue                                # promoted level: carry up unchanged
        h = _node(sib, h) if sib_is_left else _node(h, sib)
    return h == root
