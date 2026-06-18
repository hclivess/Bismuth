"""tokens_aliases — the token + alias side-index as a MODERN, optional plugin (doc/27).

Post-fork the node core carries NO token/alias logic: this plugin owns it end to end. It is a
``plugin_base.BismuthPlugin`` that:

  * owns an **isolated LMDB store** (``token_index.TokenIndex``, no SQLite — doc/26), namespaced per ledger;
  * reacts to the block lifecycle — ``on_block`` scans each committed block's txs for ``token:issue`` /
    ``token:transfer`` / ``alias=`` and updates the store; ``backfill`` builds the store for history already
    on disk when the plugin is enabled mid-chain;
  * serves its OWN queries — the ``tokensget`` / ``aliasget`` / ``aliasesget`` / ``addfromalias`` wire
    commands and the ``/api/tokens`` + ``/api/token/<name>`` REST endpoints — so nothing token-specific
    remains in the node's command loop or REST router.

It is consensus-inert: it only ever projects already-validated blocks, never changing a block hash or
accepting/rejecting a tx. It is enabled by the ``token_index`` config flag (and, post-fork, unconditionally —
the node has no other token path then). Rollback is driven through the store the plugin registers as the
``token_index`` service (``node.token_index``), so every reorg path the core already handles rolls the
plugin's store back too — reliable, unlike the best-effort ``rollback`` action hook.

The store's overspend rule is preserved EXACTLY (credit at block_height < h, debit at <= h — no same-block
re-spend); see ``token_index``.
"""
from hashlib import blake2b

from plugin_base import BismuthPlugin


def _blake2b_txid(row) -> str:
    return blake2b(str(row).encode(), digest_size=20).hexdigest()


# block_transactions rows (what on_block receives) are the digester's 12-field tuples:
_BH, _TS, _ADDR, _RECIP, _AMT, _SIG, _PUBKEY, _BHASH, _FEE, _REWARD, _OP, _OPENFIELD = range(12)


