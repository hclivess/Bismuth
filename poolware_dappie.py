import socketserver, connections, time, options, log, sqlite3, ast, socks, hashlib, os, random, re, keys, base64
from Crypto.Signature import PKCS1_v1_5
from Crypto.Hash import SHA

config = options.Get()
config.read()
debug_level = config.debug_level_conf
port = config.port
genesis_conf = config.genesis_conf
verify_conf = config.verify_conf
thread_limit_conf = config.thread_limit_conf
rebuild_db_conf = config.rebuild_db_conf
debug_conf = config.debug_conf
node_ip_conf = config.node_ip_conf
purge_conf = config.purge_conf
pause_conf = config.pause_conf
ledger_path_conf = config.ledger_path_conf
hyperblocks_conf = config.hyperblocks_conf
ban_threshold = config.ban_threshold
tor_conf = config.tor_conf
debug_level_conf = config.debug_level_conf
allowed = config.allowed_conf
pool_ip_conf = config.pool_ip_conf
sync_conf = config.sync_conf
pool_percentage_conf = config.pool_percentage_conf
mining_threads_conf = config.mining_threads_conf
diff_recalc_conf = config.diff_recalc_conf
pool_conf = config.pool_conf
ram_conf = config.ram_conf
pool_address = config.pool_address_conf
version = config.version_conf
terminal_output=config.terminal_output


(key, private_key_readable, public_key_readable, public_key_hashed, address) = keys.read() #import keys
app_log = log.log("pool.log",debug_level_conf,terminal_output)

def percentage(percent, whole):
    return int((percent * whole) / 100)

def payout():
    shares = sqlite3.connect('shares.db')
    shares.text_factory = str
    s = shares.cursor()

    conn = sqlite3.connect('static/ledger.db')
    conn.text_factory = str
    c = conn.cursor()

    #get unique addresses
    addresses = []
    for row in s.execute("SELECT * FROM shares"):
        shares_address = row[0]
        shares_value = row[1]
        shares_timestamp = row[2]

        if shares_address not in addresses:
            addresses.append(shares_address)
    app_log.warning("Addresses: ".format(address))
    #get unique addresses


    # get shares for address
    output_shares = []
    output_timestamps = []

    for x in addresses:
        # get mined block threshold
        s.execute("SELECT timestamp FROM shares WHERE address = ? ORDER BY timestamp ASC LIMIT 1", (x,))
        shares_timestamp = s.fetchone()[0]
        output_timestamps.append(float(shares_timestamp))
        # get mined block threshold

        s.execute("SELECT sum(shares) FROM shares WHERE address = ? AND paid != 1", (x,))
        shares_sum = s.fetchone()[0]

        if shares_sum == None:
            shares_sum = 0

        output_shares.append(shares_sum)

    app_log.warning("Output shares: {}".format(output_shares))
    # get shares for address

    try:
        block_threshold = min(output_timestamps)
    except:
        block_threshold = time.time()
    app_log.warning("Payout block threshold: {}".format(block_threshold))

    #get eligible blocks
    reward_list = []
    for row in c.execute("SELECT * FROM transactions WHERE address = ? AND CAST(timestamp AS INTEGER) >= ? AND reward != 0", (address,) + (block_threshold,)):
        reward_list.append(float(row[9]))

    reward_total = sum(reward_list)
    #get eligible blocks

    shares_total = sum(output_shares)

    try:
        reward_per_share = reward_total / shares_total
    except:
        reward_per_share = 0

    # calculate payouts
    payout_threshold = 1
    payout_passed = 0
    for recipient, y in zip(addresses, output_shares):
        app_log.warning("Potential share recipient: {}".format(recipient))
        try:
            claim = float('%.8f' % (y * reward_per_share))
        except:
            claim = 0
        app_log.warning("Potential token claim: {}".format(claim))

        if claim >= payout_threshold:
            payout_passed = 1
            openfield = "pool"
            operation = 0
            fee = float('%.8f' % float(0.01 + (float(claim) * 0.001) + (float(len(openfield)) / 100000) + (float(operation) / 10)))  # 0.1% + 0.01 dust
            #make payout

            timestamp = '%.2f' % time.time()
            transaction = (str(timestamp), str(address), str(recipient), '%.8f' % float(claim - fee), str(operation), str(openfield))  # this is signed
            # print transaction

            h = SHA.new(str(transaction).encode("utf-8"))
            signer = PKCS1_v1_5.new(key)
            signature = signer.sign(h)
            signature_enc = base64.b64encode(signature)
            app_log.warning("Encoded Signature: {}".format(signature_enc.decode("utf-8")))

            verifier = PKCS1_v1_5.new(key)
            if verifier.verify(h, signature):
                print("The signature is valid, proceeding to save transaction to mempool")

                mempool = sqlite3.connect('mempool.db')
                mempool.text_factory = str
                m = mempool.cursor()

                m.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?)", (str(timestamp), str(address), str(recipient), '%.8f' % float(claim - fee), str(signature_enc.decode("utf-8")), str(public_key_hashed), str(operation), str(openfield)))
                mempool.commit()  # Save (commit) the changes
                mempool.close()
                print("Mempool updated with a received transaction")

            s.execute("UPDATE shares SET paid = 1 WHERE address = ?",(recipient,))
            shares.commit()

    if payout_passed == 1:
        s.execute("UPDATE shares SET timestamp = ?", (time.time(),))
        shares.commit()

    # calculate payouts
    #payout
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

