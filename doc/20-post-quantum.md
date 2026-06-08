# 20 — Post-quantum cryptography: a pivot held in reserve

> Companion to [`09-crypto-wallets-keys.md`](09-crypto-wallets-keys.md) (the live crypto/`polysign`
> map) and [`18-hardfork-hf2.md`](18-hardfork-hf2.md) (the signalled auto-activation fork machinery
> this would ride on). This is a **design** doc, not a work item. Nothing here is active code. The
> deliverable for *now* is the architecture analysis below and keeping `polysign` clean — so that the
> day a quantum timeline becomes credible, the pivot is "register a new signer," not a rearchitecture.

## 1. The threat

Every signature scheme Bismuth uses today is broken by a sufficiently large quantum computer.

- **RSA** (the mainnet default — `signer_rsa.py`, RSA-4096) and **ECDSA** (`signer_ecdsa.py`,
  secp256k1) both rest on problems — integer factorization and the elliptic-curve discrete log — that
  **Shor's algorithm** solves in polynomial time. **Ed25519** (`signer_ed25519.py`) is a discrete-log
  scheme on a different curve; same fate, same algorithm. So **all three** of Bismuth's signers fall
  to the *same* attack. There is no "safe one" in the current `SignerFactory`.

  (To be precise about what breaks: Shor recovers a **private key from a published public key**. Hash
  functions are not affected this way — Grover's algorithm only halves their effective security, which
  a wide hash absorbs. Bismuth's PoW — Heavy3, `sha224`-based — and its addresses-as-hashes are *not*
  the soft target; the **signatures** are.)

- Concretely: anyone who has ever broadcast a transaction has **published their public key** on-chain
  (it rides in the tx — see `bismuth_serialize.py`, the `public_key` field; `digest.py` reads
  `db_public_key_b64encoded`). A quantum adversary recovers the private key from that public key and
  spends the funds. An address that has only ever *received* is hashed-only (see §2) and marginally
  better off — but the moment it spends, its key is exposed, and in practice a chain has to assume
  keys are public.

- **Nobody knows the timeline.** Estimates for a cryptographically-relevant quantum computer span from
  "a decade-plus" to "sooner than the optimists think," and the honest engineering position is that
  the date is *unknown*. That has a direct consequence for this document: **the goal is not to pivot
  now.** Pivoting now would impose large, permanent costs (see §3) against a threat that may be far
  off, and would bet on a specific PQ scheme before the field has fully settled. The goal is to **hold
  the option** — to be architecturally ready to pivot on relatively short notice when things get
  messy — and to verify *today*, cheaply, that the option is real. The rest of this doc is that
  verification.

## 2. Why Bismuth is unusually well-positioned

The expensive part of a PQ migration, for most chains, is that the signature algorithm is **welded
into the protocol** — one hard-coded curve, one hard-coded address format derived from it. Swapping it
is open-heart surgery. Bismuth already did that surgery, years ago, for unrelated reasons. Two
properties matter:

### a. A pluggable, multi-algorithm signature layer already exists

`polysign` is a real abstraction, not a wrapper around one library. `signer.py` defines an abstract
`Signer` (with `from_seed` / `sign_buffer_for_bis` / `verify_bis_signature` / `public_key_to_address`)
and a `SignerType` enum; `signerfactory.py` dispatches to a concrete signer **by type and by address
shape**, and already ships **four** distinct algorithm families behind that one interface:

```python
# polysign/signer.py
class SignerType(Enum):
    NONE = 0
    RSA = 1          # signer_rsa.py     — mainnet default, pycryptodomex
    ECDSA = 2        # signer_ecdsa.py   — secp256k1, coincurve
    ED25519 = 3      # signer_ed25519.py — pynacl/ed25519
    BTC = 1000       # test schemes
    CRW = 1001
```

The non-RSA signers are **lazy-loaded** (`_load_optional_signer` in `signerfactory.py`), so adding a
signer with a heavy native dependency does **not** burden an RSA-only mainnet node until that signer is
actually used. Verification is fully indirected: `digest.py` does not know what RSA is — it calls
`SignerFactory.verify_bis_signature(...)`, which resolves the signer from the address and delegates.
**A new algorithm plugs into exactly that seam.**

