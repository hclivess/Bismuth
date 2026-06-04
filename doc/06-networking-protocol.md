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

`worker()` lifecycle: pre-checks (`IS_STOPPING`, ban, plugin filter) → connect (SOCKS5 via Tor if
`tor=True`) → handshake → open a `DbHandler` → receive loop (dispatch the commands above; call
`digest_block()` on confirmed blocks; cap at 3 concurrent syncers) → on exception, close DB/socket
and remove from the pool. There is no in-worker reconnect; `client_loop()` respawns workers every
30 s subject to a 30 s→5 m→15 m→30 m retry backoff.

## `ConnectionManager` (`connectionmanager.py`)

A 30 s loop: periodically purge the mempool, run `peers.client_loop()` (the only place outbound
workers are spawned — skipped on regnet), log thread/sync/peer/mempool status, and fire the `status`
plugin hook with a dict (`blocks`, `connections`, `difficulty`, `consensus`, `uptime`, …).

## `Peers` (`peershandler.py`)

Thread-safe registry of known and connected peers. Notable state: `peer_dict` (`{ip: port}`),
`connection_pool` (+ a mirror set), `peer_opinion_dict` (`{ip: height}`) → `consensus` /
`consensus_percentage`, `banlist` / `whitelist`, `tried` (retry backoff), `_warning_counts`.

- **Files** are single-line JSON `{ip: port}`: `peers.txt` (mainnet), `peers_test.txt` (testnet),
  `peers_reg.txt` (regnet, `{}`), plus `suggested_peers*.txt`. Writes are atomic (`.tmp` + move).
- **Bans**: `warning(ip, reason, count)` accumulates points; at `ban_threshold` the peer is banned.
  Warning sources: forked (+1), rollback/failed-longest-chain (+2), consensus deviation (+10). Three
  conditions auto-reset the banlist when the pool is small (`nodes_ban_reset`).
- **Limits**: at most 2 connections per IP C-class; the active thread count is kept below
  `3 × thread_limit`.
- **`is_allowed(ip, data)`**: `stop`/`addpeers` only from `127.0.0.1`; `portget` always; otherwise
  allowed if `ip in config.allowed` or `"any" in config.allowed`.

## Hyperlane

`hyperlane.py` / `hyperlane_asyncio.py` are placeholder managers (log-and-sleep loops); the
`hyperlane` worker command is a no-op (`pass`). The asyncio variant's class-level
`asyncio.get_event_loop()` (which raises on Python ≥3.10) has been fixed to create a loop in
`__init__` — see [14](14-known-issues-and-improvements.md).