def diffget(s):
    connections.send(s, "diffget", 10)
    diff = float(connections.receive(s, 10)[1])
    return diff

def bin_convert(string):
    return ''.join(format(ord(x), '8b').replace(' ', '0') for x in string)

if not os.path.exists('shares.db'):
    # create empty mempool
    shares = sqlite3.connect('shares.db')
    shares.text_factory = str
    s = shares.cursor()
    execute(s, "CREATE TABLE IF NOT EXISTS shares (address, shares, timestamp, paid)")
    execute(s, "CREATE TABLE IF NOT EXISTS nonces (nonce)") #for used hash storage
    app_log.warning("Created shares file")
    s.close()
    # create empty mempool

payout()

diff_percent_number = pool_percentage_conf
app_log.warning("Pool difficulty configured at {}%".format(pool_percentage_conf))

class MyTCPHandler(socketserver.BaseRequestHandler):

    def handle(self):
        peer_ip = self.request.getpeername()[0]

        data = connections.receive(self.request, 10)
        app_log.warning("Received: {} from {}".format(data, peer_ip))  # will add custom ports later

        if data == 'diffp':
            app_log.warning("Sending the share qualification difficulty requirement: {}%".format(diff_percent_number))
            connections.send(self.request, diff_percent_number, 10)

        if data == "block":  # from miner to node

            # sock
            s1 = socks.socksocket()
            if tor_conf == 1:
                s1.setproxy(socks.PROXY_TYPE_SOCKS5, "127.0.0.1", 9050)
            s1.connect((node_ip_conf, int(port)))  # connect to local node,
            # sock


            # receive block
            miner_address = connections.receive(self.request, 10)
            app_log.warning("Received a block from miner {} ({})".format(peer_ip,miner_address))

            block_send = connections.receive(self.request, 10)
            nonce = (block_send[-1][7])

            app_log.warning("Combined mined segments: {}".format(block_send))

            #print(nonce)
            #print(block_send)
            #print(miner_address)

            # check difficulty
            app_log.warning("Asking node for difficulty")
            diff = int(diffget(s1))
            app_log.warning("Calculated difficulty: {}".format(diff))
            # check difficulty

            app_log.warning("Asking node for last block")

            # get last block
            connections.send(s1, "blocklast", 10)
            blocklast = connections.receive(s1, 10)
            db_block_hash = blocklast[7]
            # get last block

            app_log.warning("Last Hash: {}".format(db_block_hash))

            mining_hash = bin_convert(hashlib.sha224((address + nonce + db_block_hash).encode("utf-8")).hexdigest())
            mining_condition = bin_convert(db_block_hash)[0:diff]

            if mining_condition in mining_hash:
                app_log.warning("Difficulty requirement satisfied for mining")
                app_log.warning("Sending block to node {}".format(peer_ip))

                global peer_dict
                peer_dict = {}
                with open("peers.txt") as f:
                    for line in f:
                        line = re.sub("[\)\(\:\\n\'\s]", "", line)
                        peer_dict[line.split(",")[0]] = line.split(",")[1]

                    for k, v in peer_dict.items():
                        peer_ip = k
                        # app_log.info(HOST)
                        peer_port = int(v)
                        # app_log.info(PORT)
                        # connect to all nodes

                        try:
                            s = socks.socksocket()
                            s.settimeout(0.3)
                            if tor_conf == 1:
                                s.setproxy(socks.PROXY_TYPE_SOCKS5, "127.0.0.1", 9050)
                            s.connect((peer_ip, int(peer_port)))  # connect to node in peerlist
                            app_log.warning("Connected")

                            app_log.warning("Pool: Proceeding to submit mined block")

                            connections.send(s, "block", 10)
                            connections.send(s, block_send, 10)

                            app_log.warning("Pool: Block submitted to {}".format(peer_ip))
                        except Exception as e:
                            app_log.warning("Pool: Could not submit block to {} because {}".format(peer_ip, e))
                            pass

            diff_percentage = percentage(diff_percent_number, diff)

            app_log.warning("Pool: Current difficulty: Pool: {} Real: {}".format(diff_percentage,diff))

            if diff < diff_percentage:
                diff_shares = diff
            else:
                diff_shares = diff_percentage

            shares = sqlite3.connect('shares.db')
            shares.text_factory = str
            s = shares.cursor()

            # protect against used share resubmission
            execute_param(s, ("SELECT nonce FROM nonces WHERE nonce = ?"), (nonce,))

            try:
                result = s.fetchone()[0]
                app_log.warning("Miner trying to reuse a share, ignored")
            except:
                # protect against used share resubmission
                mining_condition = bin_convert(db_block_hash)[0:diff_shares] #floor set by pool
                if mining_condition in mining_hash:
                    app_log.warning("Difficulty requirement satisfied for saving shares")

                    execute_param(s, ("INSERT INTO nonces VALUES (?)"), (nonce,))
                    commit(shares)

                    timestamp = '%.2f' % time.time()

                    s.execute("INSERT INTO shares VALUES (?,?,?,?)", (str(miner_address), str(1), timestamp, "0"))
                    shares.commit()

                else:
                    app_log.warning("Difficulty requirement not satisfied for anything")

            s.close()
            s1.close()

app_log.warning("Starting up...")

if __name__ == "__main__":
    HOST, PORT = "0.0.0.0", 8525

    # Create the server, binding to localhost on port 9999
    server = socketserver.TCPServer((HOST, PORT), MyTCPHandler)

    # Activate the server; this will keep running until you
    # interrupt the program with Ctrl-C
    server.serve_forever()