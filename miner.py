import base64, sqlite3, hashlib, time, socks, keys, log, sys, connections, ast, re, options
from Crypto.Signature import PKCS1_v1_5
from Crypto.Hash import SHA
from Crypto import Random
from multiprocessing import Process, freeze_support

# load config
(port, genesis_conf, verify_conf, version_conf, thread_limit_conf, rebuild_db_conf, debug_conf, purge_conf, pause_conf, ledger_path_conf, hyperblocks_conf, warning_list_limit_conf, tor_conf, debug_level_conf, allowed, pool_ip_conf, sync_conf, mining_threads_conf, diff_recalc_conf, pool_conf, pool_address, ram_conf, pool_percentage_conf, node_ip_conf) = options.read()

# load config
def percentage(percent, whole):
    return int((percent * whole) / 100)

def nodes_block_submit(block_send, app_log):
    # connect to all nodes
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
                s_peer = socks.socksocket()
                s_peer.settimeout(0.3)
                if tor_conf == 1:
                    s_peer.setproxy(socks.PROXY_TYPE_SOCKS5, "127.0.0.1", 9050)
                s_peer.connect((peer_ip, int(peer_port)))  # connect to node in peerlist
                app_log.warning("Connected")

                app_log.warning("Miner: Proceeding to submit mined block to node")

                connections.send(s_peer, "block", 10)
                connections.send(s_peer, block_send, 10)

                app_log.warning("Miner: Block submitted to node {}".format(peer_ip))
            except Exception as e:
                app_log.warning("Miner: Could not submit block to node {} because {}".format(peer_ip, e))
                pass

                # submit mined block to node


def check_uptodate(interval, app_log):
    # check if blocks are up to date
    while sync_conf == 1:
        conn = sqlite3.connect(ledger_path_conf)  # open to select the last tx to create a new hash from
        conn.text_factory = str
        c = conn.cursor()

        execute(c, ("SELECT timestamp FROM transactions WHERE reward != 0 ORDER BY block_height DESC LIMIT 1;"), app_log)
        timestamp_last_block = c.fetchone()[0]
        time_now = str(time.time())
        last_block_ago = float(time_now) - float(timestamp_last_block)

        if last_block_ago > interval:
            app_log.warning("Local blockchain is {} minutes behind ({} seconds), waiting for sync to complete".format(int(last_block_ago) / 60, last_block_ago))
            time.sleep(5)
        else:
            break
        conn.close()
        # check if blocks are up to date


def bin_convert(string):
    return ''.join(format(ord(x), '8b').replace(' ', '0') for x in string)


def execute(cursor, what, app_log):
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
            time.sleep(0.1)
            pass
            # secure execute for slow nodes
    return cursor


def execute_param(cursor, what, param, app_log):
    # secure execute for slow nodes
    passed = 0
    while passed == 0:
        try:
            # print cursor
            # print what
            cursor.execute(what, param)
            passed = 1
        except Exception as e:
            app_log.warning("Retrying database execute due to {}".format(e))
            time.sleep(0.1)
            pass
            # secure execute for slow nodes
    return cursor


