# 31 — The Accuser Subsystem

A PoW-native adaptation of the Tezos accuser for Bismuth. The accuser **detects, proves, and propagates miner equivocation** and feeds that signal into Bismuth's existing finality substitutes (checkpoints, reorg-depth caps, peer reputation). It does **not** invent a new fork-choice rule and it does **not**, in its first phase, change consensus.

> **Status:** Phase 1 is inert-by-default, regnet-testable, and gossip-gated. Phase 2 (on-chain slashing) and the proof's non-forgeability both require a **consensus change that folds into the single `hf2` fork** — see [§1 Motivation](#1-motivation) and the [Critical-finding box in §5](#52-why-the-proof-is-non-forgeable--only-after-the-hf2-binding-fix).

---

## 0. Reading guide — what the adversarial review changed

This document is the *post-review* design. The adversarial review found one critical flaw that invalidated the original "ships now as a non-forgeable proof" framing, plus several weaponization and DoS surfaces. The design below has been rewritten around those findings. Each place a review finding changed the design is marked inline:

> **[REVIEW: Cx/Hx/Mx/Lx]** — explains the original design, the finding, and the resulting change.

The headline reversal: **the coinbase signature in Bismuth does not commit to the block hash**, so an equivocation "proof" is *forgeable by signature-lifting* until a consensus change binds the signature to the block. Consequently:

- **Phase 1 ships as a purely local, advisory, non-gossiped heuristic** — it never drives a network-propagated blacklist or peer ban.
- **The genuinely non-forgeable proof, network gossip, and any cross-node penalty are deferred to `hf2`**, alongside the signature-binding consensus change and the staking bond.

This is a more honest split than the original "detect/prove/propagate now, slash later." Detection runs now; *trustworthy* proof and propagation are part of hf2.

---

## 1. Motivation

### 1.1 The concrete reason this matters: this node just suffered difficulty-state corruption from a rollback

