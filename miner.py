import base64, sqlite3, hashlib, time, socks, essentials, sys, connections, ast, re, options, getpass
from simplecrypt import *
from Cryptodome.Signature import PKCS1_v1_5
from Cryptodome.Hash import SHA
from Crypto import Random
from multiprocessing import Process, freeze_support

# load config
config = options.Get()
config.read()
debug_level = config.debug_level_conf
port = config.port
genesis_conf = config.genesis_conf
verify_conf = config.verify_conf
thread_limit_conf = config.thread_limit_conf
rebuild_db_conf = config.rebuild_db_conf
debug_conf = config.debug_conf
node_ip_conf = config.node_ip
purge_conf = config.purge_conf
pause_conf = config.pause_conf
ledger_path_conf = config.ledger_path_conf
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

if "testnet" in version:
    port = 2829
    peerlist = "peers_test.txt"
    ledger_path_conf = "static/test.db"
    print("Mining on testnet")
else:
    peerlist = "peers.txt"

# load config
def percentage(percent, whole):
    return int((percent * whole) / 100)

def nodes_block_submit(block_send):

    # connect to all nodes
    global peer_dict
    peer_dict = {}



    with open(peerlist) as f:
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
                print("Connected")

                print("Miner: Proceeding to submit mined block to node")

                connections.send(s_peer, "block", 10)
                connections.send(s_peer, block_send, 10)

                print("Miner: Block submitted to node {}".format(peer_ip))
            except Exception as e:
                print("Miner: Could not submit block to node {} because {}".format(peer_ip, e))
                pass

                # submit mined block to node


def check_uptodate(interval):
    # check if blocks are up to date
    while sync_conf == 1:
        conn = sqlite3.connect(ledger_path_conf)  # open to select the last tx to create a new hash from
        conn.text_factory = str
        c = conn.cursor()

        execute(c, ("SELECT timestamp FROM transactions WHERE reward != 0 ORDER BY block_height DESC LIMIT 1;"))
        timestamp_last_block = c.fetchone()[0]
        time_now = str(time.time())
        last_block_ago = float(time_now) - float(timestamp_last_block)

        if last_block_ago > interval:
            print("Local blockchain is {} minutes behind ({} seconds), waiting for sync to complete".format(int(last_block_ago) / 60, last_block_ago))
            time.sleep(5)
        else:
            break
        conn.close()
        # check if blocks are up to date


def bin_convert(string):
    return ''.join(format(ord(x), '8b').replace(' ', '0') for x in string)


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
            print("Retrying database execute due to {}".format(e))
            time.sleep(0.1)
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
            print("Retrying database execute due to {}".format(e))
            time.sleep(0.1)
            pass
            # secure execute for slow nodes
    return cursor


