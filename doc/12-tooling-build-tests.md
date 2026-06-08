# 12 — Tooling, build & tests

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
| `tests/test_miner.py` | the built-in solo miner (`miner.py`) over regnet via `regtest_mine`/`regtest_powcheck`: embeds mempool txs, stamps the hf2/pow2 coinbase signal, mines real Heavy3, exercises the **dual-algo PoW** (sha224 vs blake2b anneal) ([21](21-mining.md)) |
| `tests/test_pq_signer.py` | the **post-quantum** signer `polysign/signer_mldsa` (ML-DSA-65 / CRYSTALS-Dilithium3): sign/verify round-trips, deterministic-from-seed keys, hash-of-pubkey addresses ([20](20-post-quantum.md)) |
| `tests/regnet_smoke.py` | a standalone (non-pytest) gate — see below |
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