### b. Addresses are hashes of the public key — algorithm-agnostic by construction

A Bismuth address does not encode "which curve." It is a **hash (or hash-derived encoding) of the
public key**:

- **RSA:** `sha224(public_key_pem).hexdigest()` → 56-hex (`signer_rsa.py::public_key_to_address`).
- **ECDSA:** `base58( version ‖ ripemd160(sha256(pubkey)) ‖ checksum )` → a `Bis1…` string
  (`signer_ecdsa.py`), each family carrying its own `_address_versions` prefix.
- **Ed25519:** `base58( version ‖ pubkey ‖ checksum )`, a longer `Bis1…` string (`signer_ed25519.py`).

The address is a **commitment to a public key**, and the *kind* of key is recovered from the address
**shape / version prefix** (`SignerFactory.address_to_signer`), not baked into the consensus rules. So
a new signature type can reuse the **same address-as-hash scheme** with its own version prefix, and the
ledger, the balance index, the mempool, and every "send to address X" code path stay unchanged — they
already treat the address as an opaque hash string. The hash output is also **fixed-size regardless of
how large the underlying public key is**, which (see §3) is exactly the property a PQ key needs.

**The upshot:** for Bismuth, adding a post-quantum algorithm is *register a new signer in the
factory + a serialization tag + a fork gate* (§4) — not *rearchitect the protocol*. The two hardest
prerequisites (a) and (b) are **already shipped and in production.** That is the whole reason this doc
can be short.

## 3. The candidate — and its honest costs

The pivot must not pretend PQ signatures are free. They are not. The point of naming a concrete
candidate is to put the **real numbers** on the table.

### Primary candidate: ML-DSA-65 (FIPS 204 / CRYSTALS-Dilithium, NIST Category 3)

ML-DSA is the NIST-standardized (FIPS 204, 2024) lattice signature scheme, derived from
CRYSTALS-Dilithium. **ML-DSA-65** is the **Category 3** parameter set (≈ AES-192-equivalent classical
strength), the sensible middle for a value-bearing chain. Real sizes:

| Scheme              | Signature      | Public key     | Notes                              |
|---------------------|---------------:|---------------:|------------------------------------|
| Ed25519 (today)     | **64 B**       | **32 B**       | discrete-log; quantum-broken       |
| ECDSA secp256k1     | ~64–72 B       | 33 B (compr.)  | discrete-log; quantum-broken       |
| RSA-4096 (today)    | ~512 B         | ~550 B (DER)   | factoring; quantum-broken          |
| **ML-DSA-65**       | **~3 309 B**   | **~1 952 B**   | lattice; quantum-resistant         |
| SLH-DSA (SPHINCS+)  | ~7–50 KB       | ~32–64 B       | hash-only; see below               |

So a single ML-DSA-65 spend carries roughly **3.3 KB of signature + 1.95 KB of public key ≈ 5 KB** of
crypto material, versus the **~64–256 bytes** an Ed25519/ECDSA spend carries today — call it a **20–50×
blow-up of the per-tx authentication payload**, which is a large fraction of a Bismuth tx body. The
honest consequences:

- **Bigger transactions → bigger blocks → more bandwidth.** A block of PQ-signed txs is many times
  larger on the wire. On a small network (~15 nodes) propagation latency and per-peer bandwidth matter
  for orphan rates; this is a real operational cost, not a rounding error.
- **More storage.** The ledger grows much faster per tx. This compounds with — and strengthens the case
  for — the storage rework already planned (LMDB block store, **public-key dedup**, integer units;
  see [`16-database-rework-plan.md`](16-database-rework-plan.md)). Pubkey-dedup is *especially*
  valuable here: a ~2 KB PQ pubkey should be stored once and referenced, exactly the
  "public key by reference — 1:1 with the address" idea already in the `hf2` plan
  ([18 §A](18-hardfork-hf2.md)).
