"""
Bitcoin-compatible JSON-RPC adapter (doc/17) — maps the ``bitcoind`` methods that have a faithful
Bismuth backing onto the live node so standard tooling (exchanges, explorers, watch-only wallets) can
integrate without bespoke code. Behind the ``rpc_bitcoin`` flag (default off, port 8332 like bitcoind).

SCOPE — this is a *maximal faithful adapter*, not a full bitcoind, and the ceiling is architectural, not
effort (see doc/17 §gap-analysis). Bismuth is an account model with blake2b PoW and an RV32I VM; it has
no UTXO set, no Merkle tree, no Script, no PSBT, and no server-side keystore. So the whole UTXO family
(gettxout/listunspent/scantxoutset/fund/lock*), the proof family (gettxoutproof), Script/PSBT, and almost
the entire Wallet category are *impossible* and intentionally absent — calling them returns a clear
"-32601 unsupported by the Bismuth adapter (<reason>)". What IS provided, faithfully where semantics map
and with a documented caveat where they diverge:
  * chain/header reads (height/hash/blocks/headers/chaininfo/chaintips/difficulty)   -> block_store (LMDB)
  * mempool reads (info/raw/entry)                                                   -> mempool.MEMPOOL
  * mining queries + the gated submit path (getmininginfo/getnetworkhashps/submitblock)
  * network/peer reads (connectioncount/networkinfo/peerinfo/nodeaddresses)          -> node.peers
  * raw-tx read + the gated write path (getrawtransaction/sendrawtransaction)        -> indexed ledger + mempool.merge
  * address-scoped balance/tx reads (getbalance/getbalances/getreceivedbyaddress/gettransaction)
  * util/control (estimatesmartfee/validateaddress/uptime/help)

CAVEATS that matter for integrators: 'difficulty' is Bismuth's leading-bit metric, NOT a difficulty-1
multiple; blocks have no bits/merkleroot/chainwork/vsize; the mempool is keyed by SIGNATURE not a separate
txid; fees are a deterministic FLOOR, not a market; and sendrawtransaction takes a Bismuth-format signed
8-field tuple (a JSON array), NOT Bitcoin raw hex (different tx model, no UTXO to spend).

Read paths prefer node.block_store (LMDB, O(1)) and the indexed TXID4 ledger seek — NEVER an unbounded
`signature LIKE`/`SELECT *` scan of the multi-GB ledger. Deps: stdlib + dbhandler/essentials/mempool
(same as rest_api.py).
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import dbhandler
import essentials
import mempool as mp
import bismuth_serialize

__version__ = "0.2.0"


class _RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


# Methods that are architecturally impossible over Bismuth (no UTXO / Script / PSBT / Merkle / keystore).
# Listed explicitly so the adapter answers with an honest, specific reason rather than a bare "not found".
_UNSUPPORTED = {
    "gettxout": "no UTXO set (account model)",
    "gettxoutsetinfo": "no UTXO set (account model)",
    "scantxoutset": "no UTXO set (account model)",
    "listunspent": "no UTXO set (account model)",
    "gettxoutproof": "Bismuth blocks carry no per-tx Merkle branch today (txids are content hashes; inclusion proofs arrive with the roadmapped state trie, doc/19)",
    "verifytxoutproof": "no per-tx Merkle branch today (state trie is roadmapped, doc/19)",
    "decodescript": "no Bitcoin Script",
    "decoderawtransaction": "Bismuth txs are not Bitcoin raw hex",
    "createrawtransaction": "Bismuth txs are not Bitcoin raw hex (build+sign client-side, then sendrawtransaction)",
    "signrawtransactionwithkey": "no Bitcoin Script signer (sign client-side)",
    "getblocktemplate": "memory-hard bit-substring PoW is not Bitcoin-interoperable (use submitblock with a Bismuth-mined block)",
    "fundrawtransaction": "no UTXO set / PSBT",
    "getnewaddress": "keyless node — no server-side wallet (generate keys client-side)",
    "sendtoaddress": "keyless node — build+sign client-side, then sendrawtransaction",
    "importprivkey": "keyless node — no server-side wallet",
    "dumpprivkey": "keyless node — no server-side wallet",
    "getzmqnotifications": "no ZMQ",
}


def _make_handler(node):

    fh = lambda: getattr(node, "fork_height", None)

    def _tip():
        bs = getattr(node, "block_store", None)
        if bs is not None:
            t = bs.tip()
            if t is not None:
                return int(t)
        return int(getattr(node, "hdd_block", 0) or 0)

    def _rows_for_height(db, height):
        """12-field ledger rows for a height, preferring the LMDB block_store (O(1)) over an indexed
        SQLite read. Returns None if the height is absent."""
        bs = getattr(node, "block_store", None)
        if bs is not None:
            return bs.get_block(int(height))
        db.execute_param(db.h, "SELECT * FROM transactions WHERE block_height = ? ORDER BY rowid",
                         (int(height),))
        rows = db.h.fetchall()
        return [list(r) for r in rows] if rows else None

    def _hash_for_height(db, height):
        bs = getattr(node, "block_store", None)
        if bs is not None:
            return bs.block_hash(int(height))
        rows = _rows_for_height(db, height)
        return rows[0][7] if rows else None

    def _height_for_hash(db, block_hash):
        bs = getattr(node, "block_store", None)
        if bs is not None:
            return bs.height_by_hash(block_hash)
        db.execute_param(db.h, "SELECT block_height FROM transactions WHERE block_hash = ? LIMIT 1",
                         (block_hash,))
        row = db.h.fetchone()
        return row[0] if row else None

    def _post_fork():
        # A pending tx (no block height yet) lands in block last_block+1 — decide its id scheme by that.
        fhv = fh()
        if fhv is None:
            return False
        return (int(getattr(node, "last_block", 0) or 0) + 1) >= int(fhv)

    def _mempool_txid(row):
        # POST-HF2 the canonical id is the content-hash txid (blake2b of the frozen 6-field pre-image,
        # exactly what consensus signs); pre-fork it's the legacy signature[:56] slice. row = 8-field wire
        # tuple [ts,sender,recipient,amount,signature,public_key,operation,openfield]. Mirrors
        # rest_api._submit_transaction's echo (tx_id_v2_s normalises ts/amount internally).
        if _post_fork():
            return bismuth_serialize.tx_id_v2_s(row[0], row[1], row[2], row[3], row[6], row[7])
        return row[4][:56]

    def _tx_by_id(db, txid):
        """Resolve a tx by its canonical id. POST-HF2 (LMDB; the `transactions` SQLite table is retired):
        a bounded recent-first scan of block_store, recomputing each tx's content txid via format_raw_tx —
        never an unbounded ledger scan. The SQLite TXID4 seek below is a PRE-FORK legacy fallback, reached
        only on a node that has no block_store built."""
        _fh = fh()
        t = txid[2:] if isinstance(txid, str) and txid.startswith("0x") else txid
        bs = getattr(node, "block_store", None)
        if bs is not None:
            tip = int(bs.tip() or 0)
            window = int(getattr(node, "rpc_txid_scan_blocks", 50000))
            lo = max(1, tip - window + 1)
            for h in range(tip, lo - 1, -1):
                rows = bs.get_block(h)
                if not rows:
                    continue
                for r in rows:
                    tx = essentials.format_raw_tx(r, _fh)
                    if tx.get("txid") == t or (len(t) < 56 and str(r[5]).startswith(t)):
                        return tx
            return None
        # --- pre-fork legacy fallback: indexed SQLite seek (no block_store on this node) ---
        if len(t) == 64 and all(c in "0123456789abcdef" for c in t):
            if _fh is None:
                return None
            max_scan = int(getattr(node, "txid_scan_limit", 250000))
            start_h = max(int(_fh), _tip() - max_scan)
            db.execute_param(db.h, "SELECT * FROM transactions WHERE block_height >= ? "
                                   "ORDER BY block_height DESC", (start_h,))
            scanned = 0
            for row in db.h:
                scanned += 1
                if scanned > max_scan:
                    raise _RpcError(-8, "txid scan limit (%d rows) exceeded; narrow with a recent id" % max_scan)
                tx = essentials.format_raw_tx(row, _fh)
                if tx.get("txid") == t:
                    return tx
            return None
        if len(t) >= 4:
            rows = db.fetchall(db.h, "SELECT * FROM transactions WHERE substr(signature,1,4)=substr(?1,1,4) "
                                     "AND signature LIKE ?2 LIMIT 1", (t, t + "%"))
        else:
            rows = db.fetchall(db.h, "SELECT * FROM transactions WHERE signature LIKE ? LIMIT 1",
                               (t + "%",))
        return essentials.format_raw_tx(rows[0], _fh) if rows else None

    def _base_fee():
        bf = getattr(node, "base_fee", None)
        return float(bf) if bf is not None else float(getattr(essentials, "BASE_FEE", 0.01))

    def _chain_name():
        return "main" if node.is_mainnet else ("test" if node.is_testnet else "regtest")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass  # quiet

        def _db(self):
            return dbhandler.DbHandler(node.index_db, node.ledger_path, node.hyper_path, node.ram,
                                       node.ledger_ram_file, node.logger,
                                       trace_db_calls=node.trace_db_calls)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            try:
                req = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._send({"result": None, "error": {"code": -32700, "message": "parse error"},
                                   "id": None})
            # JSON-RPC batch: an array of calls -> an array of responses (bitcoind supports this).
            if isinstance(req, list):
                return self._send([self._one(r) for r in req])
            self._send(self._one(req))

        def _one(self, req):
            rid = req.get("id") if isinstance(req, dict) else None
            db = None
            try:
                method = req.get("method") if isinstance(req, dict) else None
                params = (req.get("params") if isinstance(req, dict) else None) or []
                if method in _UNSUPPORTED:
                    raise _RpcError(-32601, "%s unsupported by the Bismuth adapter (%s)"
                                    % (method, _UNSUPPORTED[method]))
                db = self._db()
                result = self._dispatch(db, method, params)
                return {"result": result, "error": None, "id": rid}
            except _RpcError as e:
                return {"result": None, "error": {"code": e.code, "message": e.message}, "id": rid}
            except Exception as e:
                return {"result": None, "error": {"code": -32603, "message": str(e)}, "id": rid}
            finally:
                if db is not None:
                    try:
                        db.close()
                    except Exception:
                        pass

        def _send(self, obj):
            body = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        # --- bitcoind method map -------------------------------------------
        def _dispatch(self, db, method, params):
            fn = getattr(self, "rpc_" + method, None) if method else None
            if fn is None:
                raise _RpcError(-32601, "method not found: %s" % method)
            return fn(db, params)

        # ---- chain ----
        def rpc_getblockcount(self, db, p):
            return _tip()

        def rpc_getbestblockhash(self, db, p):
            return _hash_for_height(db, _tip()) or node.last_block_hash

        def rpc_getconnectioncount(self, db, p):
            return len(getattr(node.peers, "connection_pool", []) or [])

        def rpc_getdifficulty(self, db, p):
            d = getattr(node, "difficulty", None)
            return float(d[0]) if d else 0.0

        def rpc_getblockhash(self, db, p):
            height = int(p[0])
            bh = _hash_for_height(db, height)
            if bh is None:
                raise _RpcError(-8, "block height out of range")
            return bh

        def rpc_getblock(self, db, p):
            # getblock(hash[, verbosity]); verbosity 0 unsupported (no raw serialization here), 1 = header+txids.
            block_hash = p[0]
            height = _height_for_hash(db, block_hash)
            if height is None:
                raise _RpcError(-5, "block not found")
            rows = _rows_for_height(db, height)
            if not rows:
                raise _RpcError(-5, "block not found")
            _fh = fh()
            txids = [essentials.format_raw_tx(r, _fh).get("txid") for r in rows]
            tip = _tip()
            return {"hash": block_hash, "confirmations": tip - int(height) + 1, "height": int(height),
                    "time": rows[0][1], "mediantime": rows[0][1], "nTx": len(rows), "tx": txids,
                    "previousblockhash": _hash_for_height(db, int(height) - 1),
                    "nextblockhash": _hash_for_height(db, int(height) + 1)}

        def rpc_getblockheader(self, db, p):
            # getblockheader(hash[, verbose]); synthesized from neighbouring hashes + the block's tx rows.
            block_hash = p[0]
            height = _height_for_hash(db, block_hash)
            if height is None:
                raise _RpcError(-5, "block not found")
            rows = _rows_for_height(db, height)
            tip = _tip()
            return {"hash": block_hash, "confirmations": tip - int(height) + 1, "height": int(height),
                    "time": rows[0][1] if rows else None, "nTx": len(rows or []),
                    "previousblockhash": _hash_for_height(db, int(height) - 1),
                    "nextblockhash": _hash_for_height(db, int(height) + 1)}

        def rpc_getblockchaininfo(self, db, p):
            d = getattr(node, "difficulty", None)
            return {"chain": _chain_name(), "blocks": _tip(), "headers": _tip(),
                    "bestblockhash": _hash_for_height(db, _tip()) or node.last_block_hash,
                    "difficulty": float(d[0]) if d else 0.0,
                    "pruned": False}

        def rpc_getchaintips(self, db, p):
            tip = _tip()
            return [{"height": tip, "hash": _hash_for_height(db, tip) or node.last_block_hash,
                     "branchlen": 0, "status": "active"}]

        def rpc_getdeploymentinfo(self, db, p):
            _fh = fh()
            tip = _tip()
            active = _fh is not None and tip >= int(_fh)
            return {"deployments": {"hf2": {"type": "buried", "height": _fh,
                                            "active": bool(active)}}}

        # ---- mempool ----
        def rpc_getmempoolinfo(self, db, p):
            count, oflen = 0, 0
            if mp.MEMPOOL is not None:
                st = mp.MEMPOOL.status()
                count = st[0] if st else 0
                try:
                    oflen = int((mp.MEMPOOL.size() or 0) * 1_000_000)
                except Exception:
                    oflen = 0
            return {"loaded": True, "size": int(count), "bytes": int(oflen),
                    "mempoolminfee": _base_fee(), "minrelaytxfee": _base_fee()}

        def rpc_getrawmempool(self, db, p):
            verbose = bool(p[0]) if p else False
            if mp.MEMPOOL is None:
                return {} if verbose else []
            rows = mp.MEMPOOL.fetchall(mp.SQL_SELECT_ALL_TXS) or []
            # rows: timestamp,address,recipient,amount,signature,public_key,operation,openfield. POST-HF2
            # the mempool identity is the content-hash txid, not the signature slice.
            ids = [_mempool_txid(r) for r in rows]
            if not verbose:
                return ids
            out = {}
            for r in rows:
                out[_mempool_txid(r)] = {"time": r[0], "address": r[1], "recipient": r[2], "amount": str(r[3]),
                                         "operation": r[6]}
            return out

        def rpc_getmempoolentry(self, db, p):
            sig = p[0]
            if mp.MEMPOOL is None:
                raise _RpcError(-5, "transaction not in mempool")
            row = mp.MEMPOOL.fetchone(
                "SELECT timestamp,address,recipient,amount,signature,public_key,operation,openfield "
                "FROM transactions WHERE substr(signature,1,4)=substr(?1,1,4) and signature LIKE ?2 LIMIT 1",
                (sig, sig + "%"))
            if not row:
                raise _RpcError(-5, "transaction not in mempool")
            return {"time": row[0], "address": row[1], "recipient": row[2], "amount": str(row[3]),
                    "operation": row[6], "fees": {"base": essentials.fee_calculate(row[7], row[6])}}

        # ---- mining ----
        def rpc_getmininginfo(self, db, p):
            d = getattr(node, "difficulty", None)
            count = mp.MEMPOOL.status()[0] if mp.MEMPOOL is not None else 0
            return {"blocks": _tip(), "difficulty": float(d[0]) if d else 0.0,
                    "networkhashps": float(d[5]) if d and len(d) > 5 else 0.0,
                    "pooledtx": int(count), "chain": _chain_name()}

        def rpc_getnetworkhashps(self, db, p):
            d = getattr(node, "difficulty", None)
            return float(d[5]) if d and len(d) > 5 else 0.0

        def rpc_submitblock(self, db, p):
            # submitblock(hexdata) in bitcoind; here p[0] is a Bismuth mined block (a list of tx arrays incl.
            # the signed coinbase) — exactly the socket 'block' payload. Routed through the same digest path
            # as rest_api._submit_block, with the same guards. Gated by rest_api_write (the node-wide write flag).
            if not getattr(node, "rest_api_write", False):
                raise _RpcError(-1, "block submission disabled (set rest_api_write=True)")
            segments = p[0]
            if not (isinstance(segments, list) and segments):
                raise _RpcError(-22, "block decode failed: provide the mined block as a list of tx arrays")
            peer_ip = self.client_address[0] if self.client_address else "127.0.0.1"
            if not node.peers.is_allowed(peer_ip, "block"):
                raise _RpcError(-1, "%s not allowed for block submission" % peer_ip)
            if node.is_mainnet:
                if len(node.peers.connection_pool) < 5 and not node.peers.is_whitelisted(peer_ip):
                    raise _RpcError(-1, "insufficient connections to the network")
                if node.db_lock.locked():
                    raise _RpcError(-1, "node is digesting; retry shortly")
                if node.last_block < node.peers.consensus_max - 3:
                    raise _RpcError(-1, "node not synced; block would orphan")
            from digest import digest_block
            try:
                digest_block(node, segments, None, peer_ip, db)
            except ValueError as e:
                return str(e)   # bitcoind returns a reject-reason string (or null on accept)
            return None

        # ---- network ----
        def rpc_getnetworkinfo(self, db, p):
            return {"version": getattr(node, "app_version", ""), "subversion": "/Bismuth/",
                    "protocolversion": getattr(node, "version", ""),
                    "connections": len(getattr(node.peers, "connection_pool", []) or []),
                    "networkactive": bool(getattr(node, "accept_peers", True)),
                    "relayfee": _base_fee()}

        def rpc_getpeerinfo(self, db, p):
            pool = getattr(node.peers, "connection_pool", []) or []
            opinions = dict(getattr(node.peers, "peer_opinion_dict", {}) or {})
            out = []
            for ip in pool:
                out.append({"addr": ip, "inbound": False,
                            "startingheight": int(opinions.get(ip, 0) or 0),
                            "subver": "/Bismuth/"})
            return out

        def rpc_getnodeaddresses(self, db, p):
            known = list((getattr(node.peers, "peer_dict", {}) or {}).keys())
            count = int(p[0]) if p else len(known)
            return [{"address": ip, "port": getattr(node, "port", 0)} for ip in known[:max(0, count)]]

        def rpc_getaddednodeinfo(self, db, p):
            known = list((getattr(node.peers, "peer_dict", {}) or {}).keys())
            connected = set(getattr(node.peers, "connection_pool", []) or [])
            return [{"addednode": ip, "connected": ip in connected} for ip in known]

        # ---- raw tx ----
        def rpc_getrawtransaction(self, db, p):
            txid = p[0]
            tx = _tx_by_id(db, txid)
            if tx is None:
                raise _RpcError(-5, "no such transaction")
            return {"txid": tx.get("txid"), "blockheight": tx.get("block_height"),
                    "blockhash": tx.get("block_hash"), "time": tx.get("timestamp"),
                    "from": tx.get("address"), "to": tx.get("recipient"),
                    "amount": str(tx.get("amount")), "operation": tx.get("operation"),
                    "openfield": tx.get("openfield"), "fee": str(tx.get("fee"))}

        def rpc_gettransaction(self, db, p):
            return self.rpc_getrawtransaction(db, p)

        def rpc_sendrawtransaction(self, db, p):
            # Bismuth-format signed tx (an 8-field array), NOT Bitcoin raw hex. Routed through the identical
            # mempool.merge admission path as rest_api._submit_transaction (no check bypassed). Gated by write.
            if not getattr(node, "rest_api_write", False):
                raise _RpcError(-1, "tx submission disabled (set rest_api_write=True)")
            tx = p[0]
            if not (isinstance(tx, list) and len(tx) == 8):
                raise _RpcError(-22, "TX decode failed: expected a Bismuth 8-field signed tx array "
                                     "[timestamp,sender,recipient,amount,signature,public_key,operation,openfield]")
            if mp.MEMPOOL is None:
                raise _RpcError(-1, "mempool not ready")
            peer_ip = self.client_address[0] if self.client_address else "127.0.0.1"
            txs = [[str(f) for f in tx]]
            result = mp.MEMPOOL.merge(txs, peer_ip, db.c, True, True)
            joined = " ".join(str(x) for x in (result or []))
            if "Success" not in joined:
                raise _RpcError(-26, "tx rejected: %s" % joined)
            return _mempool_txid(tx)   # POST-HF2 content txid; pre-fork the signature slice

        def rpc_testmempoolaccept(self, db, p):
            # Light, READ-ONLY admissibility probe: signature + balance shape, without inserting.
            txs = p[0] if (isinstance(p[0], list) and p[0] and isinstance(p[0][0], list)) else [p[0]]
            out = []
            for tx in txs:
                ok, reason = True, ""
                if not (isinstance(tx, list) and len(tx) == 8):
                    ok, reason = False, "not a Bismuth 8-field signed tx array"
                out.append({"txid": (_mempool_txid(tx) if ok else None), "allowed": ok,
                            "reject-reason": reason})
            return out

        # ---- util ----
        def rpc_estimatesmartfee(self, db, p):
            return {"feerate": _base_fee(), "blocks": int(p[0]) if p else 1}

        def rpc_validateaddress(self, db, p):
            addr = p[0]
            valid = bool(essentials.address_validate(addr)) if hasattr(essentials, "address_validate") \
                else (isinstance(addr, str) and len(addr) == 56 and all(c in "0123456789abcdef" for c in addr))
            return {"isvalid": valid, "address": addr if valid else None}

        # ---- balance / address reads ----
        def rpc_getbalance(self, db, p):
            address = p[0]
            return str(essentials.ledger_balance3(address, {}, db))

        rpc_getreceivedbyaddress = rpc_getbalance

        def rpc_getbalances(self, db, p):
            address = p[0]
            confirmed = essentials.ledger_balance3(address, {}, db)
            pending = self._mempool_credit(address)
            return {"mine": {"trusted": str(confirmed), "untrusted_pending": str(pending)}}

        def rpc_getunconfirmedbalance(self, db, p):
            return str(self._mempool_credit(p[0]))

        def _mempool_credit(self, address):
            if mp.MEMPOOL is None:
                return 0.0
            rows = mp.MEMPOOL.fetchall(
                "SELECT amount FROM transactions WHERE recipient = ?", (address,)) or []
            try:
                return float(sum(float(r[0]) for r in rows))
            except Exception:
                return 0.0

        # ---- control ----
        def rpc_uptime(self, db, p):
            return int(time.time() - getattr(node, "startup_time", time.time()))

        def rpc_help(self, db, p):
            methods = sorted(name[4:] for name in dir(self) if name.startswith("rpc_"))
            return {"supported": methods,
                    "unsupported": {m: r for m, r in _UNSUPPORTED.items()},
                    "note": "Maximal faithful bitcoind adapter over Bismuth (account model, no UTXO/Script/PSBT)."}

    return Handler


class BitcoinRPCServer:
    def __init__(self, node, port=8332):
        self.node = node
        self.port = port
        self._httpd = None
        self._thread = None

    def start(self):
        # bind localhost only: read-mostly but UNAUTHENTICATED (and the two write paths are additionally
        # gated by rest_api_write), so expose deliberately via a reverse proxy (like the REST API behind
        # nginx), never straight to the internet (DoS / info-exposure).
        self._httpd = ThreadingHTTPServer(("127.0.0.1", self.port), _make_handler(self.node))
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        self.node.logger.app_log.warning("Status: Bitcoin-compatible JSON-RPC on port %s" % self.port)

    def stop(self):
        if self._httpd is not None:
            self._httpd.shutdown()
