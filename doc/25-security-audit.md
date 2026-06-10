# doc/25 — Adversarial security audit (post-fork crypto & consensus)

Scope: the post-fork features added on this branch — shielded value incl. **RingCT confidential amounts**
(`shieldedv1.py`, `ringct.py`, `bulletproof.py`), the **native multisig signer** (`polysign/signer_multisig.py`),
the **DEX** VM contract (`contracts/dex.py`), the **REST write path** (`rest_api.py`), and the
**rollback/reorg** machinery they depend on (`chain_ops.py`, `vm_engine.py`). The audit threat-models the
well-known crypto/blockchain attack classes of the past decade. Crypto was tested empirically, not assumed.

**Method.** Each primitive was validated adversarially *in isolation before consensus wiring* (an
amount-privacy bug is silent inflation). The audit then red-teamed the integrated consensus paths with two
independent reviewers (rollback completeness; validate/apply soundness + determinism) plus direct crypto
attacks. Every finding below has a regression test.

## Findings & fixes

| # | Severity | Class | Finding | Fix | Test |
|---|----------|-------|---------|-----|------|
| 1 | **Critical** | Inflation (range-proof soundness) | The Bulletproof verifier trusted the proof's bit-width `n`. An attacker sets `n=256`, proves a "negative" amount `v = N−k ≈ 2²⁵⁶` is "in range", and — because the RingCT balance check holds mod N and the MLSAG ties to the real input — **mints supply from nothing**. | Pin `n == RANGE_BITS` (and `1 ≤ m ≤ MAX_AGG`) in `bulletproof._verify_prep`. | `test_bulletproof::test_bit_width_is_pinned_no_wraparound_bypass` |
| 2 | **High** | Rollback / reorg (VM custody inflation + consensus wedge) | `blocknf()` (the live reorg handler) and both `sequencing_check()` branches rolled back the ledger + shielded + token sidecars but **never rebuilt `vm_state`**. After a reorg, contract custody balances outrun their `VM_SINK` ledger backing (custody inflation/double-spend), and a stale `vm_state_root` makes the mandatory state-root check reject the canonical branch (consensus wedge). Only `chain_ops.rollback()` was correct. | One shared `chain_ops._rebuild_derived_state(node, db, keep_height)` (VM rebuild + root recompute + balance index + aux stores) called on **every** rollback path. | `test_vm_rollback::test_vm_custody_and_state_root_roll_back` (drives the `regtest_rollback`→`rollback` path, same helper) |
| 3 | **Medium** | Validate-pass / apply-fail (sidecar desync) | A v3 confidential spend output normally has no `amt` (hidden in `C`), but an attacker could attach a junk `amt` (`"x"`, `1.5`, `{}`) that **passed `validate_block`** yet threw `int(amt)` in `apply_block`. Since apply runs *after* `to_db` under a swallowed `except`, the block commits while the shielded sidecar silently, permanently desyncs (key image burned, outputs dropped) — uniformly on every node. | Validate `amt` (when present) as a non-negative non-bool int in `_require_confidential_note`. | `test_ringct_consensus::test_v3_spend_output_junk_amt_rejected_at_validate` |
| 4 | Low | Fork-activation consistency | The native-multisig fork-gate used `<= fork_height` (reject), activating multisig **one block later** than the shield/VM gates (`>= fork_height`). Benign for split-safety (more conservative) but a spec inconsistency. | `digest.py` gate changed to `< fork_height`, aligning activation. | (consistency; covered by the multisig live test post-fork) |

## Attack classes checked and the defense that holds

- **Counterfeiting / range-proof soundness (Monero 2017, Zcash 2018 class).** Beyond finding #1: out-of-range
  and field-wraparound ("negative") outputs are rejected by the Bulletproof; the MLSAG rejects an
  amount-mismatched pseudo-output; balance is the point equality `C_pseudo == ΣC_out`. Mint/redeem are the
  transparent boundary so supply stays auditable at the pool edge. (`test_bulletproof`, `test_ringct`.)
- **Fiat-Shamir incompleteness / "Frozen Heart" (2022 Bulletproofs/PlonK class).** The transcript absorbs
  the value commitments `V`, then `A,S` → `y,z`; `T1,T2` → `x`; `t̂,τx,μ` → `w`; each IPA `(L,R)` → `u`; then a
  **merge challenge `c`** that combines the two verification sub-equations so neither can offset the other.
  Every public input is bound before the challenge that uses it — changing any (`V`, `A`, `S`, `T1`, `T2`)
  flips the challenges and rejects. (`test_bulletproof::test_frozen_heart_value_commitment_is_bound`, the
  every-field-tamper test.)