- **Heavier verification.** ML-DSA verification is more CPU-intensive than an Ed25519 check (lattice
  arithmetic, NTTs). At Bismuth's throughput this is unlikely to be a bottleneck for a *single* node,
  but it raises the cost of initial block download / full replay (`replay_verify.py` re-checks the
  whole chain), and it is the cost the aggregation research in §3.c targets.

These costs are **why the pivot is held in reserve and not done now** (§1). They are acceptable as
insurance against a broken chain; they are not worth paying years early.

### Conservative alternative: hash-based signatures (SLH-DSA / SPHINCS+, FIPS 205)

If, when the time comes, lattice assumptions look shakier than hoped, the conservative fallback is a
**hash-based** scheme — **SLH-DSA** (FIPS 205, standardized from SPHINCS+). Its security rests **only
on the security of a hash function** — no number-theoretic *or* lattice assumption — which is the
strongest assurance available and ages extremely well. The price is **even larger signatures** (single-
digit to tens of KB, depending on parameters), and stateless variants are slower to sign. Public keys,
notably, are **tiny** (32–64 B). For a chain that already commits to addresses via hashes, a hash-only
signature scheme is philosophically the most consistent endpoint; the size cost is the reason it is the
fallback rather than the default. (Stateful hash-based schemes — LMS/XMSS — are smaller but impose
one-time-key state management that is a poor fit for ordinary wallet UX and are not considered here.)

`polysign` makes this an *option*, not a fork in the road: both could be registered as distinct
`SignerType`s and coexist. We do not have to pick the final scheme today — only keep the seam clean.

### This is the same problem Ethereum is working — and the same shape of solution

Bismuth is small, but the design space here is being explored in the open by a much larger ecosystem,
and the conclusions line up with what `polysign` already is:

- **Native account abstraction so accounts can use *any* signature algorithm.** Ethereum's
  **EIP-8141** (native AA) makes the *account itself* able to specify how its transactions are
  authenticated, rather than hard-wiring one curve into the protocol. That is precisely the property
  `polysign` + addresses-as-hashes already give Bismuth: the *account* (its address shape) determines
  the verifier. Bismuth arrived at "the account picks the algorithm" by a different road, but it is the
  same destination, and it is the destination that makes a PQ pivot tractable.
- **Cutting the verification cost with vectorized-math precompiles.** Lattice verification is
  dominated by modular vector/NTT arithmetic; dedicated **vectorized-math precompiles** make it cheap.
  Bismuth's analogue is its post-fork **RISC-V VM** ([`19-vm.md`](19-vm.md)) — *if* PQ verification ever
  needed to be exposed to contract-level logic, a precompile-style fast path is the natural mechanism;
  for L1 tx verification it is plain native code in the signer.
- **Cutting the bandwidth/verification cost with recursive proof/signature aggregation.** The ~5 KB-per-
  tx blow-up is the real pain, and the live research answer is **recursive-proof aggregation** — prove
  "all N signatures in this block are valid" in one succinct proof, so nodes verify *one* object instead
  of N, and a **STARK-based, bandwidth-efficient mempool** that propagates aggregated proofs rather than
  N full PQ signatures. That work is directly applicable if/when Bismuth's PQ blocks get heavy. We do
  not need it to *ship* a PQ signer — a plain "verify each PQ signature" path is correct and simple —
  but it is the known path to making PQ blocks cheap, and worth tracking.

See §6 for the primary sources.

## 4. The pivot mechanism

When the timeline tightens, the change is small and rides entirely on machinery Bismuth **already has**.
Four pieces:

1. **A new `polysign` signer — `signer_mldsa.py` (✅ BUILT + TESTED).** A `Signer` subclass implementing
   the existing abstract interface (`from_seed`/`from_private_key`, `sign_buffer_raw`/`_for_bis`,
   `verify_signature`/`verify_bis_signature[_raw]`, `public_key_to_address`) over `dilithium-py`'s
   ML-DSA-65, registered in `signerfactory.py` as `SignerType.MLDSA = 4` in the lazy `_OPTIONAL_SIGNERS`
   table (RSA-only nodes pull no PQ dependency until used). The wallet key is a **32-byte seed** (FIPS 204
   deterministic KeyGen); the **address is a hash of the pubkey** (the 1952-byte pubkey is too large to
   embed). It round-trips for real — `tests/test_pq_signer.py` (6 tests): sign/verify, deterministic keys,
   hash-addresses, tamper + wrong-address rejection, the Bismuth b64 network format. This is the only
   genuinely new cryptographic code, and it is **real and tested**; everything it plugs into already
   exists. What stays gated is consensus ACCEPTANCE (below), not the signer.

