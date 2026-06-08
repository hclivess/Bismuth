# 06 — Networking & wire protocol

A fully custom, thread-per-connection P2P system over raw TCP. No external P2P framework.

## Wire protocol (`connections.py`)

Every message is **a 10-byte ASCII length header (zero-padded, big) + a JSON payload (UTF-8)**:

```
send(sdef, data):  header = str(len(json)).zfill(10);  sendall(header + json)
```

- `SLEN = 10`, `LTIMEOUT = 45 s`. Reads are chunked at 8192 bytes.
- `receive()` has two implementations chosen at import time: Linux uses `select.poll`, others use
  `select.select`. A logical timeout returns the sentinel `"*"`.
- Payloads are any JSON value; commands themselves are sent as JSON strings (`"version"`, `"sync"`…).
- The non-Linux path caps payloads at 100 MB; the Linux path does not (documented hardening item in
  [14](14-known-issues-and-improvements.md)).

`rpcconnections.py` provides a stateful, auto-reconnecting, thread-safe `Connection` class for
*clients* (wallets/scripts), speaking the same framing. It is not used by the node itself.

## Handshake (outbound `worker()` → peer)

1. `send "version"`, then `send node.version` → expect `"ok"`.
2. `send "getversion"` → receive peer version; reject if not in `node.version_allow`.
3. `send "hello"` → peer begins sending sync commands.

## Command catalog (P2P / sync)

| Command | Meaning |
|---|---|
| `version` / `getversion` | protocol version exchange |
| `hello` | sends peer list, then `sync` |
| `sendsync` / `sync` | drive the next sync round |
| `blockheight` | exchange block heights; decide who is ahead |
| `blocksfnd` / `blockscf` / `blocksrj` | "I have blocks" / "send them" / "rejected" (longest-chain or most-common rule) |
| `blocknf` / `blocknfhb` | "your hash not found" (fork signal); `hb` variant = from a hyperblock node |
| `nonewblk` | tips match → exchange mempool |
| `mempool` | followed by the sender's mempool transactions |
| `block` | a freshly mined block (miner → node; mainnet requires ≥5 connections and within 3 blocks of consensus) |

