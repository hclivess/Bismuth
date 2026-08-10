# 08 — API surface & CLI

> This page covers the legacy **socket** protocol (the `*` and `api_*` commands). The modern **HTTP/REST**
> read API (`/api/...`, JSON, CORS, the explorer/wallet surface) is documented separately in
> [15-rest-api.md](15-rest-api.md) — that's where `/api/status`, `/api/fee`, `/api/supply`,
> `/api/nodes`, `/api/fork`, etc. live.

The node accepts two kinds of socket commands on its TCP port:

- **Core protocol commands** — dispatched directly inside `node.py`'s handler (the big if/elif).
  These cover peer sync (see [06](06-networking-protocol.md)) plus the wallet/explorer commands below.
- **`api_*` commands** — anything starting with `api_` is routed to
  `ApiHandler.dispatch(method, socket, db_handler, peers)` in `apihandler.py`, which calls
  `getattr(self, method)(...)`. This is the preferred place to add new read-only features.

All commands pass `peers.is_allowed(peer_ip, data)` first (see [06](06-networking-protocol.md)).

## Core wallet/explorer commands (`node.py`)

Each value below is the command string the client sends (often followed by argument frames). JSON
variants return named dicts; the others return raw tuples.

| Command(s) | Purpose |
|---|---|
| `statusget` / `statusjson` | node status (height, connections, difficulty, uptime, consensus, …) |
| `getversion`, `portget`, `diffget(json)`, `difflast(json)` | version / port / difficulty |
| `blocklast(json)`, `blockget(json)`, `block_height_from_hash` | block lookups |
| `balanceget(json)`, `balancegethyper(json)` | address balance (balance, credit, debit, fees, rewards, balance-no-mempool) |
| `addlist`, `addlistlim(json)`, `addlistlimmir(json)`, `listlim(json)` | transaction history (by address / global / mirror) |
| `mpget(json)`, `mpgetjson`, `mpinsert`, `mpclear` | mempool read / insert / clear (clear = localhost) |
| `aliasget`, `aliasesget`, `addfromalias`, `aliascheck` | alias lookups |
| `tokensget` | token balances for an address (uses the fixed `tokens_user` query) |
| `pubkeyget`, `addvalidate`, `keygen(json)` | pubkey / address validation / new keypair |
| `annget`, `annverget`, `peersget`, `addpeers` | announcements / peers |
| `block` | submit a mined block |
| `stop` | raise `IS_STOPPING` for a graceful shutdown (localhost / whitelisted); same flag the `SIGTERM`/`SIGINT` handler sets — see [06](06-networking-protocol.md) |
| `regtest_*` | regnet-only — dispatched to `regnet.command()` (see below) |
| `txsend` | **deprecated / unsafe** — builds & signs a tx server-side from a raw private key |

> `addlistlimmirjson` previously sent its response twice; the duplicate `send()` was removed during
> the revival (see [14](14-known-issues-and-improvements.md)).

### `regtest_*` commands (regnet only, `regnet.command()`)

Any command starting with `regtest_` is rejected unless `node.is_regnet`, then routed to
`regnet.command(...)`. Each is destructive/test-only and re-checks `is_regnet` defensively:

| Command | Effect |
|---|---|
| `regtest_generate <n>` | mint `n` blocks with the regnet-only trivial-nonce generator (`generate_one_block`, up to `TX_PER_BLOCK=2` mempool txs each) |
| `regtest_mine <n>` | drive the **real** solo miner (`miner.generate_block`) for `n` blocks — the actual mainnet code path (mempool txs + hf2 coinbase + dual-algo Heavy3) |
| `regtest_powcheck` | run the dual-algo PoW both ways inside the node and return sha224-vs-blake2b difficulty samples |
| `regtest_rollback <below>` | drive a real chain rollback to height `below-1` (`chain_ops.rollback`) and fix up the tip pointers — exercises the reorg path end-to-end |

## `ApiHandler` (`apihandler.py`) — `api_*` reference

`ApiHandler` is now composed from mixins — `ApiHandler(BlockApiMixin, AddressApiMixin, TxApiMixin)` —
so the `api_*` methods live across `apihandler_blocks.py` / `apihandler_address.py` / `apihandler_tx.py`
(plus a few in `apihandler.py`); `dispatch` still reaches them all via `getattr(self, method)`.

| Command | Returns |
|---|---|
| `api_ping` | `"api_pong"` |
| `api_getconfig` | node config dict |
| `api_mempool` / `api_clearmempool` | mempool rows / clear |
| `api_getaddressinfo` | `{known, pubkey}` for an address |
| `api_getblockfromhash` / `api_getblockfromhashextra` | block by hash (legacy nested / enriched with prev/next/difficulty) |
| `api_getblockfromheight` | block at a height |
| `api_getblockrange` | `limit`≤50 blocks + difficulties (sent as a JSON **string**) |
| `api_getblocksince` | txs in blocks after a height (capped at 10 blocks; JSON-RPC poll) |
| `api_getblockswhereoflike` | txs since a height with an `openfield LIKE` prefix (≤1440 blocks) |
| `api_getaddressrange` / `api_getaddresssince` | txs for an address (range / since height + minconf) |
| `api_getbalance` / `api_getreceived` / `api_listbalance` / `api_listreceived` | balances / received for address lists |
| `api_gettransaction` / `api_gettransactionbysignature` / `api_gettransaction_for_recipients` | tx by id-prefix / full signature / filtered by recipients |
| `api_getpeerinfo` | connected-peer info |
| `api_getblocksafterwhere` | **disabled** — raises `ValueError("Unsafe, do not use yet")` |

Two `api_*` methods build SQL by string interpolation (`api_gettransaction_for_recipients`) or are
deliberately disabled (`api_getblocksafterwhere`) for injection-safety reasons — see
[14](14-known-issues-and-improvements.md).

## CLI & helper scripts

- **`commands.py`** — `python3 commands.py <command> [arg1..arg5]`; connects to the local node
  (port from config) and prints the reply. Wraps most of the commands above. `keygen`/`txsend`
  transmit private keys — localhost use only.
- **`check_tx.py`** — reads `ledger.db`/`mempool.db` directly (no socket) and reports a txid's status
  (`Unknown` / `Mempool` / `Confirmed` + confirmations).
- **`attic/demo_getstatus.py`**, **`attic/demo_getaddresssince.py`** (retired to `attic/`),
  **`cmd_addpeers.py`**, **`cmd_hn_reg_round.py`**, **`cmd_hn_last_block_ts.py`** — minimal examples of
  single commands (the `HN_*` ones target the hypernode companion plugin).
- **`process_search.py`** — `proccess_presence(name)` (sic) checks whether a process is running.