def miner(q, privatekey_readable, public_key_hashed, address):
    from Cryptodome.PublicKey import RSA
    Random.atfork()
    rndfile = Random.new()
    tries = 0
    firstrun = True
    begin = time.time()
    key = RSA.importKey(privatekey_readable)



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
        print("Connected")

        print("Miner: Asking pool for share qualification difficulty requirement")
        connections.send(s_pool, "diffp", 10)
        pool_diff_percentage = int(connections.receive(s_pool, 10))
        print("Miner: Received pool for share qualification difficulty requirement: {}%".format(pool_diff_percentage))
        s_pool.close()
        #ask for diff percentage

    while True:
        try:

            # calculate new hash
            nonces = 0
            # calculate difficulty
            s_node = socks.socksocket()
            if tor_conf == 1:
                s_node.setproxy(socks.PROXY_TYPE_SOCKS5, "127.0.0.1", 9050)
            s_node.connect((node_ip_conf, int(port)))  # connect to local node

            connections.send(s_node, "blocklast", 10)
            blocklast = connections.receive(s_node, 10)
            db_block_hash = blocklast[7]

            connections.send(s_node, "diffget", 10)
            diff = connections.receive(s_node, 10)
            s_node.close()

            diff = int(diff[1])

            diff_real = int(diff)

            if pool_conf == 0:
                diff = int(diff)


            else:  # if pooled
                diff_pool = diff_real
                diff = percentage(pool_diff_percentage, diff_real)

                if diff > diff_pool:
                    diff = diff_pool

            mining_condition = bin_convert(db_block_hash)[0:diff]


            # block_hash = hashlib.sha224(str(block_send) + db_block_hash).hexdigest()


            while tries < diff_recalc_conf:
                start = time.time()

                nonce = hashlib.sha224(rndfile.read(16)).hexdigest()[:32]
                mining_hash = bin_convert(hashlib.sha224((address + nonce + db_block_hash).encode("utf-8")).hexdigest())

                end = time.time()
                if tries % 2500 == 0: #limit output
                    try:
                        cycles_per_second = 1/(end - start)
                        print("Thread{} {} @ {:.2f} cycles/second, difficulty: {}({}), iteration: {}".format(q, db_block_hash[:10], cycles_per_second, diff, diff_real, tries))
                    except:
                        pass
                tries = tries + 1

                if mining_condition in mining_hash:
                    tries = 0

                    print("Thread {} found a good block hash in {} cycles".format(q, tries))

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
                    block_timestamp = '%.2f' % time.time()
                    transaction_reward = (str(block_timestamp), str(address[:56]), str(address[:56]), '%.8f' % float(0), "0", str(nonce))  # only this part is signed!
                    # print transaction_reward

                    h = SHA.new(str(transaction_reward).encode("utf-8"))
                    signer = PKCS1_v1_5.new(key)
                    signature = signer.sign(h)
                    signature_enc = base64.b64encode(signature)

                    if signer.verify(h, signature):
                        print("Signature valid")

                        block_send.append((str(block_timestamp), str(address[:56]), str(address[:56]), '%.8f' % float(0), str(signature_enc.decode("utf-8")), str(public_key_hashed.decode("utf-8")), "0", str(nonce)))  # mining reward tx
                        print("Block to send: {}".format(block_send))

                        if not any(isinstance(el, list) for el in block_send):  # if it's not a list of lists (only the mining tx and no others)
                            new_list = []
                            new_list.append(block_send)
                            block_send = new_list  # make it a list of lists

                        #  claim reward
                        # include data

                        tries = 0

                        # submit mined block to node

                        if sync_conf == 1:
                            check_uptodate(300)

                        if pool_conf == 1:
                            mining_condition = bin_convert(db_block_hash)[0:diff_real]
                            if mining_condition in mining_hash:
                                print("Miner: Submitting block to all nodes, because it satisfies real difficulty too")
                                nodes_block_submit(block_send)

                            try:
                                s_pool = socks.socksocket()
                                s_pool.settimeout(0.3)
                                if tor_conf == 1:
                                    s_pool.setproxy(socks.PROXY_TYPE_SOCKS5, "127.0.0.1", 9050)
                                s_pool.connect((pool_ip_conf, 8525))  # connect to pool
                                print("Connected")

                                print("Miner: Proceeding to submit mined block to pool")

                                connections.send(s_pool, "block", 10)
                                connections.send(s_pool, self_address, 10)
                                connections.send(s_pool, block_send, 10)
                                s_pool.close()

                                print("Miner: Block submitted to pool")

                            except Exception as e:
                                print("Miner: Could not submit block to pool")
                                pass

                        if pool_conf == 0:
                            nodes_block_submit(block_send)
                    else:
                        print("Invalid signature")
            tries = 0

        except Exception as e:
            print(e)
            time.sleep(0.1)
            if debug_conf == 1:
                raise
            else:
                pass


if __name__ == '__main__':
    freeze_support()  # must be this line, dont move ahead

    key, public_key_readable, private_key_readable, encrypted, unlocked, public_key_hashed, address = essentials.keys_load_new ("wallet.der")
    if not unlocked:
        key, private_key_readable = essentials.keys_unlock(private_key_readable)


    connected = 0
    while connected == 0:
        try:
            s_node = socks.socksocket()
            if tor_conf == 1:
                s_node.setproxy(socks.PROXY_TYPE_SOCKS5, "127.0.0.1", 9050)
            s_node.connect((node_ip_conf, int(port)))
            print("Connected")
            connected = 1
            s_node.close()
        except Exception as e:
            print(e)
            print("Miner: Please start your node for the block to be submitted or adjust mining ip in settings.")
            time.sleep(1)
    # verify connection
    if sync_conf == 1:
        check_uptodate(120)

    instances = range(int(mining_threads_conf))
    print(instances)
    for q in instances:
        p = Process(target=miner, args=(str(q + 1), private_key_readable, public_key_hashed, address))
        # p.daemon = True
        p.start()
        print("thread " + str(p) + " started")