class TokensAliasesPlugin(BismuthPlugin):
    name = "tokens_aliases"
    version = "0.1.0"

    def __init__(self):
        self.ctx = None
        self.ti = None        # token_index.TokenIndex (None when the plugin is disabled)

    # --- lifecycle ------------------------------------------------------------
    def setup(self, ctx):
        self.ctx = ctx
        # Enabled by the token_index config flag (mainnet stays on the legacy core path until it/the fork
        # turns this on). Disabled -> the plugin loads but is completely inert.
        enabled = bool(getattr(ctx.config, "token_index", False)) if ctx.config is not None else False
        if not enabled:
            ctx.logger.warning("Plugin tokens_aliases: disabled (token_index off) — inert")
            return
        import token_index
        # The store lives beside the ledger, namespaced per ledger filename (no regnet->mainnet bleed).
        self.ti = token_index.open_for(ctx.ledger_path)
        ctx.logger.warning("Plugin tokens_aliases: LMDB store open {}".format(self.ti.stats()))

    def services(self) -> dict:
        """Expose the store as the ``token_index`` service so the node's read/rollback seams
        (``node.token_index``) drive THIS store — the reliable reorg path."""
        return {"token_index": self.ti} if self.ti is not None else {}

    def backfill(self):
        """One-time historical scan when enabled mid-chain: issuances first, then transfers, then aliases —
        from each anchor, idempotent via the store's registry/txid dedup (matches the legacy catch-up)."""
        if self.ti is None:
            return
        ti, ctx = self.ti, self.ctx
        t_anchor = ti.token_anchor()
        for row in ctx.scan_ledger_operations(t_anchor, operations=("token:issue",)):
            self._issue(row[0], row[1], row[3], row[4], row[6])             # (bh, ts, recipient, signature, openfield)
        for row in ctx.scan_ledger_operations(t_anchor, operations=("token:transfer",)):
            self._transfer(row[0], row[1], row[2], row[3], row[4], row[6])  # (bh, ts, address, recipient, signature, openfield)
        a_anchor = ti.alias_anchor()
        for row in ctx.scan_ledger_operations(a_anchor, openfield_like="alias=%"):
            self._alias(row[0], row[2], row[6])                             # (bh, address, openfield)
        ctx.logger.warning("Plugin tokens_aliases: backfill complete {}".format(ti.stats()))

    def on_block(self, height, transactions):
        """Project one committed block: issuances first (so a same-block transfer sees its token), then
        transfers, then aliases. ``transactions`` are the 12-field ledger rows."""
        if self.ti is None:
            return
        for tx in transactions:
            if str(tx[_OP]) == "token:issue" and self._is_reward_zero(tx[_REWARD]):
                self._issue(height, tx[_TS], tx[_RECIP], tx[_SIG], tx[_OPENFIELD])
        for tx in transactions:
            op = str(tx[_OP])
            if op == "token:transfer" and self._is_reward_zero(tx[_REWARD]):
                self._transfer(height, tx[_TS], tx[_ADDR], tx[_RECIP], tx[_SIG], tx[_OPENFIELD])
            if str(tx[_OPENFIELD]).startswith("alias=") and self._is_reward_zero(tx[_REWARD]):
                self._alias(height, tx[_ADDR], tx[_OPENFIELD])

    def on_rollback(self, height):
        # The store is rolled back through the node's db_handler seam (node.token_index.*_rollback), which
        # fires on every reorg path; nothing to do here (kept for the BismuthPlugin contract).
        return

    def teardown(self):
        if self.ti is not None:
            self.ti.close()
            self.ti = None

    # --- per-tx processing (shared by on_block + backfill) --------------------
    @staticmethod
    def _is_reward_zero(reward) -> bool:
        try:
            return int(reward) == 0
        except (TypeError, ValueError):
            return str(reward) in ("0", "0.0", "", "0E-8")

    def _issue(self, height, ts, recipient, signature, openfield):
        try:
            token = str(openfield).split(":")[0].lower().strip()
            if self.ti.has_token(token):
                return
            self.ti.register_issue(height, ts, token, recipient, str(signature)[:56],
                                   str(openfield).split(":")[1])
        except Exception as e:
            self.ctx.logger.warning("tokens_aliases issue parse error: {}".format(e))

    def _transfer(self, height, ts, sender, recipient, signature, openfield):
        try:
            h = abs(int(height))
            token = str(openfield).split(":")[0]
            txid = str(signature)[:56]
            if txid == "0":
                txid = _blake2b_txid((height, ts, sender, recipient, signature, openfield))
            if self.ti.has_txid(txid):
                return
            try:
                amount = int(str(openfield).split(":")[1])
            except Exception:
                amount = 0
            balance = self.ti.token_credit(token, sender, h) - self.ti.token_debit(token, sender, h)
            if balance - amount >= 0 and amount > 0:
                self.ti.apply_transfer(h, ts, token, sender, recipient, txid, amount)
            else:
                self.ti.mark_noop(h, txid)
        except Exception as e:
            self.ctx.logger.warning("tokens_aliases transfer parse error: {}".format(e))

    def _alias(self, height, address, openfield):
        try:
            from essentials import replace_regex
            alias = replace_regex(str(openfield), "alias=")
            self.ti.register_alias(height, address, alias)
        except Exception as e:
            self.ctx.logger.warning("tokens_aliases alias parse error: {}".format(e))

    # --- query surface: wire commands -----------------------------------------
    def peer_commands(self) -> dict:
        if self.ti is None:
            return {}
        return {
            "tokensget": self._cmd_tokensget,
            "aliasget": self._cmd_aliasget,
            "aliasesget": self._cmd_aliasesget,
            "addfromalias": self._cmd_addfromalias,
        }

    def _cmd_tokensget(self, data, socket):
        from connections import send, receive
        address = receive(socket)
        out = [(token, str(self.ti.token_balance(token, address)))
               for (token,) in self.ti.tokens_user(address)]
        send(socket, out)

    def _cmd_aliasget(self, data, socket):
        from connections import send, receive
        address = receive(socket)
        send(socket, self.ti.aliasget(address))

    def _cmd_aliasesget(self, data, socket):
        from connections import send, receive
        addresses = receive(socket)
        send(socket, self.ti.aliasesget(addresses))

    def _cmd_addfromalias(self, data, socket):
        from connections import send, receive
        alias = receive(socket)
        send(socket, self.ti.addfromalias(alias))

    # --- query surface: REST --------------------------------------------------
    def rest_routes(self) -> dict:
        if self.ti is None:
            return {}
        return {
            ("GET", ("tokens",)): self._rest_tokens,
            ("GET", ("token", "*")): self._rest_token,
        }

    def _rest_tokens(self, route, query):
        rows = self.ti.tokens_list(500)
        return {"count": len(rows), "tokens": [{"token": t, "transfers": c} for t, c in rows]}

    def _rest_token(self, route, query):
        # ?limit (default 200, token_detail clamps 1..1000) + ?offset page the holders array, which carries
        # holder_count/holders_returned/truncated so a consumer can tell when it is partial (it used to be
        # silently capped at the top 200 with no flag). Bad values fall back to defaults rather than 500.
        def _q(name, default):
            try:
                return int((query.get(name) or [default])[0])
            except (ValueError, TypeError):
                return int(default)
        detail = self.ti.token_detail(route[1], _q("limit", 200), _q("offset", 0))  # route == ["token","<name>"]
        return detail                                    # None -> 404 (the router maps it)


# The modern manager discovers this single instance and drives its lifecycle.
PLUGIN = TokensAliasesPlugin()
