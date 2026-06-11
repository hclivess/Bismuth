"""Modern plugin framework (doc/27) — a typed, class-based successor to the 2018 ``action_*`` / ``filter_*``
module-function plugins (BismuthPlugins), letting optional features live ENTIRELY OUTSIDE the node core.

The legacy ``plugins.PluginManager`` discovers plugins by magic function names (``action_block``,
``filter_extra_commands_prefixes`` …). That still works (compat). This adds a real **interface**: subclass
``BismuthPlugin``, override the lifecycle/handler methods you need, and the manager drives them. A plugin is:

  * **optional** — load it or don't; the node runs identically without it;
  * **self-contained** — it owns its storage (LMDB, NO SQLite) and derived state in its own data dir;
  * **consensus-inert** — it only ever builds a derived projection of already-validated blocks; it can never
    change a block hash or accept/reject a tx.

The token and alias indexes are the first features extracted this way (doc/26 stage 2 storage → doc/27
plugins): post-fork the node core carries NO token/alias code — the plugins react to the block lifecycle and
serve their own queries. See ``plugins/tokens_aliases``.

This module is pure interface + a tiny context object; it imports nothing from the node, so it is trivially
unit-testable and a plugin written against it never reaches into node internals.
"""
import os

__version__ = "0.1.0"


class PluginContext:
    """Everything a plugin is given at load time — injected, so a plugin never imports node internals.

    Attributes:
        logger      : the app logger (``.warning`` / ``.info`` / ``.error``).
        data_dir    : a directory the plugin owns for its stores (namespaced per ledger by the manager, so a
                      regnet run can never hand its derived state to a mainnet node — the doc/18 pollution class).
        ledger_path : path to the canonical ledger (for a historical backfill scan).
        config      : the node config object (read-only as far as the plugin is concerned).
        is_regnet / is_testnet / is_mainnet : net-type booleans.
    """

    def __init__(self, logger, data_dir, ledger_path, config=None,
                 is_regnet=False, is_testnet=False, is_mainnet=True):
        self.logger = logger
        self.data_dir = data_dir
        self.ledger_path = ledger_path
        self.config = config
        self.is_regnet = is_regnet
        self.is_testnet = is_testnet
        self.is_mainnet = is_mainnet

    def store_path(self, name):
        """An absolute path under the plugin's private data dir (created on demand) for a store/file."""
        os.makedirs(self.data_dir, exist_ok=True)
        return os.path.join(self.data_dir, name)

    def scan_ledger_operations(self, from_height, operations=None, openfield_like=None):
        """Yield ledger rows ``(block_height, timestamp, address, recipient, signature, operation, openfield)``
        with ``block_height >= from_height`` (block order), filtered to ``operations`` (a set/list) and/or an
        ``openfield`` SQL ``LIKE`` pattern — a plugin's one-time HISTORICAL backfill (incremental upkeep goes
        through ``on_block`` instead, which needs no ledger read at all).

        Reads the ledger through a fresh READ-ONLY cursor; it never writes and never holds the node's db lock.
        (Pre-fork the ledger is SQLite; post-fork this is the seam that points at the LMDB block store — the
        plugin's OWN storage is LMDB either way, doc/26.)
        """
        import sqlite3
        conn = sqlite3.connect(self.ledger_path, timeout=1)
        try:
            conn.text_factory = str
            cur = conn.cursor()
            clauses = ["block_height >= ?"]
            params = [int(from_height)]
            if operations:
                ops = list(operations)
                clauses.append("operation IN (%s)" % ",".join("?" * len(ops)))
                params.extend(ops)
            if openfield_like:
                clauses.append("openfield LIKE ?")
                params.append(openfield_like)
            cur.execute(
                "SELECT block_height, timestamp, address, recipient, signature, operation, openfield "
                "FROM transactions WHERE " + " AND ".join(clauses) +
                " AND reward = 0 ORDER BY block_height ASC;", params)
            for row in cur.fetchall():
                yield row
        finally:
            conn.close()


class BismuthPlugin:
    """Base class for a modern Bismuth plugin. Subclass it, set ``name``, override what you need.

    Lifecycle (called by the manager, in order):
        setup(ctx)        once, at load — open stores, stash the context.
        backfill()        once, after setup — build derived state for history already on disk.
        on_block(h, txs)  per committed block — maintain derived state (txs are the 12-field ledger rows).
        on_rollback(h)    per chain reorg — drop derived state at block_height >= h.
        teardown()        at unload/shutdown — close stores.

    Query surface (optional):
        peer_commands()   {command_str: handler(data, socket)} — extra wire-protocol commands.
        rest_routes()     {(method, (seg, ...)): handler(query_dict) -> json} — extra REST endpoints.

    Every method has a safe default, so a minimal plugin overrides only what it uses. NONE of these may touch
    consensus: ``on_block`` runs AFTER a block is committed and only updates the plugin's own store.
    """

    name = "unnamed"
    version = "0"

    # --- lifecycle -------------------------------------------------------------
    def setup(self, ctx: PluginContext):
        self.ctx = ctx

    def backfill(self):
        """Build derived state for blocks already on disk when the plugin is enabled mid-chain. Default: none
        (a from-genesis node gets everything via ``on_block``)."""

    def on_block(self, height, transactions):
        """A block was committed at ``height``; ``transactions`` are its 12-field ledger rows
        ([2]=address [3]=recipient [4]=amount [5]=signature [9]=reward [10]=operation [11]=openfield)."""

    def on_rollback(self, height):
        """A chain reorg dropped blocks at ``block_height >= height``; drop the matching derived state."""

    def teardown(self):
        """Release resources (close stores)."""

    # --- query surface ---------------------------------------------------------
    def peer_commands(self) -> dict:
        """Extra wire-protocol commands: ``{command_str: handler(data, socket)}``. The handler owns the
        socket (``connections.receive`` / ``send``), exactly like a legacy ``filter_extra_commands_prefixes``
        callback."""
        return {}

    def rest_routes(self) -> dict:
        """Extra REST endpoints: ``{(method, (segment, ...)): handler(query_dict) -> json-able}``. A trailing
        ``"*"`` segment is a wildcard capturing the rest of the path (e.g. ``("token", "*")`` -> /api/token/x)."""
        return {}
