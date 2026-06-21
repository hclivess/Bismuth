# 12 — Tooling, build & tests

## Installation (node + systemd service)

`install_node.sh` is the one-shot installer — system packages, Python deps, and the `bismuth-node`
systemd service pointing at the checkout it lives in:

```bash
sudo ./install_node.sh                 # deps + service, starts it if not already running
sudo ./install_node.sh --no-start      # deps + service, don't start
sudo ./install_node.sh --restart       # also restart if already running
sudo ./install_node.sh --deps-only     # just dependencies
sudo ./install_node.sh --regnet        # service runs a regnet node for testing
```

It is **idempotent** (re-runnable; never bumps an already-working node's deps and won't restart a
running node unless `--restart`), installs the LMDB stack (`lmdb`/`msgpack` — needed by block_store,
vm_state, and the shielded store), and auto-applies the **ed25519 Python-3.12 source patch** (its
bundled `versioneer` uses APIs removed in 3.12). The unit it writes matches the production service:
graceful `SIGTERM` stop (`TimeoutStopSec=180`) so `ledger.db`/`hyper.db` stay consistent across reboots,
and `Environment=BISMUTH_IGNORE_CONFIG_CUSTOM=1` so a stray regnet `config_custom.txt` can't hijack a
mainnet boot.

## Test suite

The suite under `tests/` was rebuilt to be **dependency-light**: it drives a regnet node over the
in-tree wire protocol (`connections`) and signs with the in-tree `polysign` (via `essentials`), so it
needs **no** external `bismuthclient` / `bismuthcore` / `tornado` / `ed25519` (those legacy client
libraries don't run on Python 3.12).

Run it from the repo root:

```bash
pip install -r requirements-node.txt -r tests/requirements.txt   # add --break-system-packages on PEP-668 systems
python3 -m pytest -v
```

Layout:

| File | Coverage |
|---|---|
| `tests/conftest.py` | session fixtures: launch a fresh regnet node subprocess, **poll** for readiness (no blind sleep), and hand tests a connected `LiteClient` |
| `tests/_lite_client.py` | `LiteClient` — `command()`, `mine()`, `send()` (builds + signs + submits a tx), `balance()`, `latest_transactions()` |
| `tests/test_crypto.py` | addresses, RSA sign/verify, tamper rejection, `wallet.der` round-trip, simplecrypt, fee formula, quantizer, lazy ECDSA |
| `tests/test_transactions.py` | amount/recipient, txid, operation/openfield, fee, op-length truncation, sender/recipient balances, overspend rejection |
| `tests/test_ledger.py` | raw vs JSON parity (blocklast/balance/addlistlim), `api_getblockfromhash`, block-hash reconstruction |
| `tests/test_mempool.py` | pending→confirmed lifecycle, mpget/mpgetjson parity, duplicate rejection, bad-signature rejection |
| `tests/test_node.py` | port, difficulty parity, keygen parity, `api_getconfig`, `api_getaddresssince`, `api_getblocksince`, address validation, pubkey→address |
| `tests/test_api.py` | `api_ping`, `api_getbalance`, `api_getaddressinfo`, `api_gettransaction`, `api_getblockfromheight`, tokens/alias no-crash |
| `tests/test_miner.py` | the built-in solo miner (`miner.py`) over regnet via `regtest_mine`/`regtest_powcheck`: embeds mempool txs, stamps the hf2 coinbase signal, mines real Heavy3, exercises the **dual-algo PoW** (sha224 vs blake2b anneal) ([21](21-mining.md)) |
| `tests/test_pq_signer.py` | the **post-quantum** signer `polysign/signer_mldsa` (ML-DSA-65 / CRYSTALS-Dilithium3): sign/verify round-trips, deterministic-from-seed keys, hash-of-pubkey addresses ([20](20-post-quantum.md)) |
| `tests/test_hf2_recoverable.py` | unit + adversarial coverage of the post-fork **recoverable-signature** path (ordinary single-sig secp256k1 only): signs the 32-byte content txid, 65-byte recoverable hex sig, `public_key` dropped, signer recovered via `ecrecover`, low-s enforced; verifies RSA/ED25519/native-multisig/shielded keep their legacy signing ([18](18-hardfork-hf2.md)) |
| `tests/test_hf2_fork_transition.py` | regnet fork-transition over the wire: pre-fork **reject** of recoverable txs, post-fork **accept**, **reject-legacy** (post-fork single-sig must use the new path), and **content-txid lookup** (a 64-char lowercase-hex query resolves the on-read content hash; everything else hits the legacy signature-prefix match) ([18](18-hardfork-hf2.md)) |
| `tests/test_tokens_aliases_plugin.py` | token + alias lifecycle through the `tokens_aliases` plugin: token issue/transfer, `alias:register`/`alias:transfer`/`alias:free`, and **reorg rollback** of the plugin's side-index ([27](27-tokens-aliases-plugin.md)) |
| `tests/regnet_smoke.py` | a standalone (non-pytest) gate — see below |
| `tests/fork_transition_smoke.py` | standalone (non-pytest) **hf2 transition gate**: lock-in → restart mid-transition (sidecar replay) → real-miner sha224→blake2b boundary crossing with a pre-fork tx confirming post-fork → reorg back across the boundary → sidecar-less restart (self-healing re-derivation). Uses the `BISMUTH_REGNET_KEEP=1` test-only env escape so a regnet chain survives a restart ([18](18-hardfork-hf2.md)) |
| `tests/config_custom.txt` | regnet test config (`regnet=True`, `heavy=False`, `port=3030`) |

Beyond the above, the suite carries a broader consensus / VM / fork / storage set — e.g. `test_riscv`,
`test_vm_state` / `test_vm_value` / `test_vm_post_fork` (the RISC-V VM, [19](19-vm.md)), `test_fee_dynamics`,
`test_difficulty_lwma`, `test_fork_wiring`, `test_consensus_invariants`, `test_replay`,
`test_characterization`, `test_integer_storage`, and `test_rollback_reorg` / `test_rollback_autorecover`.

`tests/regnet_smoke.py` is a fast, standalone check (no pytest): start `python3 node.py regnet2`, then
`python3 tests/regnet_smoke.py`. It exercises chain advance, rewards, the `tokensget` fix, an RSA
sign+verify round trip, and pins the fee formula — handy as a quick gate or in constrained
environments.

CI (`.travis.yml`) runs on Python 3.12, installs `requirements-node.txt` + `tests/requirements.txt`,
copies the regnet config, and runs `python -m pytest -v`.

## Chain snapshot / maintenance tooling (`static/`)

| Script | Purpose |
|---|---|
| `tar.py` / `tar_testnet.py` | validate the ledger (balance diff ledger↔hyper, duplicate rows/signatures) and, if the node isn't running, VACUUM and pack `ledger.tar.gz` bootstrap snapshots |
| `untar.py` | path-traversal-safe extraction of a bootstrap snapshot |
| `migrate.py` | one-off: rebuild the `transactions` table with indexes + VACUUM |
| `vacuum.sh` | `VACUUM` index/hyper/ledger DBs |
| `backup.py` | a developer-personal periodic ledger copy (hardcoded path) |

## Install / build

- `auto-install/bis-node-alone-install.sh` — unattended Ubuntu node setup (swap, OS limits, deps,
  source download + bootstrap, hypernode plugin). Note: firewall config is commented out and the node
  is expected to start post-reboot via an external mechanism.
- `compile_nuitka.cmd` + `setup.iss` — Windows: compile `node`/`node_stop`/`commands` with Nuitka and
  build an Inno Setup installer.
