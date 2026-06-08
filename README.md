# Bismuth

Bismuth is a **proof-of-work cryptocurrency and smart-contract platform written in Python**. A node
validates blocks, maintains the ledger, talks to peers, mines, and serves wallets and explorers.

> **Status — modernization fork.** This tree is an in-progress modernization of the Bismuth node. The
> guiding rule is that **consensus does not change**: the same blocks must produce the same hashes and
> validate identically. Every change is replay-verified (the chain is re-hashed end-to-end through a
> frozen serialization boundary) and gated by the test suite, so storage, networking and APIs can be
> modernized safely behind a fixed consensus layer. See the [Roadmap](#roadmap) and
> [`doc/16`](doc/16-database-rework-plan.md).
>
> For production, use a tagged release — `main` may be ahead of what's network-validated.

---

## Quickstart

Requires **Python 3.8+** (developed/tested on 3.12).

```bash
# 1. install dependencies
pip3 install -r requirements-node.txt

# 2. run a node (mainnet)
python3 node.py

# 3. stop it cleanly
python3 node_stop.py
```

### Run as a service (systemd) — recommended

Don't babysit a `screen`/`nohup`. Install the node as a systemd service — it survives reboots, restarts
on failure, and stops **gracefully** (systemd sends `SIGTERM`, the node finishes its in-flight block and
drains `db_lock`, so `ledger.db`/`hyper.db` stay consistent):

```bash
sudo bash scripts/install-node-service.sh
```

The installer auto-detects the repo dir, `python3`, and user; gracefully stops any node already running
on `:5658`; writes `/etc/systemd/system/bismuth-node.service`; and enables + starts it. After that:

```bash
systemctl status  bismuth-node     # up / synced?
systemctl stop    bismuth-node     # graceful stop
systemctl restart bismuth-node     # graceful restart
journalctl -u bismuth-node -f      # follow the logs
```

The service sets `BISMUTH_IGNORE_CONFIG_CUSTOM=1` so it always boots **mainnet** even if a leftover
regnet `config_custom.txt` is present.

### Local dev chain (regnet) + tests

`regnet` is a private, instantly-mineable chain for development — no peers, no real PoW. The test
suite spins one up automatically:

```bash
# run the full test suite (launches a throwaway regnet node, ~90 tests)
python3 -m pytest

# or run a regnet node yourself (REST API on :3031, socket on :3030)
cp tests/config_custom.txt config_custom.txt
python3 node.py regnet2
```

Configuration lives in `config.txt` (see [`doc/11`](doc/11-configuration.md)); regnet/test overrides
are in `tests/config_custom.txt`.

---

## REST API

A modern, read-only HTTP API runs alongside the legacy socket protocol (opt in with `rest_api=True`;
default port `5659`, regnet `3031`). It is self-describing — `GET /api` (or the bare root `/`) lists
every method:

| Endpoint | Purpose |
|---|---|
| `GET /api/status` | node height, peers, difficulty, consensus |
| `GET /api/capabilities` | REST/transport capabilities for peer sync (reachable = capable) |
| `GET /api/block/height/{n}` · `GET /api/block/hash/{h}` | a single block |
| `GET /api/blocks/since/{h}` · `GET /api/blocks/range/{a}/{b}` | block ranges, for **parallel** sync |
| `GET /api/balance/{address}` | confirmed balance |
| `GET /api/transaction/{txid}` | a transaction |
| `GET /api/address/{address}/transactions` | recent txs (`?limit=N`) |
| `GET /api/mempool` · `GET /api/peers` | pending txs · known peers |

**Transport compression** is applied at the HTTP layer: responses are `gzip`/`br` compressed for any
client that sends `Accept-Encoding` (browsers, and the bundled `rest_client.py`). Add `?compress=none`
to read plaintext, or `?compress=gzip|br` to force a codec.

`rest_client.py` is the parallel block-fetch client: it discovers a peer (`GET /api/capabilities`) and
pulls `/api/blocks/range` in concurrent, compressed chunks — the modern alternative to the serial
socket sync. Full REST API reference: [`doc/15`](doc/15-rest-api.md).

---

## Architecture at a glance

| Concern | Where | Notes |
|---|---|---|
| Consensus serialization | `bismuth_serialize.py` | **frozen** signing/block-hash byte forms; the boundary everything hides behind |
| Block processing | `digest.py` | validates and applies blocks |
| PoW / difficulty | `mining_heavy3.py`, `difficulty` | Heavy3 hashing |
| Ledger / storage | `dbhandler.py`, `amounts.py`, SQLite | integer atomic-unit storage behind the frozen boundary (opt-in) |
| Schema migrations | `db_migrations.py` | ordered, `PRAGMA user_version`-tracked |
| Mempool | `mempool.py` | pending transactions |
| Networking (legacy) | `node.py`, `connections.py` | length-prefixed JSON over sockets |
| Networking (modern) | `rest_api.py`, `rest_client.py`, `transport.py` | HTTP API + parallel fetch + compression |
| Crypto / keys | `polysign/` | RSA / ECDSA / ed25519 signers |

Full design docs are in [`doc/`](doc/) — start with [`doc/01-overview.md`](doc/01-overview.md) and the
[file reference](doc/13-file-reference.md).

---

## Documentation

| | | |
|---|---|---|
| [01 Overview](doc/01-overview.md) | [02 Architecture & threads](doc/02-architecture-and-threads.md) | [03 Consensus, blocks, digest](doc/03-consensus-blocks-digest.md) |
| [04 PoW & difficulty](doc/04-pow-and-difficulty.md) | [05 Database & ledger](doc/05-database-and-ledger.md) | [06 Networking protocol](doc/06-networking-protocol.md) |
| [07 Mempool](doc/07-mempool.md) | [08 API & commands](doc/08-api-and-commands.md) | [09 Crypto, wallets, keys](doc/09-crypto-wallets-keys.md) |
| [10 Features](doc/10-features.md) | [11 Configuration](doc/11-configuration.md) | [12 Tooling, build, tests](doc/12-tooling-build-tests.md) |
| [13 File reference](doc/13-file-reference.md) | [14 Known issues & improvements](doc/14-known-issues-and-improvements.md) | [15 REST API](doc/15-rest-api.md) |
| [16 Database rework plan](doc/16-database-rework-plan.md) | [17 Roadmap](doc/17-roadmap.md) | |

---

## Roadmap

Detailed plan: [`doc/17-roadmap.md`](doc/17-roadmap.md). Database deep-dive: [`doc/16`](doc/16-database-rework-plan.md).

**✅ Done**
- **Frozen consensus boundary** — signing-buffer and block-hash byte forms extracted into
  `bismuth_serialize.py` and characterization-locked, so storage/transport can change without touching
  consensus.
- **Schema versioning & migrations** — ordered, `user_version`-tracked (`db_migrations.py`).
- **Integer atomic-unit storage** — exact integer amounts behind the frozen boundary, replay-verified
  (every block hash byte-identical), **live on regnet** behind `ledger_integer_amounts` (default off).
- **Modern HTTP layer** — read-only REST API; capability discovery (reachable = capable); parallel,
  gzip/br-compressed block fetch client (`rest_client.py`); `mainnet0023` capability signal.

**◑ In progress / next**
- Wire the parallel REST fetch into the **actual sync path**, so nodes catch up over the HTTP API
  instead of the serial, blocking socket loop.
- **Incremental balance index** — O(1) maintained credit/debit, bit-matching the authoritative sum.
- **Explicit reward & pruning model** — replace negative-height "mirror" rows and `Hyperblock` string
  rows with real columns/tables.

**🗺️ Planned**
- **Consensus hard fork** — sign/hash native integer units + a binary tx encoding (deleting the
  `'%.8f'`/`'%.2f'` string conversions, all tagged `# HARDFORK (doc/16)`), and adopt a bounded,
  content-derived **txid** (nado-style BLAKE2b, the signature signs the txid).
- **Deprecate the blocking socket protocol** in favour of the HTTP API for node-to-node traffic.
- **Storage-engine evaluation** — modernize SQLite usage, benchmark, and consider a KV store
  (LMDB/RocksDB) for block bodies while keeping SQLite for queryable indexes — decided on data.
- **Repository reorganization** — group modules, retire dead files.

---

## Community & links

- **Website:** https://bismuth.cz · **Explorers:** https://bismuth.online · https://bismuth.im
- **Hypernodes:** https://hypernodes.bismuth.live · https://bismuth.world
- **Wallets:** [Tornado Wallet](https://github.com/bismuthfoundation/TornadoWallet) ·
  [tk-wallet](https://github.com/bismuthfoundation/tk-wallet) ·
  [BIS Paper Wallet](https://github.com/AngainorDev/BIS-Paper) ·
  [Android](https://github.com/redDwarf03/my_bismuth_wallet)
- **Exchanges:** [Implementation guide](https://github.com/bismuthfoundation/Bismuth-FAQ/blob/master/Exchanges/How_to_Implement.md) ·
  [CoinGecko](https://www.coingecko.com/en/coins/bismuth) · [CoinMarketCap](https://coinmarketcap.com/currencies/bismuth/)
- **Social:** [Discord](https://discord.gg/dKVZd4z) · [Reddit](https://www.reddit.com/r/cryptobismuth) ·
  [Telegram](https://t.me/cryptobismuth) · [Blog](https://hypernodes.bismuth.live/?page_id=20)
- **Foundation:** https://github.com/bismuthfoundation

## License

GNU General Public License — see [LICENSE](LICENSE).
