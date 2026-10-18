"""
Ethereum/ERC compatibility shim (doc/17) — exposes the ``eth_*`` JSON-RPC subset that has a faithful
Bismuth backing so web3-style tooling can read chain data. Behind the ``rpc_ethereum`` flag (default off,
port 8545).

Bismuth has NO contract VM: the node is a pure value/data ledger, so the contract-execution half of the
eth_* surface (eth_call / eth_getCode / eth_getStorageAt / eth_estimateGas / eth_getLogs) has nothing to
read and is answered as unsupported. What this shim DOES back faithfully is chain and account data. The
other bounds are deliberate design divergences:
  * addresses are 28-byte/56-hex (no reversible 20-byte-0x map) — every address arg is a Bismuth address;
  * replay protection is content-txid dedup, not a per-account nonce — eth_getTransactionCount is a tx COUNT;
  * the node is keyless (eth_sendTransaction / eth_sign sign client-side) and the PoW is blake2b, not
    keccak/RLP/EIP-155 (doc/18), so eth_getWork/submitWork aren't Ethereum-miner-interoperable.
Methods with no backing return "-32601 unsupported (<reason>)". eth_sendRawTransaction takes a
Bismuth-native pre-signed 8-field tuple (gated by rest_api_write), not an RLP secp256k1 MetaMask blob.

Provided, hex-encoded the way eth_* clients expect: chain height/blocks(+by hash/index)/tx/balance reads,
fee/sync/peer/mempool(txpool) status, synthetic receipts for value txs, and block/pending-tx polling
filters. POST-HF2 native: ids are content-hash txids; every read goes to the node's ONE LMDB ledger
(``node.ledger``, a ``block_store.BlockStore``) through its indexes. Deps: stdlib +
dbhandler/essentials/mempool (same as rpc_bitcoin.py).
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
_CHAIN_ID = 0xB15  # "BIS"
_WEI = 10 ** 18

# Optional keccak256 for web3_sha3 (Ethereum's Keccak, NOT NIST sha3). Resolved best-effort at import.
try:
    from Crypto.Hash import keccak as _kc
    def _keccak256(b):
        h = _kc.new(digest_bits=256); h.update(b); return h.digest()
except Exception:
    try:
        import sha3 as _sha3   # pysha3 exposes keccak_256
        def _keccak256(b):
            return _sha3.keccak_256(b).digest()
    except Exception:
        _keccak256 = None


def _hex(n):
    return hex(int(n))


# Methods with no backing — answered with an honest reason. Everything contract-shaped is unsupported
# because Bismuth has no contract VM; the rest are deliberate divergences from Ethereum.
_UNSUPPORTED = {
    "eth_call": "DIVERGENCE: no contract VM — Bismuth is a value/data ledger, nothing to execute",
    "eth_estimateGas": "DIVERGENCE: no contract VM — no execution to meter",
    "eth_getCode": "DIVERGENCE: no contract VM — addresses never hold code",
    "eth_getStorageAt": "DIVERGENCE: no contract VM — addresses have no storage",
    "eth_getLogs": "DIVERGENCE: no contract VM — no logs/events are ever emitted",
    "eth_newFilter": "DIVERGENCE: depends on contract logs/events (see eth_getLogs)",
    "eth_getFilterLogs": "DIVERGENCE: depends on contract logs/events (see eth_getLogs)",
    "eth_getProof": "ROADMAP: no state trie — inclusion proofs are not exposed",
    "eth_sendTransaction": "DIVERGENCE: keyless node — sign client-side and use eth_sendRawTransaction (Bismuth tuple)",
    "eth_sign": "DIVERGENCE: keyless node — no server-side keystore",
    "eth_signTransaction": "DIVERGENCE: keyless node — no server-side keystore",
    "eth_getWork": "DIVERGENCE: PoW is blake2b, not keccak/RLP — not Ethereum-miner-interoperable",
    "eth_submitWork": "DIVERGENCE: PoW is blake2b, not keccak/RLP — not Ethereum-miner-interoperable",
    "eth_createAccessList": "DIVERGENCE: no contract VM, no EVM access lists",
    "eth_subscribe": "ROADMAP: no WebSocket transport yet (use the polling filters: eth_newBlockFilter)",
    "eth_unsubscribe": "ROADMAP: no WebSocket transport yet",
    "debug_traceTransaction": "DIVERGENCE: no contract VM to trace / no persisted receipts",
    "debug_traceBlockByNumber": "DIVERGENCE: no contract VM to trace / no persisted receipts",
}


def _make_handler(node):

    # Shared polling-filter registry (eth_newBlockFilter / eth_newPendingTransactionFilter). Lives in the
    # closure so it is shared across per-request Handler instances; guarded by a lock.
    _filters = {}
    _fseq = [0]
    _flock = threading.Lock()

    fh = lambda: getattr(node, "fork_height", None)

    def _ledger():
        bs = getattr(node, "ledger", None)
        return bs if bs is not None else getattr(node, "block_store", None)

    def _tip():
        t = _ledger().tip()
        return int(t) if t is not None else int(getattr(node, "hdd_block", 0) or 0)

    def _resolve_block(tag):
        if isinstance(tag, str):
            if tag in ("latest", "pending", "safe", "finalized"):
                return _tip()
            if tag == "earliest":
                return 1
            if tag.startswith("0x"):
                return int(tag, 16)
        return int(tag)

    def _rows_for_height(db, height):
        return _ledger().get_block(int(height))

    def _hash_for_height(db, height):
        return _ledger().block_hash(int(height))

    def _height_for_hash(db, block_hash):
        try:
            return _ledger().height_by_hash(block_hash)
        except Exception:
            return None

    def _post_fork():
        fhv = fh()
        if fhv is None:
            return False
        return (int(getattr(node, "last_block", 0) or 0) + 1) >= int(fhv)

    def _mempool_txid(row):
        # POST-HF2 content-hash txid; pre-fork the legacy signature[:56]. row = 8-field wire tuple.
        if _post_fork():
            return bismuth_serialize.tx_id_v2_s(row[0], row[1], row[2], row[3], row[6], row[7])
        return row[4][:56]

    def _resolve_tx_row(db, txid):
        """12-field ledger row for a tx id: POST-HF2 the content txid (the ledger's txid index -> its block),
        else the legacy signature[:56] id or any signature prefix (the signature-prefix index)."""
        _fh = fh()
        t = txid[2:] if isinstance(txid, str) and txid.startswith("0x") else txid
        if not isinstance(t, str) or not t:
            return None
        bs = _ledger()
        if _fh is not None:
            try:
                h = bs.txid_height(t)
            except Exception:
                h = None
            if h is not None:
                for r in bs.get_block(h) or ():
                    if essentials.format_raw_tx(r, _fh).get("txid") == t:
                        return r
        if len(t) >= 4:
            rows = bs.rows_by_sig_prefix(t, limit=1)
            if rows:
                return rows[0]
        return None

    def _tx_obj(row):
        # row: 12-field ledger row -> minimal eth-style tx object (hex value, Bismuth from/to addresses).
        _fh = fh()
        tx = essentials.format_raw_tx(row, _fh)
        return {"hash": tx.get("txid"), "blockNumber": _hex(row[0]), "blockHash": row[7],
                "from": row[2], "to": row[3], "value": _hex(int(float(row[4]) * _WEI)),
                "gas": "0x0", "gasPrice": "0x0", "input": "0x", "nonce": "0x0"}

    def _base_fee_wei():
        bf = getattr(node, "base_fee", None)
        bis = float(bf) if bf is not None else float(getattr(essentials, "BASE_FEE", 0.01))
        return int(bis * _WEI)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _db(self):
            return dbhandler.DbHandler.for_node(node)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            try:
                req = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._send({"jsonrpc": "2.0", "id": None,
                                   "error": {"code": -32700, "message": "parse error"}})
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
                    return {"jsonrpc": "2.0", "id": rid,
                            "error": {"code": -32601, "message": "%s unsupported (%s)"
                                      % (method, _UNSUPPORTED[method])}}
                db = self._db()
                return {"jsonrpc": "2.0", "id": rid, "result": self._dispatch(db, method, params)}
            except Exception as e:
                return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": str(e)}}
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

        def _dispatch(self, db, method, params):
            fn = getattr(self, "m_" + method, None) if method else None
            if fn is None:
                raise Exception("method not supported by shim: %s" % method)
            return fn(db, params)

        # ---- node / net constants ----
        def m_eth_blockNumber(self, db, p):
            return _hex(_tip())

        def m_eth_chainId(self, db, p):
            return _hex(_CHAIN_ID)

        def m_net_version(self, db, p):
            return str(_CHAIN_ID)

        def m_net_listening(self, db, p):
            return bool(getattr(node, "accept_peers", True))

        def m_net_peerCount(self, db, p):
            return _hex(len(getattr(node.peers, "connection_pool", []) or []))

        def m_web3_clientVersion(self, db, p):
            return "Bismuth/%s" % node.app_version

        def m_web3_sha3(self, db, p):
            if _keccak256 is None:
                raise Exception("web3_sha3 needs a keccak256 lib (pycryptodome or pysha3); not installed")
            data = p[0]
            raw = bytes.fromhex(data[2:]) if isinstance(data, str) and data.startswith("0x") else \
                (data.encode() if isinstance(data, str) else bytes(data))
            return "0x" + _keccak256(raw).hex()

        def m_eth_protocolVersion(self, db, p):
            return str(getattr(node, "version", ""))

        def m_eth_syncing(self, db, p):
            cur = int(getattr(node, "last_block", _tip()) or 0)
            try:
                high = int(node.peers.consensus_max or cur)
            except Exception:
                high = cur
            if high <= cur:
                return False
            return {"startingBlock": "0x0", "currentBlock": _hex(cur), "highestBlock": _hex(high)}

        # ---- fees ----
        def m_eth_gasPrice(self, db, p):
            return _hex(_base_fee_wei())

        def m_eth_maxPriorityFeePerGas(self, db, p):
            return "0x0"   # Bismuth has no priority-fee market

        def m_eth_blobBaseFee(self, db, p):
            return "0x0"   # no blobs

        def m_eth_feeHistory(self, db, p):
            n = _resolve_block(p[0]) if p else 1
            base = _hex(_base_fee_wei())
            count = max(1, min(int(n) if isinstance(n, int) else 1, 1024))
            tip = _tip()
            return {"oldestBlock": _hex(max(1, tip - count + 1)),
                    "baseFeePerGas": [base] * (count + 1), "gasUsedRatio": [0.0] * count,
                    "reward": None}

        # ---- balances ----
        def m_eth_getBalance(self, db, p):
            # p[0] is a Bismuth address (NOT an 0x-eth address). Wei-scaled so eth clients render whole BIS.
            bal = db.store.balance(p[0])
            return _hex(int(float(bal) * _WEI))

        def m_eth_getTransactionCount(self, db, p):
            # CAVEAT: Bismuth has no account nonce. This is confirmed(+pending) tx COUNT for the address,
            # which is NOT a usable signing nonce (returned only because tooling polls this field).
            addr = p[0]
            confirmed = 0
            try:
                confirmed = int(db.store.address_tx_count(addr, role="sender"))
            except Exception:
                confirmed = 0
            pending = 0
            if mp.MEMPOOL is not None:
                try:
                    pending = sum(1 for r in mp.MEMPOOL.by_address(addr, valid_only=False) if r[1] == addr)
                except Exception:
                    pending = 0
            return _hex(confirmed + pending)

        # ---- blocks ----
        def _block_json(self, db, height, full):
            rows = _rows_for_height(db, int(height))
            if not rows:
                return None
            bh = _hash_for_height(db, int(height))
            txs = [self._tx_obj_idx(rows, i) for i in range(len(rows))] if full else \
                [essentials.format_raw_tx(r, fh()).get("txid") for r in rows]
            return {"number": _hex(height), "hash": bh,
                    "parentHash": _hash_for_height(db, int(height) - 1),
                    "timestamp": _hex(int(float(rows[0][1]))), "transactions": txs,
                    "miner": rows[-1][3] if rows else None, "gasLimit": "0x0", "gasUsed": "0x0",
                    "size": _hex(len(rows)), "uncles": [], "difficulty": "0x0", "nonce": "0x0"}

        def _tx_obj_idx(self, rows, i):
            o = _tx_obj(rows[i])
            o["transactionIndex"] = _hex(i)
            return o

        def m_eth_getBlockByNumber(self, db, p):
            return self._block_json(db, _resolve_block(p[0]), bool(p[1]) if len(p) > 1 else False)

        def m_eth_getBlockByHash(self, db, p):
            height = _height_for_hash(db, p[0])
            if height is None:
                return None
            return self._block_json(db, height, bool(p[1]) if len(p) > 1 else False)

        def m_eth_getBlockTransactionCountByNumber(self, db, p):
            rows = _rows_for_height(db, _resolve_block(p[0]))
            return _hex(len(rows or []))

        def m_eth_getBlockTransactionCountByHash(self, db, p):
            height = _height_for_hash(db, p[0])
            rows = _rows_for_height(db, height) if height is not None else None
            return _hex(len(rows or []))

        def m_eth_getUncleCountByBlockHash(self, db, p):
            return "0x0"   # Bismuth has no uncles/ommers

        def m_eth_getUncleCountByBlockNumber(self, db, p):
            return "0x0"

        # ---- transactions ----
        def m_eth_getTransactionByHash(self, db, p):
            row = _resolve_tx_row(db, p[0])
            return _tx_obj(row) if row else None

        def m_eth_getTransactionByBlockNumberAndIndex(self, db, p):
            rows = _rows_for_height(db, _resolve_block(p[0]))
            idx = int(p[1], 16) if isinstance(p[1], str) and p[1].startswith("0x") else int(p[1])
            return self._tx_obj_idx(rows, idx) if rows and 0 <= idx < len(rows) else None

        def m_eth_getTransactionByBlockHashAndIndex(self, db, p):
            height = _height_for_hash(db, p[0])
            rows = _rows_for_height(db, height) if height is not None else None
            idx = int(p[1], 16) if isinstance(p[1], str) and p[1].startswith("0x") else int(p[1])
            return self._tx_obj_idx(rows, idx) if rows and 0 <= idx < len(rows) else None

        def m_eth_getTransactionReceipt(self, db, p):
            # SYNTHETIC receipt: Bismuth persists no receipts and has no contract VM (so no logs), so
            # status is 0x1 for any included tx and logs are empty — reconstructed, not stored.
            row = _resolve_tx_row(db, p[0])
            if not row:
                return None
            return {"transactionHash": essentials.format_raw_tx(row, fh()).get("txid"),
                    "blockNumber": _hex(row[0]), "blockHash": row[7], "from": row[2], "to": row[3],
                    "status": "0x1", "logs": [], "logsBloom": "0x" + "00" * 256,
                    "gasUsed": "0x0", "cumulativeGasUsed": "0x0", "contractAddress": None}

        # ---- write (Bismuth-native pre-signed tuple only) ----
        def m_eth_sendRawTransaction(self, db, p):
            if not getattr(node, "rest_api_write", False):
                raise Exception("tx submission disabled (set rest_api_write=True)")
            tx = p[0]
            if not (isinstance(tx, list) and len(tx) == 8):
                raise Exception("expected a Bismuth 8-field signed tx array, not an RLP/0x blob "
                                "(MetaMask blobs are not accepted — different curve/address/pre-image)")
            if mp.MEMPOOL is None:
                raise Exception("mempool not ready")
            peer_ip = self.client_address[0] if self.client_address else "127.0.0.1"
            result = mp.MEMPOOL.merge([[str(f) for f in tx]], peer_ip, db, True, True)
            if "Success" not in " ".join(str(x) for x in (result or [])):
                raise Exception("tx rejected: %s" % result)
            return "0x" + _mempool_txid(tx)   # POST-HF2 content txid (0x-prefixed); NOT a 32-byte eth hash

        # ---- txpool ----
        def m_txpool_status(self, db, p):
            count = mp.MEMPOOL.status()[0] if mp.MEMPOOL is not None else 0
            return {"pending": _hex(count), "queued": "0x0"}

        def m_txpool_content(self, db, p):
            pending = {}
            if mp.MEMPOOL is not None:
                for r in (mp.MEMPOOL.wire_transactions() or []):
                    pending.setdefault(r[1], {})[_mempool_txid(r)] = {
                        "from": r[1], "to": r[2], "value": _hex(int(float(r[3]) * _WEI))}
            return {"pending": pending, "queued": {}}

        def m_txpool_inspect(self, db, p):
            pending = {}
            if mp.MEMPOOL is not None:
                for r in (mp.MEMPOOL.wire_transactions() or []):
                    pending.setdefault(r[1], {})[_mempool_txid(r)] = "%s: %s BIS" % (r[2], r[3])
            return {"pending": pending, "queued": {}}

        # ---- misc node state ----
        def m_eth_accounts(self, db, p):
            return []   # keyless node manages no accounts

        def m_eth_coinbase(self, db, p):
            addr = getattr(node, "miner_address", None) or getattr(getattr(node, "keys", None), "address", None)
            if not addr:
                raise Exception("no coinbase address configured")
            return addr

        def m_eth_mining(self, db, p):
            return bool(getattr(node, "mining", False) or getattr(node, "is_mining", False))

        def m_eth_hashrate(self, db, p):
            d = getattr(node, "difficulty", None)   # network estimate (no per-node counter)
            return _hex(int(d[5])) if d and len(d) > 5 else "0x0"

        def m_eth_submitHashrate(self, db, p):
            return True   # no backing store; accept and acknowledge (no-op)

        # ---- polling filters (block / pending-tx; NOT log filters) ----
        def m_eth_newBlockFilter(self, db, p):
            with _flock:
                _fseq[0] += 1
                fid = _hex(_fseq[0])
                _filters[fid] = {"type": "block", "last": _tip()}
            return fid

        def m_eth_newPendingTransactionFilter(self, db, p):
            seen = set()
            if mp.MEMPOOL is not None:
                seen = {_mempool_txid(r) for r in (mp.MEMPOOL.wire_transactions() or [])}
            with _flock:
                _fseq[0] += 1
                fid = _hex(_fseq[0])
                _filters[fid] = {"type": "pending", "seen": seen}
            return fid

        def m_eth_getFilterChanges(self, db, p):
            fid = p[0]
            with _flock:
                f = _filters.get(fid)
                if f is None:
                    raise Exception("filter not found")
                if f["type"] == "block":
                    tip = _tip()
                    out = [_hash_for_height(db, h) for h in range(f["last"] + 1, tip + 1)]
                    f["last"] = tip
                    return [h for h in out if h]
                cur = set()
                if mp.MEMPOOL is not None:
                    cur = {_mempool_txid(r) for r in (mp.MEMPOOL.wire_transactions() or [])}
                fresh = ["0x" + s for s in (cur - f["seen"])]
                f["seen"] = cur
                return fresh

        def m_eth_uninstallFilter(self, db, p):
            with _flock:
                return _filters.pop(p[0], None) is not None

    return Handler


class EthereumRPCServer:
    def __init__(self, node, port=8545):
        self.node = node
        self.port = port
        self._httpd = None

    def start(self):
        # bind localhost only: unauthenticated read shim (the lone write path is additionally gated by
        # rest_api_write), expose deliberately via a reverse proxy.
        self._httpd = ThreadingHTTPServer(("127.0.0.1", self.port), _make_handler(self.node))
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        self.node.logger.app_log.warning("Status: Ethereum-compatible JSON-RPC shim on port %s" % self.port)

    def stop(self):
        if self._httpd is not None:
            self._httpd.shutdown()
