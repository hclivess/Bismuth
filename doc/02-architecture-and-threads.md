# 02 — Architecture, startup & threading

## Startup sequence (`node.py`, `if __name__ == "__main__"`)

1. Instantiate the shared singletons: `node = Node()`, `node.logger = Logger()`, `node.keys = Keys()`
   (the data-holder classes in `libs/`). Default the network flags to mainnet. Install the
   `SIGTERM`/`SIGINT` handler (`_graceful_stop`, which raises `IS_STOPPING`) so shutdown is graceful.
2. `config = options.Get(); config.read()` — load `config.toml`, then `config_custom.toml` if present,
   then `mandatory_message.json`. Copy every field onto `node.*` (see [11](11-configuration.md)).
3. Initialise the logger: `node.logger.app_log = log.log("node.log", debug_level, terminal_output)`.
4. Create the plugin manager (`plugins.PluginManager(..., init=True)`) and collect any
   `extra_commands` prefixes plugins register.
5. `setup_net_type()` — decide mainnet/testnet/regnet, set the port, `ledger_path` and peer file, force
   `version='mainnet0023'` / `version_allow` on mainnet, and for regnet call `regnet.init()` and set
   `regnet.DIGEST_BLOCK = digest_block`.
6. `load_keys()` — `essentials.keys_check()` then `essentials.keys_load()`; generates `wallet.der`
   (RSA-4096) on first run. In regnet, the key material is copied onto the `regnet` globals so the
   regtest miner can sign coinbase transactions.
7. `mining_heavy3.mining_open(heavy3_path)` — open/validate the 1 GiB `heavy3a.bin` PoW file
   (skipped when `heavy=False`, e.g. regnet).
8. Build `node.peers = Peers(...)`, `node.apihandler = ApiHandler(...)`, and the in-memory
   `mp.MEMPOOL = mempool.Mempool(...)`.
9. Ledger: `node_init.open_ledger()` opens the LMDB ledger (`blockstore-<ledger name>`). On a fresh
   install it first bootstraps — a DB-direct peer snapshot (doc/43, tip hash verified), else the genesis
   block is seeded (`ledger_genesis`) and the chain syncs from peers. A store on an older schema stops
   the node with the instruction to run `scripts/upgrade_ledger.py`. The fixed mainnet allocations are
   applied, the plugin manager starts (the `tokens_aliases` plugin opens its LMDB index), an optional
   `rollback_to` trigger file is honoured, `node_block_init()` sets the chain tip and difficulty, and an
   optional `verify()` re-checks every signature.
10. Unless Tor: create a `ThreadedTCPServer` on `0.0.0.0:<port>` (`daemon_threads=True`,
    `request_queue_size=100`, per-socket timeout 60 s; command processing has its own
    `timeout_operation` = 120 s inside `handle()`) and run `serve_forever()` in a daemon thread.
11. Start the `ConnectionManager` thread.
12. If `mine=True`, launch the `solo-miner` daemon thread (`miner.mining_loop`, with its own DB handle).
13. Enter the shutdown-watch loop: sleep 0.1 s; when `node.IS_STOPPING` is set and `db_lock` is free,
    call `mining_heavy3.mining_close()` and exit.

## Threads

| Thread | Created by | Loop / job |
|---|---|---|
| **MainThread** | OS | startup, then the shutdown-watch loop |
| **server** (daemon) | `serve_forever()` | accept loop; spawns one handler thread per connection |
| **`in_<ip>`** (per connection) | `ThreadingMixIn` | `ThreadedTCPRequestHandler.handle()` — receive a command, dispatch (big if/elif), reply; 120 s per-op timeout |
| **ConnectionManagerThread** (daemon) | startup | every 30 s: purge mempool periodically, run `peers.client_loop()` to launch outbound workers, log status, fire the `status` plugin hook |
| **`out_<host>_<port>`** (per peer, daemon) | `peers.client_loop()` | `worker()` — outbound sync to one peer (handshake, block/mempool exchange) |
| **solo-miner** (daemon, opt-in `mine=True`) | startup (`node.py` ~1513–1521) | `miner.mining_loop()` — build + mine (Heavy3) + digest a block on the current tip in its own thread (with its own DB handle), serialised against sync via `db_lock` ([21](21-mining.md)) |

