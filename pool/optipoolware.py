# optipoolware.py v 0.397 to be used with Python3.5 or later
# Bismuth pool mining software
# Copyright Hclivess, Maccaspacca 2017, 2018
# for license see LICENSE file
# .

import connections, time, options, log, sqlite3, socks, hashlib, random, re, essentials, base64, sys, os, math
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer   # miner<->pool is HTTP now
from urllib.parse import urlparse
from Cryptodome import Random
from Cryptodome.Hash import SHA
from Cryptodome.Signature import PKCS1_v1_5
from Cryptodome.PublicKey import RSA
import threading
import statistics
import json

import logging
import mining_heavy3 as mining
import bismuth_serialize

config = options.Get()
config.read()
port = config.port
node_ip_conf = config.node_ip
ledger_path_conf = "static/ledger.db"
#tor_conf = config.tor
debug_level_conf = config.debug_level
version = config.version

# Logger up-front so every line in this module goes through it (no raw print anywhere).
# File log via the node's log factory; a stdout StreamHandler mirrors INFO+ to the console.
app_log = log.log("pool.log", debug_level_conf)
ch = logging.StreamHandler(sys.stdout)
ch.setLevel(logging.INFO)
ch.setFormatter(logging.Formatter('%(asctime)s %(funcName)s(%(lineno)d) %(message)s'))
app_log.addHandler(ch)

if version == "testnet":
    port = "2829"
    m_peer_file = "peers_test.txt"
    ledger_path_conf = "static/test.db"
else:
    m_peer_file = "peers.txt"

app_log.info("Peers file: {}".format(m_peer_file))

key, public_key_readable, private_key_readable, encrypted, unlocked, public_key_hashed, address, keyfile = essentials.keys_load("privkey.der", "pubkey.der")

app_log.info("Pool Address: {}".format(address))

# load config (pool.toml — stdlib tomllib; typed, with defaults)
import tomllib
_pool_cfg = {}
try:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "pool.toml"), "rb") as _f:
        _pool_cfg = tomllib.load(_f)
except Exception as e:
    app_log.warning("pool.toml not loaded ({}); using defaults".format(e))
mdiff = int(_pool_cfg.get("mine_diff", 65))
min_payout = float(_pool_cfg.get("min_payout", 1))
pool_fee = float(_pool_cfg.get("pool_fee", 0))
alt_fee = float(_pool_cfg.get("alt_fee", 0))
w_time = int(_pool_cfg.get("worker_time", 10))
alt_add = str(_pool_cfg.get("alt_add", "92563981cc1e70d160c176edf368ea4bbc1d8d5ba63aceee99ef6ebd"))
# hf2 pubkey-by-reference (doc/40): a pool pays from ONE RSA address thousands of times, so its ~736-byte
# base64 pubkey on every payout is the single biggest per-tx wire waste. When this is on AND the node
# confirms the pool address is registered (a prior confirmed payout), omit the pubkey (public_key="") and
# let the node resolve it by address. Default OFF + fail-closed: carry inline whenever unsure.
pubkey_by_reference = bool(_pool_cfg.get("pubkey_by_reference", False))

# --- hf2 fork awareness (doc/18-D / doc/22 / doc/39) ----------------------------------------------
# Mirror the node's miner._new_pow + _coinbase_prefix WITHOUT a node object, by reading the node's REST
# fork view (/api/fork) + the post-fork VM pre-state root (/api/vm/contracts). Until the fork activates
# these stay (False, "") so the pool behaves EXACTLY as pre-hf2 — zero change on mainnet today.
# fork_signal is the node's own config flag. NEEDS LIVE VALIDATION across the activation boundary (the
# blake2b switch mirrors the tested node code, but the VM-state-root commitment + the exact boundary
# can only be confirmed against a live post-activation node — see pool/README.md / doc/39).
import fork
import urllib.request

# ALL node communication is over the node's REST API (doc/15) — no legacy socket calls to the node.
# Requires the node started with rest_api=True (reads) + rest_api_write=True (payout + block submit).
REST_PORT = int(getattr(config, "rest_api_port", 5659))
REST_BASE = "http://%s:%d/api" % (node_ip_conf, REST_PORT)


