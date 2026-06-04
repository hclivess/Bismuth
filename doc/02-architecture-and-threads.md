# 02 — Architecture, startup & threading

## Startup sequence (`node.py`, `if __name__ == "__main__"`)

1. Instantiate the shared singletons: `node = Node()`, `node.logger = Logger()`, `node.keys = Keys()`
   (the data-holder classes in `libs/`). Default the network flags to mainnet.
2. `config = options.Get(); config.read()` — load `config.txt`, then `config_custom.txt` if present,
   then `mandatory_message.json`. Copy every field onto `node.*` (see [11](11-configuration.md)).
3. Initialise the logger: `node.logger.app_log = log.log("node.log", debug_level, terminal_output)`.
4. If running hyperblock-only (`full_ledger=False`), delete `ledger.db` and clone `hyper.db` into it.
5. Create the plugin manager (`plugins.PluginManager(..., init=True)`) and collect any
   `extra_commands` prefixes plugins register.
6. `setup_net_type()` — decide mainnet/testnet/regnet, set the port, DB paths and peer file, force
   `version='mainnet0022'` / `version_allow` on mainnet, and for regnet call `regnet.init()` and set
   `regnet.DIGEST_BLOCK = digest_block`.
7. `load_keys()` — `essentials.keys_check()` then `essentials.keys_load()`; generates `wallet.der`
   (RSA-4096) on first run. In regnet, the key material is copied onto the `regnet` globals so the
   regtest miner can sign coinbase transactions.
8. `mining_heavy3.mining_open(heavy3_path)` — open/validate the 1 GiB `heavy3a.bin` PoW file
   (skipped when `heavy=False`, e.g. regnet).
9. Build `node.peers = Peers(...)`, `node.apihandler = ApiHandler(...)`, and
   `mp.MEMPOOL = mempool.Mempool(...)`.
10. Integrity & bootstrap: `check_integrity()`, open an initial `DbHandler`, `ledger_check_heights()`
    (cross-check ledger vs hyper heights, possibly recompress or roll back), optional `ram_init()`
    (stream `hyper.db` into a shared-cache in-memory DB), `node_block_init()` (set the chain tip),
    `sequencing_check()` (detect block-height gaps), optional `verify()` (full signature re-check),
    `add_indices()`.
11. Unless Tor: create a `ThreadedTCPServer` on `0.0.0.0:<port>` (`daemon_threads=True`,
    `request_queue_size=100`, per-socket timeout 60 s) and run `serve_forever()` in a daemon thread.
12. Start the `ConnectionManager` thread.
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

All spawned threads are daemons, so they die with the main thread.

## Shared state & locks

| Object | Lock | Role |
|---|---|---|
| `node.db_lock` (`threading.Lock`) | the master DB-write lock | `digest_block()` holds it for the duration of validating+committing a block. All sync paths check `node.db_lock.locked()` and back off rather than block. |
| `mp.MEMPOOL.lock` | mempool's own lock | guards mempool reads/writes; `digest_block()` waits for it to be free before committing. Lock order is `db_lock` → `mempool.lock`. |
| `node.IS_STOPPING` (`bool`) | none (single-writer) | set by the `stop` command; every loop polls it to exit. |
| `node.syncing` (`list`) | none | peers currently being synced from; capped at 3 concurrent. |
| `node.last_block`, `last_block_hash`, `last_block_timestamp`, `hdd_block`, `hdd_hash` | guarded by `db_lock` on write | the in-memory chain tip. In RAM mode, `last_block*` track the RAM DB and `hdd_*` track on-disk. |
| `node.difficulty` (tuple) | atomic reassignment | the current difficulty 8-tuple, refreshed after each block. |
| `node.peers.*` (`connection_pool`, `peer_opinion_dict`, `consensus*`, `banlist`, …) | mostly per-thread mutation | peer registry & consensus tracking (see [06](06-networking-protocol.md)). |

## The shared objects (`libs/`)

`libs/node.py:Node`, `libs/logger.py:Logger`, `libs/keys.py:Keys`, `libs/client.py:Client` are thin
data holders. The node populates `Node`'s attributes from config and runtime state; subsystems
receive the `node` object and read/write its attributes (e.g. `node.logger.app_log.warning(...)`,
`node.db_lock`, `node.last_block`). `Keys` holds the RSA key object plus the readable PEMs, the
base64-encoded public key, and the 56-hex address.

## Shutdown

`node_stop.py` connects to `127.0.0.1:<port>` and sends `stop`. The handler sets
`node.IS_STOPPING = True`; the main loop waits for `db_lock` to be free, closes the Heavy3 mmap, and
exits. In-flight server/worker threads are daemons and are terminated on process exit.

> Wiring note: `node.py` previously obtained `regnet`, `mp`, `tokens`, `mining_heavy3`, `difficulty`
> and several stdlib names implicitly via `from digest import *`. That has been replaced with
> explicit imports — see [14](14-known-issues-and-improvements.md). The startup/threading behavior
> is unchanged; only the imports are now explicit (and regnet startup no longer crashes).