- **Pedersen binding / generator independence.** `H` (value) and `G` (blind) and the vector generators and
  `Q` are NUMS `hash_to_point` outputs with distinct domains — all distinct, no known discrete-log relation,
  so a commitment cannot be opened to two values. `ringct.H == bulletproof.H` so a BP `V` equals a RingCT
  output commitment. (`test_bulletproof::test_generators_are_independent`.)
- **ECDSA signature malleability (BIP62/BIP146, txid-malleability class).** libsecp256k1 enforces low-s on
  verify, so a high-`s` malleated multisig component signature is rejected. (`test_multisig_signer::test_signature_malleability_low_s_rejected`.)
- **Double-spend / key-image attacks (Monero key-image class).** The spent-set keys on the **canonicalised
  (compressed)** key image (closing the compressed-vs-uncompressed bypass); an intra-block guard (`seen_images`)
  rejects two ops with the same key image in one block; a spend and a redeem of the same note share a key
  image so only one can land; a reorg correctly clears the key image (note spendable again on the new branch).
  (`test_ringct`, `test_ringct_consensus`, `test_shielded`.)
- **Cross-version confusion.** A v3 confidential spend that references a v2 (transparent) note as a ring
  member is **cleanly rejected** (the v2 `commitment` column is a sha256 hash, not a 33-byte point, so
  deserialisation fails closed) — no crash.
- **Consensus determinism.** All randomness is confined to signing/proving; the verify path (`verify_spend`,
  `mlsag_verify`, `bulletproof.verify`, multisig verify) is pure — no time, float, env, or order-dependent
  result. The randomized `batch_verify` is **not** wired into consensus (`verify_spend` uses the deterministic
  single `bp.verify`). Canonical JSON (`sort_keys`) makes signer and verifier byte-identical.
- **Multisig threshold / ordering / replay.** ≥ M distinct valid sigs at strictly-increasing owner indices
  (kills reorder-malleability and one-owner-two-slots); the address is rebuilt from the redeem and matched;
  BIP67-sorted redeem (one accepted redeem per address); sigs are over the canonical buffer binding every tx
  field (replay handled by Bismuth's existing dup-signature + timestamp-window guards). (`test_multisig_signer`.)
- **Smart-contract classes (DEX).** No reentrancy (the VM has no external calls mid-execution; transfers are
  queued and applied only on success). No integer-overflow inflation (token supply is conserved; no balance
  can exceed the 32-bit supply). Access control enforced (admin-only mint, maker-only cancel). Honest 32-bit
  amount cap documented (over-deposit is stuck, not stealable). (`test_dex`.)
- **DoS bounds.** Ring ≤ `MAX_RING`, outputs ≤ `MAX_OUTPUTS`, aggregate ≤ `MAX_AGG`; the IPA proof length is
  pinned to `log₂(nm)` (can't be padded); the REST write body is capped; `hash_to_point` is variable-time only
  over public inputs.

## Residual / out of scope (honest)
- **`blocknf` cannot be unit-tested in regnet** — it is triggered by peer consensus reporting block-not-found,
  which needs a two-node harness (the same reason `node.py` `handle()` dispatch is deferred). The fix wires the
  *same* `_rebuild_derived_state` helper that the tested `rollback()` path uses, so it is correct by
  construction; a two-node integration test is the follow-up.
- **`sequencing_check` height skew.** The sidecar deletes key on `y` (first missing height) while the ledger
  deletes on `row[0] ≥ y`; the VM rebuild here matches the ledger boundary (`row[0]−1`). The sidecar over-rolls
  (safe direction — never the reverse) on a startup corruption-recovery path that forces a full resync; the
  skew is documented, not a double-spend.
- **PoW-economic attacks** (51%, selfish mining, time-warp) and **network-layer** attacks (eclipse) are
  properties of the base chain, unchanged by this branch.
- **Multi-input RingCT, BIP62 low-s for the legacy ECDSA signer, and RingCT mempool key-image dedup** are
  documented enhancements, not soundness gaps.