def _node_get(path, timeout=10):
    with urllib.request.urlopen(REST_BASE + path, timeout=timeout) as _r:
        return json.load(_r)


def _node_post(path, body, timeout=15):
    _req = urllib.request.Request(REST_BASE + path, data=json.dumps(body).encode("utf-8"),
                                  headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(_req, timeout=timeout) as _r:
        return json.load(_r)


new_pow = False        # blake2b Heavy3 active for the block being mined?
cb_prefix = ""         # coinbase-openfield prefix: fork.FORK2_SIGNAL ("hf2") [+ VM state root post-fork]


def _refresh_fork_state(tip_height):
    """Set the module globals new_pow + cb_prefix from the node's REST fork view. Safe fallback to
    pre-fork (False, "") if the node REST is unreachable, so the pool keeps working."""
    global new_pow, cb_prefix
    try:
        fh = _node_get("/fork").get("fork_height")
        np = fh is not None and (int(tip_height) + 1) >= int(fh)
        prefix = fork.FORK2_SIGNAL if getattr(config, "fork_signal", False) else ""
        if np:
            # post-fork the coinbase MUST also commit the VM pre-state root (doc/19); fetch it from the node
            try:
                sr = _node_get("/vm/contracts").get("state_root")
                if sr:
                    import vm_engine
                    prefix += vm_engine.embed_state_root(sr, "")
            except Exception:
                pass
        new_pow, cb_prefix = np, prefix
    except Exception:
        new_pow, cb_prefix = False, ""

bin_format_dict = dict((x, format(ord(x), '8b').replace(' ', '0')) for x in '0123456789abcdef')

def percentage(percent, whole):
    return int((percent * whole) / 100)

def checkdb():
    shares = sqlite3.connect('shares.db')
    shares.text_factory = str
    s = shares.cursor()
    s.execute("SELECT * FROM shares")
    present = s.fetchall()

    if not present:
        return False
    else:
        return True

# payout processing

def payout(payout_threshold,myfee,othfee):

    app_log.info("Minimum payout is {} Bismuth".format(str(payout_threshold)))
    app_log.info("Current pool fee is {} Percent".format(str(myfee)))

    shares = sqlite3.connect('shares.db')
    shares.text_factory = str
    s = shares.cursor()

    #get sum of all shares not paid
    s.execute("SELECT sum(shares) FROM shares WHERE paid != 1")
    shares_total = s.fetchone()[0]
    #get sum of all shares not paid

    #get block threshold
    try:
        s.execute("SELECT min(timestamp) FROM shares WHERE paid != 1")
        block_threshold = float(s.fetchone()[0])
    except:
        block_threshold = time.time()
    #get block threshold

    #get eligible blocks (the pool's mined coinbase rewards) from the node REST — NOT a ledger.db scan
    reward_list = []
    try:
        for t in _node_get("/address/%s/transactions?limit=500" % address).get("transactions", []):
            if float(t.get("reward") or 0) != 0 and float(t.get("timestamp") or 0) >= block_threshold:
                reward_list.append(float(t["reward"]))
    except Exception as e:
        app_log.warning("payout: could not read pool rewards from the node REST: {}".format(e))

    super_total = sum(reward_list)
    #get eligible blocks

    # so now we have sum of shares, total reward, block threshold

    # reduce total rewards by total fees percentage
    reward_total = "%.8f" % (((100-(myfee+othfee))*super_total)/100)
    reward_total = float(reward_total)

    if reward_total > 0:

        # calculate alt address fee

        ft = super_total - reward_total
        try:
            at = "%.8f" % (ft * (othfee/(myfee+othfee)))
        except:
            at = 0

        # calculate reward per share
        reward_per_share = reward_total / shares_total

        # calculate shares threshold for payment

        shares_threshold = math.floor(payout_threshold/reward_per_share)

        #get unique addresses
        addresses = []
        for row in s.execute("SELECT * FROM shares"):
            shares_address = row[0]

            if shares_address not in addresses:
                addresses.append(shares_address)
        print (addresses)
        #get unique addresses

        # prepare payout address list with number of shares and new total shares
        payadd = []
        new_sum = 0
        for x in addresses:
            s.execute("SELECT sum(shares) FROM shares WHERE address = ? AND paid != 1", (x,))
            shares_sum = s.fetchone()[0]

            if shares_sum == None:
                shares_sum = 0
            if shares_sum > shares_threshold:
                payadd.append([x,shares_sum])
                new_sum = new_sum + shares_sum
        #prepare payout address list with number of shares and new total shares

        # recalculate reward per share now we have removed those below payout threshold
        try:

            reward_per_share = reward_total / new_sum

        except:
            reward_per_share = 0

        app_log.info("Reward per share: {}".format(reward_per_share))

        paylist = []
        for p in payadd:
            payme =  "%.8f" % (p[1] * reward_per_share)
            paylist.append([p[0],payme])

        if othfee > 0:
            paylist.append([alt_add,at])

        payout_passed = 0
        # Canonical fee: track the node's live base_fee (/api/fee) via essentials.fee_calculate so post-fork
        # congestion pricing (fee_dynamics) is honoured. Pre-fork base_fee is the static BASE_FEE, so this
        # equals the old 0.01 + len(openfield)/100000. Queried once per run (openfield is the constant "pool").
        try:
            _base_fee = (_node_get("/fee") or {}).get("base_fee")
        except Exception:
            _base_fee = None                     # node unreachable -> essentials falls back to static BASE_FEE
        for r in paylist:
            app_log.info("Paying {}".format(r))
            recipient = r[0]
            claim = float(r[1])

            payout_passed = 1
            openfield = "pool"
            keep = 0
            fee = float(essentials.fee_calculate(openfield, operation=str(keep), base_fee=_base_fee))
            #make payout

            timestamp = '%.2f' % time.time()
            transaction = (str(timestamp), str(address), str(recipient), '%.8f' % float(claim - fee), str(keep), str(openfield))  # this is signed
            # print transaction

            # signed over the frozen 6-field buffer (byte-identical to str(transaction).encode())
            h = SHA.new(bismuth_serialize.signature_buffer(*transaction))
            signer = PKCS1_v1_5.new(key)
            signature = signer.sign(h)
            signature_enc = base64.b64encode(signature)
            app_log.info("Encoded signature: {}".format(signature_enc.decode("utf-8")))

            verifier = PKCS1_v1_5.new(key)
            if verifier.verify(h, signature) == True:
                app_log.info("Signature valid, submitting payout transaction")
                # legacy label only; post-hf2 the canonical txid is blake2b(content), so prefer the
                # node's returned txid (POST /api/transaction -> {"txids": [...]}) over this slice.
                mytxid = signature_enc[:56].decode("utf-8")
                # hf2 pubkey-by-reference (doc/40): omit the pool's RSA pubkey when the node confirms it is
                # registered (a prior confirmed payout). Fail-closed: any uncertainty -> carry inline.
                pub_field = str(public_key_hashed.decode("utf-8"))
                if pubkey_by_reference:
                    try:
                        if _node_get("/pubkey/%s" % address).get("registered"):
                            pub_field = ""        # registered -> the node resolves the key by address
                    except Exception:
                        pass                      # node unreachable / no registry -> inline (safe default)
                tx_submit = (str(timestamp), str(address), str(recipient), '%.8f' % float(claim - fee), str(signature_enc.decode("utf-8")), pub_field, str(keep), str(openfield)) #float kept for compatibility

                # submit the payout over REST (was the socket mpinsert command)
                try:
                    reply = _node_post("/transaction", {"transaction": list(tx_submit)})
                    node_txid = (reply.get("txids") or [None])[0] if isinstance(reply, dict) else None
                    app_log.info("Payout {} submitted via REST: {}".format(node_txid or mytxid, reply))
                except Exception as e:
                    reply = "REST submit failed: {}".format(e)
                    app_log.warning(reply)
            else:
                app_log.warning("Invalid signature, skipping payout")
                reply = "Invalid signature"

            s.execute("UPDATE shares SET paid = 1 WHERE address = ?",(recipient,))
            shares.commit()

        if payout_passed == 1:
            s.execute("UPDATE shares SET timestamp = ?", (time.time(),))
            shares.commit()

        # calculate payouts
        #payout

        # archive paid shares
        s.execute("SELECT * FROM shares WHERE paid = 1")
        pd = s.fetchall()

        if pd == None:
            pass
        else:
            archive = sqlite3.connect('archive.db')
            archive.text_factory = str
            a = archive.cursor()

            for sh in pd:
                a.execute("INSERT INTO shares VALUES (?,?,?,?,?,?,?,?)", (sh[0],sh[1],sh[2],sh[3],sh[4],sh[5],sh[6],sh[7]))

            archive.commit()
            a.close()
        # archive paid shares

    # clear nonces
    s.execute("DELETE FROM nonces")
    s.execute("DELETE FROM shares WHERE paid = 1")
    shares.commit()
    s.execute("VACUUM")
    #clear nonces
    s.close()


def commit(cursor):
    # secure commit for slow nodes
    passed = 0
    while passed == 0:
        try:
            cursor.commit()
            passed = 1
        except Exception as e:
            app_log.warning("Retrying database execute due to " + str(e))
            time.sleep(random.random())
            pass
            # secure commit for slow nodes


def execute(cursor, what):
    # secure execute for slow nodes
    passed = 0
    while passed == 0:
        try:
            # print cursor
            # print what

            cursor.execute(what)
            passed = 1
        except Exception as e:
            app_log.warning("Retrying database execute due to {}".format(e))
            time.sleep(random.random())
            pass
            # secure execute for slow nodes
    return cursor


def execute_param(cursor, what, param):
    # secure execute for slow nodes
    passed = 0
    while passed == 0:
        try:
            # print cursor
            # print what
            cursor.execute(what, param)
            passed = 1
        except Exception as e:
            app_log.warning("Retrying database execute due to " + str(e))
            time.sleep(0.1)
            pass
            # secure execute for slow nodes
    return cursor


def bin_convert(string):
    return ''.join(bin_format_dict[x] for x in string)

def bin_convert_orig(string):
    return ''.join(format(ord(x), '8b').replace(' ', '0') for x in string)

def s_test(testString):

    if testString.isalnum():
        if (re.search('[abcdef]',testString)):
            if len(testString) == 56:
                return True
    else:
        return False

def n_test(testString):

    if testString.isalnum():
        if (re.search('[abcdef]',testString)):
            if len(testString) < 129:
                return True
    else:
        return False

def paydb():

    while True:
        app_log.warning("Payout run finished")
        time.sleep(3601)
        #time.sleep(60) # test
        v = float('%.2f' % time.time())
        v1 = new_time
        v2 = v - v1

        if v2 < 300:
            payout(min_payout,pool_fee,alt_fee)
            app_log.warning("Payout running...")
        else:
            app_log.warning("Node over 5 mins out...payout delayed")

def worker(s_time):

    global new_diff
    global new_hash
    global new_time

    while True:

        time.sleep(s_time)

        try:

            app_log.warning("Worker task...")
            # node REST instead of the socket blocklast/diffget commands
            st = _node_get("/status")
            new_hash = st.get("last_block_hash")
            new_time = st.get("server_timestamp")
            new_diff = math.floor(float(_node_get("/difficulty").get("difficulty")))
            # hf2: refresh new_pow + the coinbase prefix from the node for the block we're about to mine
            _refresh_fork_state(st.get("blocks"))

            app_log.warning("Difficulty = {}".format(str(new_diff)))
            app_log.warning("Blockhash = {}".format(str(new_hash)))

        except Exception as e:
            app_log.warning(str(e))

# TODO: for tests only
#os.remove('shares.db')
#os.remove('archive.db')


if not os.path.exists('shares.db'):
    # create empty shares
    shares = sqlite3.connect('shares.db')
    shares.text_factory = str
    s = shares.cursor()
    execute(s, "CREATE TABLE IF NOT EXISTS shares (address, shares, timestamp, paid, rate, name, workers, subname)")
    execute(s, "CREATE TABLE IF NOT EXISTS nonces (nonce)") #for used hash storage
    app_log.warning("Created shares file")
    s.close()
    # create empty shares
if not os.path.exists('archive.db'):
    # create empty archive
    archive = sqlite3.connect('archive.db')
    archive.text_factory = str
    a = archive.cursor()
    execute(a, "CREATE TABLE IF NOT EXISTS shares (address, shares, timestamp, paid, rate, name, workers, subname)")
    app_log.warning("Created archive file")
    a.close()
    # create empty archive

"""
if checkdb():
    payout(min_payout,pool_fee,alt_fee)
"""

# RSA key used to sign the coinbase reward tx (imported once)
_signing_key = RSA.importKey(private_key_readable)


def _build_work():
    """The work package handed to a miner (was the socket 'getwork' reply). hf2: cb_prefix + new_pow tell
    the miner to fold the fork-signal into the openfield and switch sha224->blake2b."""
    return {"blockhash": new_hash, "diff": mdiff, "pool_address": address,
            "netdiff": new_diff, "cb_prefix": cb_prefix, "new_pow": new_pow}


def process_share(miner_address, sh):
    """Validate a miner's submitted share and, if it meets the NETWORK difficulty, build + sign + submit
    the block (via the node REST POST /api/block, socket broadcast as fallback). Record the share. Returns
    a result dict. (This is the old socket 'block' handler, now driven by the HTTP /share endpoint.)"""
    if not s_test(miner_address):
        app_log.warning("Bad miner address - using the default (alt_add)")
        miner_address = alt_add

    block_timestamp = str(sh.get("block_timestamp"))
    nonce = str(sh.get("nonce"))
    mine_hash = str(sh.get("blockhash"))
    mrate = sh.get("rate", 0)
    bname = str(sh.get("worker_base", ""))
    wnum = sh.get("workers", 1)
    wstr = str(sh.get("worker_num", ""))
    wname = "{}{}".format(bname, wstr)

    if not n_test(nonce):
        app_log.warning("Bad nonce format from miner")
        return {"accepted": False, "reason": "bad nonce format"}
    app_log.warning("Solution from {}: nonce={} claimed_diff={}".format(miner_address, nonce, sh.get("sdiff")))

    diff = new_diff
    db_block_hash = mine_hash
    # hf2: validate with the SAME algo the node consensus uses (the submitted nonce already includes the
    # miner's cb_prefix, so it IS the coinbase openfield: diffme_heavy3 hashes address+openfield+blockhash).
    real_diff = mining.diffme_heavy3(address, nonce, db_block_hash, new_pow=new_pow)

    block_found = real_diff >= int(diff)
    if block_found:
        app_log.warning("Network difficulty met -- building + submitting block")
        result = _node_get("/mempool").get("transactions", [])
        app_log.warning("Pulled {} mempool tx(s) from the node".format(len(result)))
        block_send = []
        for d in result:
            block_send.append((
                str(d["timestamp"]), str(d["address"][:56]), str(d["recipient"][:56]),
                '%.8f' % float(d["amount"]), str(d["signature"]), str(d["public_key"]),
                str(d["operation"]), str(d["openfield"])))
        # claim reward. hf2 §2.C coinbase compaction: post-fork (new_pow) the coinbase carries NO signature
        # + public key (PoW + reward formula authorize it; the node enforces empty). Pre-fork: RSA-signed.
        if new_pow:
            block_send.append((str(block_timestamp), str(address[:56]), str(address[:56]), '%.8f' % float(0),
                               "", "", "0", str(nonce)))  # sig-less coinbase
            if not any(isinstance(el, list) for el in block_send):
                block_send = [block_send]
        else:
            # frozen 6-field signing buffer via the single consensus authority (byte-identical to the
            # old inline str((...)).encode())
            h = SHA.new(bismuth_serialize.signature_buffer(
                str(block_timestamp), str(address[:56]), str(address[:56]), '%.8f' % float(0), "0", str(nonce)))
            signer = PKCS1_v1_5.new(_signing_key)
            signature_enc = base64.b64encode(signer.sign(h))
            block_send.append((str(block_timestamp), str(address[:56]), str(address[:56]), '%.8f' % float(0),
                               str(signature_enc.decode("utf-8")), str(public_key_hashed.decode("utf-8")),
                               "0", str(nonce)))  # mining reward tx
            if not any(isinstance(el, list) for el in block_send):
                block_send = [block_send]   # make it a list of lists

        # submit to the node over REST POST /api/block (doc/39); socket peer-broadcast as fallback
        submitted_via_rest = False
        try:
            _res = _node_post("/block", {"block": block_send})
            if _res.get("accepted"):
                submitted_via_rest = True
                app_log.warning("Block submitted via REST /api/block: height={} hash={}".format(
                    _res.get("block_height"), _res.get("block_hash")))
            else:
                app_log.warning("REST /api/block rejected ({}); falling back to socket broadcast"
                                .format(_res.get("reason")))
        except Exception as e:
            app_log.warning("REST /api/block unavailable ({}); falling back to socket broadcast".format(e))
        if not submitted_via_rest:
            try:
                with open(m_peer_file) as f:
                    peer_dict = json.load(f)
                for k, v in peer_dict.items():
                    try:
                        s = socks.socksocket()
                        s.settimeout(0.3)
                        s.connect((k, int(v)))
                        connections.send(s, "block", 10)
                        connections.send(s, block_send, 10)
                        app_log.warning("Block submitted to {}".format(k))
                    except Exception as e:
                        app_log.warning("Could not submit block to {}: {}".format(k, e))
            except Exception as e:
                app_log.warning("Socket broadcast failed: {}".format(e))

    # record the share
    diff_shares = diff if diff < mdiff else mdiff
    saved = False
    duplicate = False
    shares = sqlite3.connect('shares.db')
    shares.text_factory = str
    s = shares.cursor()
    execute_param(s, ("SELECT nonce FROM nonces WHERE nonce = ?"), (nonce,))
    try:
        s.fetchone()[0]
        app_log.warning("Miner trying to reuse a share, ignored")
        duplicate = True
    except Exception:
        if real_diff >= diff_shares:
            execute_param(s, ("INSERT INTO nonces VALUES (?)"), (nonce,))
            commit(shares)
            timestamp = '%.2f' % time.time()
            s.execute("INSERT INTO shares VALUES (?,?,?,?,?,?,?,?)",
                      (str(miner_address), str(1), timestamp, "0", str(mrate), bname, str(wnum), wname))
            shares.commit()
            saved = True
        else:
            app_log.warning("Difficulty requirement not satisfied for anything")
    s.close()
    return {"accepted": saved, "block_found": block_found, "real_diff": real_diff,
            "duplicate": duplicate, "share_diff": diff_shares}


def _make_pool_handler():
    class PoolHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code, obj):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parts = [p for p in urlparse(self.path).path.split("/") if p]
            if parts == ["work"]:                 # was the socket 'getwork' command
                self._send(200, _build_work())
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            parts = [p for p in urlparse(self.path).path.split("/") if p]
            if parts != ["share"]:                # was the socket 'block' command (miner submitting a share)
                self._send(404, {"error": "not found"})
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                sh = json.loads(self.rfile.read(n) or b"{}") if n > 0 else {}
                self._send(200, process_share(str(sh.get("miner_address", "")), sh))
            except Exception as e:
                app_log.error("Share processing error: {}".format(e))
                self._send(500, {"error": str(e)})

    return PoolHandler


if __name__ == "__main__":

    mining.mining_open()

    try:
        background_thread = threading.Thread(target=paydb)
        background_thread.daemon = True
        background_thread.start()

        worker_thread = threading.Thread(target=worker, args=(w_time,))
        worker_thread.daemon = True
        worker_thread.start()
        app_log.warning("Starting up background tasks....")
        time.sleep(10)

        try:
            pool_port = int(sys.argv[1])
        except Exception as e:
            pool_port = 8525

        HOST, PORT = "0.0.0.0", pool_port

        # Bind + serve in the MAIN thread so Ctrl-C / SIGTERM shut the pool down cleanly. The old pattern
        # (serve_forever in a daemon thread + server_thread.join()) blocked the main thread in join(),
        # which swallowed Ctrl-C — the README "won't stop with Ctrl-C on Windows" known-issue.
        server = ThreadingHTTPServer((HOST, PORT), _make_pool_handler())
        server.daemon_threads = True
        ip, port = server.server_address
        app_log.warning("Pool server listening on {}:{}".format(ip, port))
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            app_log.warning("Ctrl-C received — shutting down pool...")
        finally:
            server.shutdown()
            server.server_close()
    finally:
        mining.mining_close()