2. **A new signature-type tag in the tx / serialization.** The verifier is currently selected from the
   **address shape** (`address_to_signer`); a PQ family needs its own recognizable form — its own
   `_address_versions` prefix in `signer_mldsa.py`, yielding a distinct `Bis…`/prefixed address class
   that `address_to_signer` and `address_is_valid` learn to route. (The `hf2` serialization rework
   [18 §A] is already moving sig/pubkey to canonical raw-bytes encoding with **pubkey-by-reference**;
   a PQ tag fits cleanly into that post-fork encoding, and pubkey-by-reference is what keeps the ~2 KB
   PQ key from being repeated on every tx.)

3. **Addresses still = hash(pubkey), so old and new coexist.** A PQ address is the **same kind of
   object** as today's addresses — a hash-derived commitment to a public key, just over a PQ key, with
   its own version prefix (§2b). Nothing in the ledger/balance/mempool layer changes; RSA, ECDSA,
   Ed25519 and ML-DSA addresses live side by side in the **one** chain, each spending only with the
   algorithm its address commits to. There is **no flag day** for receiving — only the spender's own
   signer changes.

4. **Activation via the *same* signalled, gated hard fork as `hf2`.** A PQ signature type is a
   consensus rule change (a node that doesn't understand `SignerType.MLDSA` would reject a valid PQ tx),
   so it must be a coordinated fork — and Bismuth already has the **exact** vehicle, built and tested
   ([18 §"Activation"](18-hardfork-hf2.md)):
   - Upgraded miners stamp a **`pq` signal** into their coinbase openfield (a new marker alongside
     `fork.FORK2_SIGNAL`'s `"hf2"`), the same free-form-data mechanism, **no rule change to start
     signalling**.
   - `fork.py`'s `dynamic_fork_height` counts the signal off the **same chain**, so the PQ activation
     height is computed **identically on every node — deterministic, no off-chain survey, no split
     risk** — locking in once a full window signals and burying past the reorg margin
     (`FORK2_WINDOW` / `FORK2_BURY`).
   - The live `block_height >= fork_height` gate in `digest.py` flips the rule: **below** the PQ height,
     PQ txs are not accepted (unknown type); **at/above**, `SignerFactory` accepts the new type.
   - `/api/fork` (`rest_api.py` → `fork.fork_status`) gives the same readiness view, so the network can
     watch PQ adoption climb before lock-in.
   Whether the fork makes PQ merely **accepted** (coexist with legacy indefinitely) or eventually
   **required** (legacy signatures refused past some later height — a hard cutover once quantum is a
   credible near-term threat) is a **policy choice for activation time**, expressible as a second gate.
   The mechanism supports both; the decision is deferred.

**Migration for existing holders.** Because addresses are per-algorithm and coexist, migration is an
ordinary on-chain action: a holder generates a PQ address (`SignerFactory.from_seed(...,
SignerType.MLDSA)`) and **moves funds to it** with a normal transaction, signed with their *current*
(pre-quantum) key. The critical point is **timing**: this must happen **before** quantum is a real
threat, because the migration tx itself is authorized by the old, quantum-vulnerable key — once that
key is breakable, the window is closed. This is the single most important piece of user-facing
guidance, and it is exactly *why* the option must be **ready early** even though it is **activated
late**: the network needs PQ addresses to exist and be spendable *before* the threat lands, so people
have somewhere safe to move to while their old keys are still safe to move *with*.

## 5. Staging — what to build, and when

The discipline is the same as the rest of the modernization: **incremental, reversible, opt-in,
nothing live until it must be** ([`17-roadmap.md`](17-roadmap.md) principles). Explicitly, this is an
**option held in reserve** — architecturally ready, **not active code.**

**Now (this doc — no code, zero consensus surface):**
- Keep `polysign` clean and genuinely pluggable. Treat "could a new `Signer` be added without touching
  `digest`/`mempool`/ledger?" as an **invariant to protect** when refactoring (see [14](14-known-issues-and-improvements.md)).
  Today the answer is yes; keep it yes.
- **Document the extension point — this file** — so the design is captured while the relevant context is
  fresh, and the eventual implementer starts from a map, not a blank page.
- Track the upstream work (§6): ML-DSA/SLH-DSA library maturity, and the Ethereum AA / aggregation
  research that would cut the size/verification cost.

**When the timeline tightens (a credible quantum estimate, or upstream chains begin moving):**
1. **Implement `signer_mldsa.py` behind a flag.** New file, new `SignerType`, lazy-loaded; standalone
   unit tests (keygen / sign / verify / address round-trip) mirroring the existing per-signer tests.
   Inert: nothing in consensus references it yet.
2. **Add the serialization tag / address prefix**, slotted into the post-`hf2` canonical encoding (§4.2).
3. **Regnet-test end-to-end.** Generate PQ wallets, send PQ-signed txs on regnet, mine and **replay** —
   confirm PQ blocks round-trip and re-verify, and that the size/bandwidth impact (§3) matches the
   estimates on a real (if tiny) network. Validate the `pq` coinbase signal drives `dynamic_fork_height`
   exactly as `hf2` does (extend `tests/test_dynamic_fork.py`).
4. **Schedule the signalled fork** when confidence is there — the off-chain ~15-node survey as the
   confidence gate, the chain's signal count as the actual decision ([18](18-hardfork-hf2.md)). Decide
   *at that point* whether PQ is "accepted" or eventually "required."

Until step 1 is deliberately triggered, **none of this is in the codebase** beyond this document. That
is the intended state: the cost of readiness is one design doc and one architectural invariant, and the
payoff is the ability to move fast precisely when there is no time to spare.

## 6. References

- **Ethereum post-quantum portal** — overview of the PQ migration problem and approaches:
  <https://pq.ethereum.org/>
- **Vitalik Buterin** on native account abstraction + quantum-resistant signatures + vectorized-math
  precompiles + recursive aggregation (the §3.c program):
  <https://x.com/VitalikButerin/status/2027075026378543132>
- **Recursive-STARK-based, bandwidth-efficient mempool** — aggregating PQ signatures so nodes verify one
  proof instead of N, and propagate the aggregate rather than N full signatures (ethresear.ch):
  <https://ethresear.ch/t/recursive-stark-based-bandwidth-efficient-mempool/23838>
- **EIP-8141 — native account abstraction** (accounts may use any signature algorithm; the property
  `polysign` already gives Bismuth): <https://eips.ethereum.org/EIPS/eip-8141>
- **FIPS 204 — ML-DSA** (Module-Lattice Digital Signature Algorithm; CRYSTALS-Dilithium): NIST, 2024.
- **FIPS 205 — SLH-DSA** (Stateless Hash-Based Digital Signature Algorithm; SPHINCS+): NIST, 2024.

---

> **Status: signer BUILT + tested; consensus acceptance gated.** The codebase assets it relies on —
> `polysign`'s `SignerFactory` (§2a), addresses-as-hashes (§2b), and the `hf2` signalled-fork machinery
> (§4.4) — **exist and are in production.** The post-quantum signer **`signer_mldsa.py` is real, working,
> tested code** (`SignerType.MLDSA`, registered in the factory, `tests/test_pq_signer.py` 6/6 green) — it
> signs and verifies real ML-DSA-65. What remains **gated/not-active** is consensus ACCEPTANCE: no mainnet
> path mints or validates ML-DSA txs until a `pq` signalled fork (§4.4 / §5 step 1) is deliberately
> triggered — exactly how the VM and the dual-algo PoW are real-but-inert until their forks.
