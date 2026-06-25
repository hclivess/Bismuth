# doc/38 — Tor / onion routing

Status: **landed** (clearnet + external + managed modes; managed unit-tested with a mocked controller).
Modernizes the old outbound-only Tor bolt-on (a single bool that flipped three hardcoded
`setproxy(SOCKS5,"127.0.0.1",9050)` calls and silently suppressed the inbound listener) into a proper,
opt-in, self-managing integration — **without changing anything for a clearnet node**.

## Honest scope — there is no daemon-free Tor

Onion routing is a property of the **live Tor network**: your traffic is relayed by volunteers running
the C `tor` daemon, and a v3 hidden service is reachable only because tor publishes its descriptor to the
network. Pure-Python "Tor" implementations are experiments, not anonymity-audited, and must not carry a
coin node's privacy. So the **`tor` C binary remains a hard runtime requirement** for every tor mode.
"Managed" mode does **not** remove the binary — it removes the *manual* burden: no hand-written `torrc`,
no `HiddenServiceDir`, no separate daemon to run/supervise. Bismuth embeds that orchestration via
[`stem`](https://stem.torproject.org/) (the official Tor controller library), launching and owning a tor
child process from an in-memory config and provisioning the hidden service at runtime.

## Modes (`tor` config key — tri-state, backward-compatible)

The old boolean is now a tri-state string, coerced for compatibility (`options._coerce_tor_mode`):

| `tor` value | Mode | Behaviour |
|---|---|---|
| `False`/`0`/`no`/`off`/`""` | **off** (default) | Pure clearnet. `stem` is never imported; the listener binds `0.0.0.0:port`. **Identical to before** — the live mainnet node is unaffected. |
| `True`/`1`/`yes`/`on`/`external` | **external** | Route outbound through an **already-running** tor daemon's SOCKS proxy (the old `tor=True` behavior; inbound listener suppressed). |
| `managed` | **managed** | Bismuth **launches and owns** its own tor (via `stem`): auto SOCKS + control ports, cookie auth, bootstrap gating, an ephemeral v3 onion for inbound, `take_ownership` so tor dies with the node. |

`node.tor` is kept as a back-compat truthy bool (`tor_mode != "off"`) so legacy `if node.tor:` readers
still work; `node.tor_mode` carries the actual mode; `node.tor_manager` is the `TorManager` (or `None`).

## Config reference

| Key | Default | Meaning |
|---|---|---|
| `tor` | `off` | mode: `off` / `external` / `managed` (accepts the legacy bool) |
| `tor_socks_host` | `127.0.0.1` | external-mode SOCKS host |
| `tor_socks_port` | `9050` | external-mode SOCKS port (was the hardcoded literal); managed derives its own |
| `tor_control_port` | `0` | `0` = auto (managed) |
| `tor_binary` | `""` | path to `tor`; empty = discover on `PATH` |
| `tor_onion` | `False` | managed: publish an ephemeral v3 hidden service for inbound |
| `tor_onion_key` | `static/tor_onion_key` | persist the onion ED25519 key (file mode `0600`) for a stable `.onion`; empty = throwaway |
| `tor_required` | `False` | `False` = graceful clearnet fallback on tor failure; `True` = fail-fast (never silently deanonymize) |
| `tor_dial_timeout` | `30` | bounded connect under tor (replaces the old unbounded `None`) |
| `tor_data_dir` | `""` | managed tor's `DataDirectory`; empty = a node-owned dir (`static/tor_data`) |

All defaults reproduce today's behaviour, so a node with no `tor*` lines is clearnet and unchanged.

## Managed mode walkthrough (`tor_manager.TorManager`)

1. `stem.process.launch_tor_with_config({SocksPort: auto, ControlPort: auto, CookieAuthentication: 1,
   DataDirectory: <node-owned>}, take_ownership=True)` — no torrc on disk; tor is a child of the node.
2. Authenticate to the auto control port (cookie), stream `Bootstrapped N%` to the node log, and **wait
   for 100%** (bounded by `_BOOTSTRAP_TIMEOUT`) before the connection manager dials.
3. Read the real SOCKS port back (`get_listeners`) — so the dial sites consume the manager's derived port,
   never a literal (avoids colliding with a system tor on 9050).
4. If `tor_onion`: `create_ephemeral_hidden_service` (v3, ED25519) mapping `<onion>:port -> 127.0.0.1:port`,
   persist the key `0600` for a stable address, and **start the loopback listener** (the onion virtport
   forwards to it) — replacing the old "conceal identity by listening to nothing".
5. `get_proxy()` / `is_ready()` / `onion_address()` / `signal_newnym()` are the query surface used by
   `worker.py`, `peers_storage.py`, and `node.py`.

## What improved over the old implementation

- **Auto-launch** replaces blind "assume a daemon on 9050"; **real inbound** over an ephemeral v3 onion
  replaces the listener-suppression; **bootstrap/health gating** replaces blind connects; the hardcoded
  `127.0.0.1:9050` is now a single configurable source of truth; **bounded dial under tor**
  (`tor_dial_timeout`) replaces the unbounded `None`; **NEWNYM** circuit rotation is available; `stem` is
  optional + lazy; and **graceful fallback / `tor_required`** replaces silent connect failures.

## Graceful fallback vs `tor_required`

On any failure (no `stem`, no tor binary, launch/bootstrap timeout): with `tor_required=False` (default)
the manager logs one loud `FALLING BACK TO CLEARNET` warning and the node continues clearnet; with
`tor_required=True` it raises and refuses to start, so a privacy-critical operator is never silently
deanonymized. Either way it is **bounded — never a hang, never a retry-spin**.

## Installing

The node installer bundles Tor for you (the right way — it installs your distro's `tor` package so it
keeps getting security updates; we deliberately do **not** vendor a tor binary in the repo):

```bash
sudo ./install_node.sh --tor   # installs the tor binary (apt/dnf/yum/brew) + the stem controller lib
```

Or manually: `sudo apt install tor` (the C binary — required for any tor mode) + `pip install stem` (only
for managed mode; clearnet nodes skip it). Then set `tor=managed` (and optionally `tor_onion=True` for
inbound) in `config.toml` / `config.txt`. A clearnet node needs none of this.

## Security notes

Control port is loopback + cookie auth only; the onion key file is `0600` in a node-owned dir;
`DataDirectory` is node-owned; `take_ownership=True` so tor cannot outlive the node.

## Testing & known limitations

- `tests/test_tor.py` is hermetic: config coercion, off/external `get_proxy`, the real
  no-`stem` fallback (this build has neither tor nor stem), the v3-onion regex, and a **mocked-controller**
  managed happy path (SOCKS-port derivation, ephemeral onion, key persistence, NEWNYM, stop). 20 passed.
- **Not yet validated against a real tor network** (none in the build environment). Before running
  `tor=managed` in production, validate on a tor-equipped host. `external` mode preserves the existing,
  field-proven behaviour.
- **Follow-up (staged):** advertising our own `.onion` to peers (gossip injection) and NEWNYM-on-ban
  wiring are designed (recon) but deferred until they can be validated multi-node over a real tor — see
  the design notes. v3 only (v2 onions were removed from the network in 2021).