def miner(q, privatekey_readable, public_key_hashed, address):
    from Crypto.PublicKey import RSA
    Random.atfork()
    key = RSA.importKey(privatekey_readable)
    app_log = log.log("miner_" + q + ".log", debug_level_conf)
    rndfile = Random.new()
    tries = 0
    firstrun = True
    begin = time.time()

    if pool_conf == 1:
        #do not use pools public key to sign, signature will be invalid

        self_address = address
        address = pool_address

        #ask for diff percentage
        s_pool = socks.socksocket()
        s_pool.settimeout(0.3)
        if tor_conf == 1:
            s_pool.setproxy(socks.PROXY_TYPE_SOCKS5, "127.0.0.1", 9050)
        s_pool.connect((pool_ip_conf, 8525))  # connect to pool
        app_log.warning("Connected")

        app_log.warning("Miner: Asking pool for the difficulty percentage requirement")
        connections.send(s_pool, "diffp", 100)
        pool_diff_percentage = int(connections.receive(s_pool, 100))
        s_pool.close()
        #ask for diff percentage

    while True:
        try:

            # calculate new hash

            if tries % int(diff_recalc_conf) == 0 or firstrun:  # only do this ever so often
                firstrun = False
                now = time.time()
                block_timestamp = '%.2f' % time.time()

                # calculate difficulty
                s_node = socks.socksocket()
                if tor_conf == 1:
                    s_node.setproxy(socks.PROXY_TYPE_SOCKS5, "127.0.0.1", 9050)
                s_node.connect((node_ip_conf, int(port)))  # connect to local node

                connections.send(s_node, "blocklast", 10)
                db_block_hash = connections.receive(s_node, 10)[7]

                cycles_per_second = tries / (now - begin) if (now - begin) != 0 else 0
                begin = now
                tries = 0

                connections.send(s_node, "diffget", 10)
                diff = float(connections.receive(s_node, 10))
                s_node.close()

                diff = int(diff)
                diff_real = int(diff)

                if pool_conf == 0:
                    diff = int(diff)


                else:  # if pooled
                    diff_pool = diff_real
                    diff = percentage(pool_diff_percentage, diff_real)

                    if diff > diff_pool:
                        diff = diff_pool

                    if diff < 37:
                        diff = 37

                app_log.warning("Thread{} {} @ {:.2f} cycles/second, difficulty: {}({})".format(q, db_block_hash[:10], cycles_per_second, diff, diff_real))

            nonce = hashlib.sha224(rndfile.read(16)).hexdigest()[:32]

            tries = tries + 1

            if nonce is None:
                nonce = hashlib.sha224(rndfile.read(16)).hexdigest()[:32]

            # block_hash = hashlib.sha224(str(block_send) + db_block_hash).hexdigest()
            mining_hash = bin_convert(hashlib.sha224((address + nonce + db_block_hash).encode("utf-8")).hexdigest())
            mining_condition = bin_convert(db_block_hash)[0:diff]

            if mining_condition in mining_hash:
                app_log.warning("Thread {} found a good block hash in {} cycles".format(q, tries))

                # serialize txs

                block_send = []
                del block_send[:]  # empty
                removal_signature = []
                del removal_signature[:]  # empty

                s_node = socks.socksocket()
                if tor_conf == 1:
                    s_node.setproxy(socks.PROXY_TYPE_SOCKS5, "127.0.0.1", 9050)
                s_node.connect((node_ip_conf, int(port)))  # connect to config.txt node
                connections.send(s_node, "mpget", 10)
                data = connections.receive(s_node, 10)
                s_node.close()

                if data != "[]":
                    mempool = data

                    for mpdata in mempool:
                        transaction = (
                            str(mpdata[0]), str(mpdata[1][:56]), str(mpdata[2][:56]), '%.8f' % float(mpdata[3]), str(mpdata[4]), str(mpdata[5]), str(mpdata[6]),
                            str(mpdata[7]))  # create tuple
                        # print transaction
                        block_send.append(transaction)  # append tuple to list for each run
                        removal_signature.append(str(mpdata[4]))  # for removal after successful mining

                # claim reward
                transaction_reward = (str(block_timestamp), str(address[:56]), str(address[:56]), '%.8f' % float(0), "0", str(nonce))  # only this part is signed!
                # print transaction_reward

                h = SHA.new(str(transaction_reward).encode("utf-8"))
                signer = PKCS1_v1_5.new(key)
                signature = signer.sign(h)
                signature_enc = base64.b64encode(signature)

                if signer.verify(h, signature) == True:
                    app_log.warning("Signature valid")

                    block_send.append((str(block_timestamp), str(address[:56]), str(address[:56]), '%.8f' % float(0), str(signature_enc.decode("utf-8")), str(public_key_hashed), "0", str(nonce)))  # mining reward tx
                    app_log.warning("Block to send: {}".format(block_send))
                    #  claim reward
                    # include data

                    tries = 0

                    # submit mined block to node

                    if sync_conf == 1:
                        check_uptodate(300, app_log)

                    if pool_conf == 1:
                        mining_condition = bin_convert(db_block_hash)[0:diff_real]
                        if mining_condition in mining_hash:
                            app_log.warning("Miner: Submitting block to all nodes, because it satisfies real difficulty too")
                            nodes_block_submit(block_send, app_log)

                        try:
                            s_pool = socks.socksocket()
                            s_pool.settimeout(0.3)
                            if tor_conf == 1:
                                s_pool.setproxy(socks.PROXY_TYPE_SOCKS5, "127.0.0.1", 9050)
                            s_pool.connect((pool_ip_conf, 8525))  # connect to pool
                            app_log.warning("Connected")

                            app_log.warning("Miner: Proceeding to submit mined block to pool")

                            connections.send(s_pool, "block", 10)
                            connections.send(s_pool, self_address, 10)
                            connections.send(s_pool, block_send, 10)
                            s_pool.close()

                            app_log.warning("Miner: Block submitted to pool")

                        except Exception as e:
                            app_log.warning("Miner: Could not submit block to pool")
                            pass

                    if pool_conf == 0:
                        nodes_block_submit(block_send, app_log)
                else:
                    app_log.warning("Invalid signature")


        except Exception as e:
            print(e)
            time.sleep(0.1)
            if debug_conf == 1:
                raise
            else:
                pass


if __name__ == '__main__':
    freeze_support()  # must be this line, dont move ahead

    app_log = log.log("miner.log", debug_level_conf)

    (key, private_key_readable, public_key_readable, public_key_hashed, address) = keys.read()

    connected = 0
    while connected == 0:
        try:
            s_node = socks.socksocket()
            if tor_conf == 1:
                s_node.setproxy(socks.PROXY_TYPE_SOCKS5, "127.0.0.1", 9050)
            s_node.connect((node_ip_conf, int(port)))
            app_log.warning("Connected")
            connected = 1
            s_node.close()
        except Exception as e:
            print(e)
            app_log.warning("Miner: Please start your node for the block to be submitted or adjust mining ip in settings.")
            time.sleep(1)
    # verify connection
    if sync_conf == 1:
        check_uptodate(120, app_log)

    instances = range(int(mining_threads_conf))
    print(instances)
    for q in instances:
        p = Process(target=miner, args=(str(q + 1), private_key_readable, public_key_hashed, address))
        # p.daemon = True
        p.start()
        print("thread " + str(p) + " started")