This is not a theoretical exercise. A node in this deployment recently experienced **difficulty-state corruption following a chain rollback** (task #21): after `chain_ops.rollback()` truncated the ledger, the re-derived difficulty did not match what a fresh recomputation from the truncated ledger would produce. The chain's derived state and its persisted state silently disagreed. That class of bug — *a rollback leaving the node in a self-inconsistent state* — is exactly what an adversary triggering reorgs wants to induce, and it is exactly what we currently have no systematic detector for.

The lesson is twofold:

1. **Rollbacks are a first-class attack and fragility surface.** Every reorg is a moment where the node mutates consensus-critical derived state (`difficulty()`, hyperblock indexes, balances). We must both *harden* the rollback path against self-corruption (Stage 2b below, independent of the accuser) and *detect* when a reorg is adversarial rather than an honest partition heal.
2. **We have no attribution today.** When a competing chain forces a rollback, the node cannot tell "honest partition heal" from "attacker out-mining a shadow fork," and it cannot attribute a same-height double-block to the miner that produced it. The accuser adds that attribution where — and only where — it is cryptographically sound.

### 1.2 Why an accuser at all, in a no-stake PoW chain

The Tezos `octez-accuser` rests on four primitives Bismuth's base layer does not have:

- **P1** — bonded identity (a slashable security deposit per baker).
- **P2** — deterministic slot assignment (exactly one baker is entitled to a given slot, so two blocks for that slot is provably illegitimate).
- **P3** — a self-incriminating signature (a baker signs each block/endorsement with their consensus key, so two signatures over two conflicting messages for one slot is a binding admission).
- **P4** — BFT finality (Tenderbake), which makes "two finalized conflicting blocks" a hard fault.

Bismuth has **none of P1, P2, P4**. The *only* primitive with a clean PoW analog is **P3**: every Bismuth block carries a **coinbase (mining) transaction signed by the miner's key** (`digest_tx.py`, coinbase branch). That signature is the one thing that can attribute fault to a *key* rather than an IP — which is what lets evidence survive gossip, replay, and sybil noise.

The accuser therefore extracts maximum value from P3 and feeds it into Bismuth's *existing* PoW finality substitutes rather than inventing new consensus:

- **Hard finality floor** — `validation_exceptions.MAINNET_CHECKPOINTS` + `verify_checkpoint` (invoked from `digest.py`): a reorg below a checkpoint height HALTS.
- **Soft reorg-depth cap** — `essentials.checkpoint_set` / `rollback_allowed` / `rollback_consensus`: deep rollbacks require a reputable-peer supermajority.
- **Peer reputation** — `peers_reputation.py`: `PENALTY_INVALID_BLOCK = 40`, auto-ban at `REP_BAN_BELOW = -50`.

> **The honest framing (post-review).** The original §8 rationale claimed "P3 ships now, no consensus change." That is **false on this codebase**: P3's binding admission requires the signature to commit to the conflicting message, and Bismuth's coinbase signature does *not* commit to the block hash (see [§5.2](#52-why-the-proof-is-non-forgeable--only-after-the-hf2-binding-fix)). So the truthful statement is: **the *detection heuristic* ships now (local, advisory); the *binding P3 proof* and everything built on it ship in hf2.**

---

## 2. Threat model

**Adversary capabilities.**

- Can mine blocks (including, at 51%, out-mining the honest chain and forcing arbitrarily deep reorgs up to the checkpoint floor).
- Can observe all gossip and all coinbase signatures already on-chain.
- Can submit arbitrary messages to permissionless endpoints (`POST /api/transaction`, and any new accuser endpoints).
- Can sybil/eclipse a node's peer set (mitigated, not eliminated, by reputation + whitelist).
- **Cannot** forge a signature for a key it does not hold.
- **Can**, however, *reuse* an existing valid signature wherever the signed pre-image is identical (this is the crux — see C1 below).

**What we defend.**

- **Liveness / no self-eclipse:** the accuser must never let a single message partition the network or ban honest peers en masse.
- **No framing:** the accuser must never attribute fault to a miner that did not actually equivocate.
- **No new DoS:** accuser endpoints must not give an attacker an asymmetric CPU/memory amplification.
- **No honest-partition harm:** a node rejoining after downtime must still be able to roll back to the true chain.
- **Rollback integrity:** a rollback must leave the node in a self-consistent derived state.

**Out of scope / explicitly not solved.**

- **The accuser does not prevent malicious rollbacks.** PoW reorg is decided by accumulated work; the accuser adds no work and cannot stop a 51% attacker.
  > **[REVIEW: H1]** The original §4.2 included a "refuse the malicious reorg toward an equivocator's fork" lever presented as prevention. The review showed this lever is *invertible*: an attacker who can forge a proof (C1) against the honest fork's miners makes honest nodes refuse the honest reorg, i.e. the "prevention" mechanism can be turned into a tool that *forces* a malicious rollback. **The fork-choice-override lever is removed.** The accuser feeds the checkpoint floor and reorg-depth cap; it never overrides PoW fork-choice based on accusation state. Real rollback prevention remains exactly the already-shipped `verify_checkpoint` hard floor and reorg-depth cap.

---

## 3. What the accuser detects

### 3.1 Primary: miner equivocation (the only candidate attributable offense)

**Definition.** Two blocks `A`, `B` with `A.block_height == B.block_height`, `A.miner_address == B.miner_address`, `A.block_hash != B.block_hash`, **and** — *this is the part Bismuth cannot honor today* — two **distinct signatures**, each committing to its own distinct block, both verifying against that miner's key.

> **[REVIEW: C1, M2]** The original definition triggered on "same miner, same height, two different block hashes, each carrying a coinbase signature that verifies." The review showed (a) the coinbase signature does **not** commit to the block hash, so a single lifted signature satisfies the trigger for two unrelated blocks (forgeable), and (b) **honest pool re-templating** at the same height (adding one more mempool tx, re-broadcasting) routinely produces *a different block hash with the same coinbase signature* — a false positive against honest pools. **The trigger is therefore redefined to require two distinct signatures each binding their own block**, which is only achievable after the hf2 binding fix in [§5.2](#52-why-the-proof-is-non-forgeable--only-after-the-hf2-binding-fix). Pre-fork, the same-coinbase-different-hash event is treated as a *weak local advisory heuristic only*, never a proof.

A re-broadcast or independently re-mined **byte-identical** block (same hash) is never an offense.

**Index.** `accuser.py` maintains a bounded in-memory LRU keyed by `(miner_address, height)`:

```python
# accuser.py
class EquivocationIndex:
    # (miner_address, height) -> CoinbaseWitness(block_hash, signature, public_key,
    #     timestamp, recipient, amount, operation, openfield, sig_binds_block_hash: bool)
    def observe(self, witness) -> Optional["EquivocationProof"]:
        key = (witness.miner_address, witness.height)
        prior = self._seen.get(key)
        if prior is None:
            self._seen[key] = witness          # first block at this slot — normal
            return None
        if prior.block_hash == witness.block_hash:
            return None                        # re-broadcast / re-mined identical — NOT an offense
        # Two distinct block hashes, same miner+height.
        # PRE-hf2: signatures do not bind the block hash -> NOT a proof, advisory only.
        # POST-hf2: each signature binds its own block hash -> candidate proof.
        if not (prior.sig_binds_block_hash and witness.sig_binds_block_hash
                and prior.signature != witness.signature):
            return None                        # cannot prove fault; emit a local advisory instead
        return EquivocationProof(prior, witness)
```

The index is bounded (`maxlen` ≈ a few thousand recent heights) and **pruned below `node.checkpoint`** — an offense older than the last hard checkpoint is moot, the natural PoW analog of the Tezos accusation window, here bounded by the rollback floor.

**Hook points** (all observe; none reject — equivocation across forks is not invalid PoW):

1. **`digest.py`, immediately after a block fully validates** (after `verify_proof_of_work` and the coinbase signature check, where `miner_tx.miner_address` is set). Every *accepted* block's coinbase witness is fed to `observe`. Catches equivocation on the chain we follow.
2. **`chain_ops.rollback()` / `chain_ops.blocknf()`** — capture the *displaced* tip's coinbase witness via the existing `backup_higher()` path before it is dropped. The displaced block and its same-height replacement, same miner = equivocation surfacing *during a reorg*, the highest-signal site.
3. **Inbound block-serving paths** (`node.py` `block`/`blocksfnd`, `worker.py` sync loop): feed candidate blocks at heights we already have, even if not adopted.
   > **[REVIEW: M4]** The original design let hook #3 (shadow-fork blocks the node never adopts and no one else saw) feed the blacklist. The review noted this makes the gossiped blacklist **non-deterministic and gameable** (feed different shadow blocks to different nodes). **Hook #3 now only feeds the *local advisory* and only triggers a network-relevant proof when both blocks are PoW-valid and independently verifiable by every node.** Blacklist effects (Phase 2) are a deterministic function of *gossiped, persisted* proofs only — never of locally observed shadow forks.

### 3.2 Secondary: checkpoint-violating / over-deep reorg offers (event, not fault)

A peer that offers a reorg rewriting history **below a `MAINNET_CHECKPOINTS` height** (already caught by `verify_checkpoint` → `ValueError` → HALT) or **deeper than the reorg-depth cap** (`node.checkpoint` from `checkpoint_set`) is a detectable *event*. It is provable (`{common_ancestor, old_tip_header, offered_tip_header, depth}` — anyone recomputes the PoW chain) but it is **not** non-forgeable proof of an attack: a deep reorg can be an honest partition heal.

It feeds **local reputation policy only** (down-rank + refuse-as-PoW-fork-choice-already-does), never an on-chain accusation, never a cross-network blacklist. Hook: `essentials.rollback_allowed()` returning `False`, or `verify_checkpoint` raising, emits a `ReorgAlert` for logging + reputation.

---

## 4. Evidence format (non-forgeable — *only after* the hf2 binding fix)

### 4.1 `EquivocationProof` wire shape

```jsonc
{
  "type": "equivocation",
  "miner_address": "<bis address>",
  "height": H,
  "block_a": {
    "block_hash": "...",
    "timestamp": t_a, "recipient": "...", "amount": "0",
    "operation": "...", "openfield": "...",
    "public_key": "...", "signature": "..."
  },
  "block_b": { /* same fields, different block_hash AND different signature */ }
}
```

Each `block_x` carries exactly the inputs needed to reconstruct the **signed pre-image of the coinbase** and verify the signature, i.e. the inputs `SignerFactory.verify_tx_signature()` consumes (`polysign/signerfactory.py`): `timestamp, address, recipient, amount, operation, openfield, signature, public_key` — **plus** the `block_hash` the coinbase must commit to *after the hf2 binding fix*.

### 4.2 Hard size and structural bounds

> **[REVIEW: L3, H2]** The original proof carried two coinbase `openfield` blobs (each up to `[:100000]`) with no bound, and verification ran before any structural check. **A proof now has a hard serialized-size cap and is structurally pre-checked before any crypto runs** (see [§7.2](#72-anti-dos-mandatory-on-every-endpoint)). Oversized or malformed proofs are rejected without a signature verification.

### 4.3 Stateless, deterministic verification

```python
# accuser.py
def verify_equivocation(proof) -> bool:
    a, b = proof["block_a"], proof["block_b"]
    # ----- cheap structural checks first (no crypto) -----
    if not _within_size_bounds(proof):              # L3 / H2
        return False
    if a["block_hash"] == b["block_hash"]:          # MUST be two DIFFERENT blocks (C1/M2)
        return False
    if a["signature"] == b["signature"]:            # MUST be two DISTINCT signatures (C1)
        return False
    if a["height"] != b["height"]:                  # bind height of BOTH blocks (L1)
        return False
    # ----- POST-hf2 only: signatures must bind their own block_hash -----
    if not (_sig_binds_block_hash(a) and _sig_binds_block_hash(b)):
        return False                                # pre-fork: unprovable -> not a proof
    # ----- now the expensive crypto -----
    ok_a = SignerFactory.verify_tx_signature(a["signature"], a["public_key"], _preimage(a))
    ok_b = SignerFactory.verify_tx_signature(b["signature"], b["public_key"], _preimage(b))
    if not (ok_a and ok_b):
        return False
    # both keys must resolve canonically to the accused miner_address
    return (address_of(a["public_key"]) == proof["miner_address"]
            == address_of(b["public_key"]))
```

> **[REVIEW: L1]** The original pseudocode contained `if proof["height"] != proof["height"]:` — a value compared to itself, always false, leaving the height binding unguarded. **Fixed:** compare the two blocks' heights, and bind height into each block's signed/hashed pre-image.

> **[REVIEW: M1]** Coinbase transactions are **RSA-only** (`digest_tx.py`: *"Coinbase (Mining) transaction only supports legacy RSA Bismuth addresses"*). RSA signatures can be malleable/non-canonical. **`verify_equivocation` pins the exact consensus RSA verify path, asserts canonical encoding, and `address_of()` must be the canonical, collision-resistant derivation used by consensus.** Characterization vectors are required (see Rollout). If coinbase remains RSA post-hf2, the secp256k1 branch is dead code for coinbases and must be asserted unreachable for `type == "equivocation"`.

### 4.4 Why this is non-forgeable — and why only post-fork

The argument the original design made — *"only the holder of the key could have produced both signatures over two distinct block hashes"* — is the correct argument, **but it only holds if each signature actually commits to its block hash.** On today's Bismuth it does not. See the dedicated section below.

---

## 5. The binding defect and its hf2 fix (the critical review finding)

### 5.1 What the coinbase signature actually commits to

Verified against the codebase:

- `bismuth_serialize.signature_buffer()` signs over exactly `(timestamp, address, recipient, amount, operation, openfield)`. **`block_hash` is not an input.**
- Post-fork, `verify_tx_signature` signs `tx_id_v2_s` over the **same six content fields** (`polysign/signerfactory.py`). Block hash is absent in both schemes.
- For a coinbase (`digest_tx.py`): `amount` is forced to `0`, `recipient`/`address` are the miner address, and the PoW `nonce` lives in `openfield[:128]` (pre-fork; **[doc/41](41-hf2-coinbase-free-fields.md)** relocates the post-fork nonce into the coinbase `signature` slot and the `"vmsr"<root>`+signal into the `public_key` slot, leaving `operation`/`openfield` optional free-form).
- The `block_hash` itself (`block_hash_at`, `digest.py`) is `sha224`/`blake2b` over the **whole transaction list + previous hash** — it is **not signed by the miner**.

### 5.2 Why the proof is non-forgeable — *only after* the hf2 binding fix

> **[REVIEW: C1] — CRITICAL. This finding reshaped the entire document.**
>
> **Original claim (§2.2):** "the coinbase signature binds the miner's private key to that exact block." **This is false on this codebase.**
>
> **The attack:** A miner's coinbase signature for height `H` is valid for *any* block at height `H` that reuses the same `(timestamp, openfield/nonce, amount=0, recipient=miner)` coinbase payload. An attacker who observes one signed coinbase can **lift that signature verbatim** onto a *second* block at the same height built with a different transaction set (→ different `block_hash`). Both `block_a` and `block_b` pass the original `verify_equivocation` because both carry a signature that genuinely verifies — **yet the honest miner produced and signed only one block.** Worse, anyone can do their *own* PoW on a block that embeds the victim's coinbase tx + signature, framing the victim with a same-height block they never made. The signature is a binding admission of *one coinbase payload* (reusable across blocks), not of *two blocks*.
>
> **Consequence:** as originally designed the proof is **forgeable / replayable and is a framing weapon against honest miners.** It cannot be called non-forgeable, cannot be gossiped as a "proof," and cannot drive any cross-node penalty.
>
> **The fix (consensus change, folds into the single `hf2` fork):** make the coinbase signature commit to the block. Concretely, fold the `block_hash` (equivalently: the parent hash + the merkle/`block_hash_at` root of the transaction list) **into the coinbase signed pre-image**. This dovetails with the already-decided hf2 work: hf2 already makes the signature sign the content `txid` (see the txid-nado decision) and reworks serialization; binding the coinbase signature to the block hash is the same class of change and **must ride the same single `hf2` fork** — never a second fork signal.
>
> **doc/41 interaction (post-fork coinbase has no signature).** [doc/41](41-hf2-coinbase-free-fields.md) repurposes the post-fork coinbase `signature` slot to carry the PoW **nonce** and the `public_key` slot to carry the `"vmsr"<root>`+signal mining header — the coinbase is PoW-authorized and **never signature-verified**. So the binding admission above cannot be "the coinbase signature commits to the block hash" post-fork; the hf2 binding must instead attach the block-hash commitment to the coinbase **mining header** (the `public_key`-slot commitment), or bind a distinct miner-signed artifact. This sharpens Open question #2 (RSA-forever for coinbases?) below.
>
> **Until that lands, equivocation is not provable on this chain.** Therefore:
> - **Phase 1 is downgraded** from "non-forgeable fraud proof" to a **two-distinct-blocks-same-coinbase heuristic, used for *local* fork-choice distrust only** — never gossiped as a proof, never feeding a blacklist other nodes act on. Every "non-forgeable"/"proof" claim is re-labeled "advisory heuristic" pre-fork.
> - The genuinely non-forgeable proof, gossip, and slashing are all **post-hf2**, gated on `node.fork_height`.

### 5.3 The corrected guarantee

After the hf2 binding fix: the coinbase signature commits to the block hash, so two *distinct* signatures over two *distinct* block hashes at one height, both verifying against the same key, can only have been produced by the key holder. The verifier needs nothing but the two blobs and Bismuth's existing signature machinery — no chain replay, no "which fork won." Fault attaches to a **key**, not an IP. *That* is the binding admission, and it exists only post-fork.

---

## 6. Propagation

> **[REVIEW: C1 cascade]** Because pre-fork proofs are forgeable, **propagation is gated entirely behind the hf2 binding fix.** Phase 1 (pre-fork) does **no network gossip of accusations**; it only logs locally. The protocol below activates with hf2.

### 6.1 Socket protocol (`node.py` dispatch, `worker.py` push, `connections.py` send helper)

Two commands are added to the `handle()` dispatch in `node.py` (alongside `mempool`/`blockheight`/`blocknf`), **inert until `node.fork_height` is reached** and `node.accuser_gossip` is set:

- **`accusation`** — receive a proof; run cheap structural pre-checks and `(miner_address, height)` dedup *before* any crypto; if novel and `verify_equivocation` passes, store, apply policy, and re-gossip (flood). If it fails verification, penalize the *relayer* (`PENALTY_INVALID_BLOCK`) — relaying a bogus proof is itself a bannable offense.
- **`accusationsreq`** — peer requests proofs above a given height (anti-entropy on reconnect, mirroring mempool sync).

Body is the JSON/list `EquivocationProof`, sent via the existing length-prefixed `connections.send`/`receive`. Outbound push lives in `worker.py`'s per-peer loop, capped at N proofs per cycle, deduped on `node.accuser`.

### 6.2 REST (`api.py` / `apihandler.py`)

- `POST /api/accusation` — submit a proof (permissionless, like the already-shipped `POST /api/transaction`). Verifies, stores, gossips. **Rate-limited and structurally pre-checked** (see §7.2).
- `GET /api/accusations?from_height=H` — list held proofs for explorers / `api_sync.py` peers to pull, mirroring the headers-first style of `api_sync.sync_segment`.

---

## 7. Penalty — phased

### 7.1 Phase 1 (ships now, NO fork, NO gossip): local advisory only

On observing a same-miner same-height two-distinct-block event (a *heuristic*, not a proof):

1. **Log it.** Emit a structured advisory to the node log and to a local, in-memory advisory store.
2. **Local fork-choice distrust, scoped narrowly.**
   > **[REVIEW: C2] — CRITICAL.** The original Phase 1 added the equivocating *miner_address* to a network-propagated `miner_blacklist` and penalized (`PENALTY_INVALID_BLOCK = 40`, auto-ban at `-50`) any peer "serving a chain whose tip descends from a blacklisted equivocating coinbase." The review showed this is a **network-wide censorship / eclipse weapon**: one forged proof (trivial via C1) against the largest honest pool's address floods the network, every node blacklists that pool, and every node bans every peer relaying the honest chain (which legitimately descends from that pool's coinbases) — a network split from a single permissionless message. **Even with genuine proofs, blacklisting by address and refusing chains that *descend* from a once-equivocating coinbase is wrong**: an honest chain contains blocks from many miners; one miner equivocating once does not invalidate the chain later built on it.
   >
   > **Resulting change:**
   > - **Phase 1 has NO `miner_blacklist` and NO cross-network peer-ban driven by accusations.** It is local, advisory, non-gossiped.
   > - The only fork-choice effect, ever (Phase 1 or 2), is: **do not voluntarily build on the *specific* block whose coinbase is provably double-signed** — scoped to that one block, never the address, never descendant chains.
   > - Peer bans key on **demonstrably invalid blocks a peer actually served**, never on the identity of a miner upstream in history.
3. **No consensus change, no gossip, inert unless `node.accuser` is set.**

### 7.2 Anti-DoS (mandatory on every endpoint)

> **[REVIEW: H2, L2] — HIGH.** `POST /api/accusation` is permissionless and each submission triggers two `verify_tx_signature` calls (RSA verify is expensive) plus a flood re-gossip — an asymmetric CPU-DoS. The original dedup key `(miner_address, height, sorted(hashes))` includes attacker-controlled hash fields, so flipping one byte mints a new dedup key and defeats dedup. **Required, before any crypto runs:**
>
> - **Cheap structural pre-checks first:** height within the live window, both hashes present and different, both signatures present and different, all fields length-bounded, total serialized proof size under the hard cap (§4.2).
> - **Dedup *before* verify, keyed on `(miner_address, height)` ALONE.** Once a node holds *any* proof for that slot it never verifies another for that slot.
> - **Rate-limit `POST /api/accusation` per source IP**; require socket-gossip senders to be already-connected/reputable peers.
> - **Hard caps** on proofs-in-flight and total stored proofs (the LRU bounds storage, but the verify cost precedes insertion, so the in-flight cap is what protects CPU).

### 7.3 Phase 2 (gated on `node.fork_height` / hf2 + the binding fix + staking): on-chain accusation op

Activates only when all three exist: (a) the §5.2 coinbase-signature-binds-block-hash consensus change, (b) the `staking.py` slashable bond, (c) `node.last_block >= node.fork_height`.

- **Carrier:** a normal tx with `operation == "accuser:equivocation"` and `openfield` = the serialized `EquivocationProof`. Permissionless.
- **Validation** (`digest_tx.py` / `digest.py`, gated on `node.fork_height`): re-run `verify_equivocation` (now genuinely binding); confirm the offense height is within the slashing window (above the last checkpoint, within `max_slashing_period`); confirm **not already accused**, deduped by `(miner_address, height)` in an **LMDB side-index** (per the no-SQLite storage directive).
- **Slashing effect** at `apply_rewards()` (`digest.py`): forfeit a protocol-constant fraction of the equivocator's staked bond.
  > **[REVIEW: H3] — HIGH.** The original "redirect part to the accuser, burn the rest" enables (a) **self-denounce farming**: a miner about to be slashed self-accuses via sockpuppet to recover the reward fraction `R` (pure upside against a profitable double-spend), and (b) **denunciation front-running / MEV**: anyone watching the mempool re-submits a pending accusation with higher fee to steal `R`, which also *delays* honest reporting. **Resulting changes:**
  > - **Pay the reward to the *block producer who includes* the proof, not the tx sender** — neutralizes sender front-running (miners capture it; that is acceptable and even desirable for inclusion incentive).
  > - **`R` is a small fixed fraction; the majority of the slash is burned.** Document the launch precondition invariant **`R ≪ equivocation gain`** and require that the equivocation itself already be unprofitable given bond size `B`.
  > - Add a **first-seen-height / commit-reveal rule** to further reduce front-running.
  > - For miners **without a bond the op is inert** — which is the whole reason detection ships first and slashing waits.
- **Single fork gate only.** This is a consensus change (new tx type + balance effect) and rides the one `hf2` fork. Never a second fork signal.

---

## 8. Integration with existing machinery

| Existing | How the accuser plugs in |
|---|---|
| `validation_exceptions.MAINNET_CHECKPOINTS` + `verify_checkpoint` (from `digest.py`) | **Hard finality floor** = the slashing-window boundary. Offenses below the last checkpoint are discarded; reorgs below it already HALT. No change to checkpoints. |
| `essentials.checkpoint_set` / `rollback_allowed` / `rollback_consensus` | **Soft reorg-depth cap**, reused verbatim. The accuser **feeds** this (advisories/proofs inform reputation) but **never overrides** its PoW fork-choice (H1). |
| `chain_ops.rollback()` / `chain_ops.blocknf()` | Detection hook #2: feed displaced-tip coinbase to `EquivocationIndex` via `backup_higher()`. Existing rollback action hooks (peer IP, hash, reason) are where any reputation effect fires. |
| `chain_ops._rebuild_derived_state()` | **Stage 2b fix for the task-#21 corruption class:** fold difficulty re-derivation into the rebuild and add a post-rollback self-heal assertion (recompute `difficulty()` from the truncated ledger, assert it matches persisted). Ships now; not a consensus change; not gated on the accuser flag. |
| `peers_reputation.py` (`penalize`, `reputable_count`, `consensus_reputation_weighted`, `PENALTY_INVALID_BLOCK=40`, `REP_BAN_BELOW=-50`) | Penalty path. Penalize relayers of **bogus proofs** and peers serving **demonstrably invalid blocks** — never miners upstream in history (C2). Whitelist-immunity + per-peer ≥1 vote weight keep accusations from being weaponized to isolate a node. |
| `digest.py` post-validate (`miner_tx` set) | Detection hook #1: every accepted block's coinbase witness → `observe`. |
| `polysign/signerfactory.py` (`verify_tx_signature`) | The verification primitive. Note: signs the six content fields / content txid, **not** the block hash today — the root of C1 (§5). |
| `bismuth_serialize.signature_buffer()` | The signed pre-image. hf2 must extend it (for coinbases) to bind the block hash. |
| `node.py handle()` / `worker.py` / `api.py` / `apihandler.py` / `connections.py` | New `accusation` / `accusationsreq` socket commands + REST endpoints (§6) — **inert until hf2**. |
| `staking.py` (post-hf2) | Phase-2 bond = the thing slashed. Until it exists, the on-chain op is a no-op. |

---

## 9. False-positive and anti-griefing safety (load-bearing)

The system must **never** punish honest behavior, and **never** let one message harm the network.

1. **Two DIFFERENT blocks required.** `observe()` returns `None` when `prior.block_hash == witness.block_hash`. A re-broadcast or independently re-mined identical block is not an offense.
2. **Two DISTINCT signatures required (post-fork).** *[REVIEW: C1/M2]* After the binding fix, a single lifted signature can no longer fabricate a second block, and honest pool re-templating (same coinbase signature, different block) no longer trips the trigger. Pre-fork, the same-coinbase-different-hash event is *advisory only*.
3. **Honest reorgs / orphans are never an offense.** The index keys on `(miner_address, height)`; two *different* miners at height H is normal forking and yields nothing.
4. **No fault attribution for deep reorgs.** The over-deep/checkpoint-violating detector emits only a `ReorgAlert` (reputation + the already-existing fork-choice), never an `EquivocationProof`. An honest partition heal costs proposing peers reputation but is never slashed.
5. **No network-wide blacklist from one message.** *[REVIEW: C2]* No `miner_blacklist` drives cross-node fork-choice or peer-banning. The only effect is "don't build on the one provably double-signed block," and peer bans require a demonstrably invalid *block the peer served*.
6. **No fork-choice override.** *[REVIEW: H1]* The accuser never refuses a higher-work chain based on accusation state; that lever was removed because it is invertible into forcing a malicious rollback.
7. **Bogus proofs can't be weaponized.** A fake proof fails `verify_equivocation`; the *relayer* is penalized, not the accused. Whitelist-immunity + per-peer ≥1 vote weight mean an accusation flood cannot isolate a node.
8. **Partition rejoin is protected.** *[REVIEW: M3]* See §10 — `min_reputable` is **capped at a small constant**, preserving the existing `rollback_allowed` auto-recover semantics so an honestly-partitioned node can still roll back to the true chain.
9. **No new DoS.** *[REVIEW: H2/L2/L3]* Structural pre-checks + `(miner, height)` dedup before crypto + rate limits + size caps.
10. **Deterministic, persisted proof corpus.** *[REVIEW: M4]* Only PoW-valid, independently-verifiable, gossiped proofs (persisted in LMDB) drive any effect — never locally-observed shadow forks. The corpus survives restart, so anti-entropy does not re-incur full verify cost on reboot.

---

## 10. Reorg-depth hardening (bounded, partition-safe)

> **[REVIEW: M3] — MEDIUM.** The original §4.4 proposed `min_reputable = max(1, depth // 30)` so deeper reorgs need more proven peers. The review showed this **regresses the deliberate auto-recover semantics of `essentials.rollback_allowed`**: a freshly-rejoined node hasn't yet earned reputation from the new majority's blocks, so after a long partition it may need a deep reorg but cannot reach `depth // 30` *reputable* peers — locking it out of the true chain. Combined with the (now-removed) equivocator-fork refusal, this was the exact "punish honest nodes during a partition" harm to avoid.
>
> **Resulting change:** **cap `min_reputable` at a small constant** (e.g. `3`, matching `min_peers`); do **not** scale it unboundedly with depth. Keep the existing `rollback_allowed` auto-recover behavior — the accuser must never make `rollback_allowed` stricter than it is today for honest deep reorgs. The retained hardening is benign: require backing peers to actually serve PoW-valid alternate headers before committing (which `blocknf` already enforces via `db_block_hash == block_hash_delete`), not roll back on a claimed height alone.

---

## 11. Rollout plan (inert-by-default, regnet-testable)

Two config flags gate the subsystem, both default `False` (inert on mainnet):

- **`node.accuser`** — enables local detection + advisory logging.
- **`node.accuser_gossip`** — enables network propagation; additionally **no-ops until `node.fork_height`** because pre-fork proofs are forgeable.

**Stage 0 — `accuser.py` skeleton, inert.** Land `EquivocationIndex`, `CoinbaseWitness`, `EquivocationProof`, `verify_equivocation` (with structural pre-checks, size caps, distinct-signature requirement, fixed L1 height check). Wire detection hooks #1–3 in `digest.py` / `chain_ops.py` behind `node.accuser`. **Pure local advisory logging; no gossip, no policy, no blacklist.** Add this doc. Zero mainnet behavior change.

**Stage 1 — local fork-choice distrust (scoped).** Enable the narrow "don't voluntarily build on the one provably double-signed block" behavior and bogus-relayer penalty *for locally observed events only*. Still no gossip, no `miner_blacklist`.

**Stage 2a (parallel, independent of the accuser flag) — rollback difficulty self-heal.** Fold difficulty into `_rebuild_derived_state` + post-rollback assertion. Closes the task-#21 corruption class. **This is the most directly motivated piece (§1.1) and ships first/independently.**

**Stage 3 — hf2 binding fix.** Land the coinbase-signature-binds-block-hash consensus change (§5.2) inside the single `hf2` fork, alongside the existing hf2 txid/serialization work. *Only after this is the proof non-forgeable.*

**Stage 4 — gossip + REST, gated on hf2.** Add `accusation` / `accusationsreq` socket commands and the two REST endpoints behind `node.accuser_gossip` AND `node.fork_height`. Nodes share and independently verify genuine proofs. Anti-DoS (§7.2) is mandatory here.

**Stage 5 — Phase-2 on-chain op.** Land the `accuser:equivocation` tx type, LMDB dedup side-index, slashing at `apply_rewards()`, block-producer-paid reward with `R ≪ B` invariant and first-seen-height rule. Inert until `staking.py` bond exists. Single fork gate.

### Regnet testability

Regnet has no `MAINNET_CHECKPOINTS` (`_checkpoints` returns `{}` for regnet), so window logic is exercised via the `node.checkpoints` override.

- **Heuristic injection (Stage 0/1):** mine two blocks at the same height with the *same* regnet miner key but different transaction sets → distinct hashes, same address. Assert `observe()` emits a *local advisory* and, pre-binding-fix, returns **no proof**.
- **Proof injection (Stage 3+):** with the binding fix active, produce two genuinely distinct-signature blocks at one height → `observe()` returns a proof; `verify_equivocation` passes.
- **Forgery / framing test (the C1 regression test):** lift a victim miner's coinbase signature onto a second same-height block built by the attacker. **Pre-fix:** assert the old trigger would have fired (documenting the vulnerability). **Post-fix:** assert `verify_equivocation` returns `False` (signatures bind distinct blocks, the lifted one fails). This test must exist and pass before gossip is enabled.
- **Negative tests:** (a) re-mine identical block → no proof; (b) two *different* miners at one height → no proof; (c) honest pool re-template (same coinbase sig, different block) → no proof post-fix; (d) tampered signature → `verify_equivocation` False → relayer penalized.
- **Anti-DoS tests:** flood `POST /api/accusation` with structurally-bad and crypto-bad proofs; assert rate-limit kicks in, dedup-on-`(miner,height)` prevents repeat verifies, and CPU stays bounded.
- **Gossip test (Stage 4):** 2-node regnet harness — inject a genuine proof on node A; assert node B independently verifies and re-gossips; assert A bans the source of a bogus injection. Observe the live-regnet gotchas: poll for inclusion / `mpclear` under mempool contention; never full-scan the prod ledger; keep test procs off the prod node's I/O.
- **Rollback self-heal test (Stage 2a):** roll back N blocks, assert recomputed `difficulty()` matches persisted, assert the post-rollback assertion does not fire on honest rollbacks and *does* fire on injected derived-state corruption.

---

## 12. Open questions

1. **Exact coinbase binding form (Stage 3).** Sign the full `block_hash`, or sign `(previous_hash, merkle_root_of_txs)`? The latter is cheaper to include in the proof pre-image and composes with the hf2 serialization rework — but it must be exactly what `block_hash_at` commits to, to avoid a second collision surface.
2. **RSA-forever for coinbases?** (M1) Will coinbase remain RSA-only post-hf2, or migrate to secp256k1 with the rest of hf2? This decides whether the secp256k1 branch in `verify_equivocation` is live or dead code for accusations, and whether RSA canonical-encoding pinning is a permanent requirement.
3. **Slashing economics (H3).** What is the bond size `B`, the reward fraction `R`, and the burn fraction, such that `R ≪ equivocation gain` holds for the worst-case double-spend? This is a launch precondition for Phase 2 and depends on `staking.py` parameters that do not yet exist.
4. **Accusation tx fee / spam economics on-chain (Stage 5).** Should the `accuser:equivocation` op be fee-exempt (to not penalize honest reporters) while still bounded against spam, given that an invalid one is cheaply rejected at validation?
5. **Window length vs. checkpoint cadence.** The slashing window is bounded below by the last checkpoint. Is the checkpoint cadence frequent enough that the window is long enough to catch real offenses, but short enough to bound proof-corpus memory?
6. **Cross-restart corpus authority.** Persisted proofs (LMDB) are deterministic given gossip, but who is authoritative on first sync — should a newly-synced node trust a peer's `GET /api/accusations` corpus, or independently re-verify all of it (re-incurring verify cost, mitigated by §7.2 dedup)?
7. **Does the binding fix interact with the txid-nado change?** hf2 already makes the signature sign the content `txid`. Confirm that adding the block-hash commitment to the coinbase pre-image does not double-commit or conflict with the nado txid model — ideally the txid model already transitively binds enough block context that the coinbase fix is a small delta.