`worker()` lives in `worker.py` (the inbound dispatcher is `node.py`'s `handle()`). Lifecycle:
pre-checks (`IS_STOPPING`, ban, plugin filter) → connect (SOCKS5 via Tor if `tor=True`) → handshake →
open a `DbHandler` → receive loop (dispatch the commands above; call `digest_block()` on confirmed
blocks; cap at 3 concurrent syncers — `len(node.syncing) >= 3`) → on exception, close DB/socket and
remove from the pool. There is no in-worker reconnect; `client_loop()` respawns workers every 30 s
subject to a per-peer **capped** retry backoff (`add_try` in `peers_pool.py`): `30 s → 1 m → 2 m →
5 m` (the old `30 s→5 m→15 m→30 m` schedule stalled a catching-up node for many minutes when only a
handful of peers were live).

## `ConnectionManager` (`connectionmanager.py`)

A 30 s loop: periodically purge the mempool, run `peers.client_loop()` (the only place outbound
workers are spawned — skipped on regnet), log thread/sync/peer/mempool status, and fire the `status`
plugin hook with a dict (`blocks`, `connections`, `difficulty`, `consensus`, `uptime`, …).

## `Peers` (`peershandler.py`)

Thread-safe registry of known and connected peers. The class is now composed from mixins
(`Peers(PeersStorageMixin, PeersPoolMixin, PeersConsensusMixin, PeersAccessMixin,
PeersReputationMixin)`); the files `peers_storage.py` / `peers_pool.py` / `peers_consensus.py` /
`peers_access.py` / `peers_reputation.py` hold the respective method groups. Notable state:
`peer_dict` (`{ip: port}`), `connection_pool` (+ a mirror set `_connection_pool_set`),
`peer_opinion_dict` (`{ip: height}`) → `consensus` / `consensus_percentage`, `banlist` /
`whitelist`, `tried` (retry backoff), `_warning_counts`, and `_reputation` (`{ip: score}`).

- **Files** are single-line JSON `{ip: port}`: `peers.txt` (mainnet), `peers_test.txt` (testnet),
  `peers_reg.txt` (regnet, `{}`), plus `suggested_peers*.txt`. Writes are atomic (`.tmp` + move).
- **Bans**: `warning(ip, reason, count)` accumulates points; at `ban_threshold` the peer is banned.
  Warning sources: forked (+1 outbound / +2 inbound), rollback (+2), consensus deviation (+10). Three
  conditions auto-reset the banlist when the pool is small (`nodes_ban_reset`).
- **Limits**: at most 2 connections per IP C-class (`peers_pool.py`); inbound connections are accepted
  only while `threading.active_count() < thread_limit * 2/3` (or the peer is whitelisted).
- **`is_allowed(ip, command)`** (`peers_access.py`): `stop`/`addpeers` only from `127.0.0.1`;
  `portget` always; `block` always for whitelisted peers; otherwise allowed if `ip in config.allowed`
  or `"any" in config.allowed`.

## Peer reputation & reputation-weighted consensus (`peers_reputation.py`)

The flat 1-peer-1-vote `most_common` height is hardened with a per-peer **reputation score** (the
`PeersReputationMixin`, state `_reputation`), bounded to `[REP_MIN=-100, REP_MAX=100]`:

- `penalize(ip, points, reason)` lowers a peer's score (`PENALTY_INVALID_BLOCK=40`,
  `PENALTY_HEIGHT_LIE=20`, `PENALTY_TIMEOUT=5`); at/under `REP_BAN_BELOW=-50` the peer is banned.
  `reward(ip)` raises it (`REWARD_VALID_BLOCK=5`). Both are **whitelist-immune** and bounded, so they
  can never isolate the node. Scores are the node's *own* observations — a peer cannot inflate its own.
- Wired in: `digest.py` rewards a peer that delivers a valid block and penalizes one whose block fails
  validation; `peers_consensus.consensus_add` penalizes a peer claiming a tip far above consensus.
- **Reputation-weighted tip**: `consensus_reputation_weighted` tallies `peer_opinion_dict` heights
  weighted by `reputation_weight(ip)` (≥1 per peer, so no single peer dictates the tip). It is the
  "most common block" rule used in the sync path — when the tip is stale (`last_block_timestamp <
  now-600`), `node.py`/`worker.py` set `block_req = peers.consensus_reputation_weighted` instead of the
  flat plurality; the longest-chain rule (`consensus_max`) still applies when the tip is fresh. It
  reduces to the plain plurality when reputations are uniform, and block validation backstops whatever
  the node syncs to. `reputable_count` (peers with a positive score) gates deep auto-recovery rollbacks
  so a fresh sybil flood cannot force a reorg.

## Hyperlane

`hyperlane.py` / `hyperlane_asyncio.py` are placeholder managers (log-and-sleep loops); the
`hyperlane` worker command is a no-op (`pass`). The asyncio variant's class-level
`asyncio.get_event_loop()` (which raises on Python ≥3.10) has been fixed to create a loop in
`__init__` — see [14](14-known-issues-and-improvements.md).

## Graceful shutdown

Every loop (worker, ConnectionManager, miner, main) honours the `node.IS_STOPPING` flag. It is raised
two ways: the localhost/whitelisted **`stop` socket command** (see [08](08-api-and-commands.md)), and a
**`SIGTERM`/`SIGINT` handler** installed in `node.py` (`_graceful_stop` → `IS_STOPPING = True`). On
either, the main loop waits for `db_lock` to free (so an in-flight block finishes writing to **both**
`ledger.db` and `hyper.db`), closes Heavy3, and exits — preventing a `kill`/Ctrl-C from terminating
mid-write and leaving the ledger/hyper heights split.