All spawned threads are daemons, so they die with the main thread.

## Shared state & locks

| Object | Lock | Role |
|---|---|---|
| `node.db_lock` (`threading.Lock`) | the master DB-write lock | `digest_block()` holds it for the duration of validating+committing a block. All sync paths check `node.db_lock.locked()` and back off rather than block. |
| `mp.MEMPOOL.lock` | mempool's own lock | guards mempool reads/writes; `digest_block()` waits for it to be free before committing. Lock order is `db_lock` → `mempool.lock`. |
| `node.IS_STOPPING` (`bool`) | none (single-writer) | set by the `stop` command; every loop polls it to exit. |
| `node.syncing` (`list`) | none | peers currently being synced from; capped at 3 concurrent. |
| `node.last_block`, `last_block_hash`, `last_block_timestamp`, `hdd_block`, `hdd_hash` | guarded by `db_lock` on write | the in-memory chain tip (`hdd_*` mirror `last_block*`; both are the committed ledger tip). |
| `node.difficulty` (tuple) | atomic reassignment | the current difficulty 8-tuple, refreshed after each block. |
| `node.peers.*` (`connection_pool`, `peer_opinion_dict`, `consensus*`, `banlist`, …) | mostly per-thread mutation | peer registry & consensus tracking (see [06](06-networking-protocol.md)). |

## The shared objects (`libs/`)

`libs/node.py:Node`, `libs/logger.py:Logger`, `libs/keys.py:Keys`, `libs/client.py:Client` are thin
data holders. The node populates `Node`'s attributes from config and runtime state; subsystems
receive the `node` object and read/write its attributes (e.g. `node.logger.app_log.warning(...)`,
`node.db_lock`, `node.last_block`). `Keys` holds the RSA key object plus the readable PEMs, the
base64-encoded public key, and the 56-hex address.

> Post-fork signing note: ordinary single-sig **secp256k1** transactions switch to *recoverable*
> signatures — the tx signs its 32-byte content-hash txid (computed on read by
> `essentials.format_raw_tx`, amount via `amounts.ledger_value`, so it is storage-mode agnostic),
> the `public_key` field is **dropped** from the tx, and the signer is recovered via `ecrecover`
> (low-s enforced). RSA, ED25519, native multisig and shielded/RingCT keep their existing legacy
> signing post-fork (e.g. multisig still carries explicit pubkeys and signs the frozen buffer N-of-M,
> not the txid). All post-fork txs nonetheless use the content-hash txid as their canonical id;
> pre-fork is byte-identical and historical txs keep their `signature[:56]` ids.

## Shutdown

`node_stop.py` connects to `127.0.0.1:<port>` and sends `stop`. The handler sets
`node.IS_STOPPING = True`; the main loop waits for `db_lock` to be free, closes the Heavy3 mmap, and
exits. In-flight server/worker threads are daemons and are terminated on process exit.

`SIGTERM`/`SIGINT` are now handled by a graceful signal handler (`node.py` `_graceful_stop`,
~1239–1242) that simply raises `IS_STOPPING`, so `kill`/Ctrl-C funnels through the same
wait-for-`db_lock` path (an in-flight block finishes its LMDB commit) instead of terminating mid-write. A hard kill is safe
too: a block commit is one LMDB transaction, so the ledger always reopens at a clean block boundary.

> Wiring note: `node.py` previously obtained `regnet`, `mp`, `tokens`, `mining_heavy3`, `difficulty`
> and several stdlib names implicitly via `from digest import *`. That has been replaced with
> explicit imports — see [14](14-known-issues-and-improvements.md). The startup/threading behavior
> is unchanged; only the imports are now explicit (and regnet startup no longer crashes).
