import base64, sqlite3, hashlib, time, socks, keys, log, sys, connections, ast, re, options
from Crypto.Signature import PKCS1_v1_5
from Crypto.Hash import SHA
from Crypto import Random
from multiprocessing import Process, freeze_support

try:
    import quickbismuth
except ImportError:
    quickbismuth = None

# load config
(port, genesis_conf, verify_conf, version_conf, thread_limit_conf, rebuild_db_conf, debug_conf, purge_conf, pause_conf, ledger_path_conf, hyperblocks_conf, warning_list_limit_conf, tor_conf, debug_level_conf, allowed, mining_ip_conf, sync_conf, mining_threads_conf, diff_recalc_conf, pool_conf, pool_address, ram_conf) = options.read()
# load config

def check_uptodate(interval, app_log):
    # check if blocks are up to date
    while sync_conf == 1:
        conn = sqlite3.connect("static/ledger.db")  # open to select the last tx to create a new hash from
        conn.text_factory = str
        c = conn.cursor()

        execute(c, ("SELECT timestamp FROM transactions WHERE reward != 0 ORDER BY block_height DESC LIMIT 1;"), app_log)
        timestamp_last_block = c.fetchone()[0]
        time_now = str(time.time())
        last_block_ago = float(time_now) - float(timestamp_last_block)

        if last_block_ago > interval:
            app_log.warning("Local blockchain is {} minutes behind ({} seconds), waiting for sync to complete".format(int(last_block_ago) / 60,last_block_ago))
            time.sleep(5)
        else:
            break
        conn.close()
    # check if blocks are up to date

def send(sdef, data):
    sdef.sendall(data)

def bin_convert(string):
    return ''.join(format(ord(x), 'b') for x in string)

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

def miner(q,privatekey_readable, public_key_hashed, address):
    from Crypto.PublicKey import RSA
    Random.atfork()
    key = RSA.importKey(privatekey_readable)
    app_log = log.log("miner_"+q+".log",debug_level_conf)
    rndfile = Random.new()
    tries = 0
    firstrun = True
    begin = time.time()

    if pool_conf == 1:
        self_address = address
        address = pool_address

    if quickbismuth:
        app_log.warning('Using QuickBismuth: ' + quickbismuth.__version__)

    while True:
        try:

            # calculate new hash

            if tries % int(diff_recalc_conf) == 0 or firstrun: #only do this ever so often
                firstrun = False
                now = time.time()
                block_timestamp = '%.2f' % time.time()

                # calculate difficulty
                s = socks.socksocket()
                if tor_conf == 1:
                    s.setproxy(socks.PROXY_TYPE_SOCKS5, "127.0.0.1", 9050)
                s.connect((mining_ip_conf, int(port)))  # connect to local node

                connections.send(s, "blocklast", 10)
                db_block_hash = ast.literal_eval(connections.receive(s, 10))[7]

                cycles_per_second = tries / (now - begin) if (now - begin) != 0 else 0
                begin = now
                tries = 0

                if pool_conf == 0:
                    connections.send(s, "diffget", 10)
                    diff = float(connections.receive(s, 10))
                    diff = int(diff)

                else: #if pooled
                    diff = 37

                app_log.warning("Thread{} {} @ {:.2f} cycles/second, difficulty: {:.2f}".format(q, db_block_hash[:10], cycles_per_second, diff))

            nonce = hashlib.sha224(rndfile.read(16)).hexdigest()[:32]

            if quickbismuth:
                fastminer_cycles = 500000
                nonce = quickbismuth.bismuth_mine(diff, address, db_block_hash, fastminer_cycles, rndfile.read(32))
                tries += fastminer_cycles
            else:
                tries = tries +1

            if nonce is None:
                nonce = hashlib.sha224(rndfile.read(16)).hexdigest()[:32]

            #block_hash = hashlib.sha224(str(block_send) + db_block_hash).hexdigest()
            mining_hash = bin_convert(hashlib.sha224((address + nonce + db_block_hash).encode("utf-8")).hexdigest())
            mining_condition = bin_convert(db_block_hash)[0:diff]

            if mining_condition in mining_hash:
                app_log.warning("Thread {} found a good block hash in {} cycles".format(q,tries))

                # serialize txs

                block_send = []
                del block_send[:]  # empty
                removal_signature = []
                del removal_signature[:]  # empty

                s = socks.socksocket()
                if tor_conf == 1:
                    s.setproxy(socks.PROXY_TYPE_SOCKS5, "127.0.0.1", 9050)
                s.connect((mining_ip_conf, int(port)))  # connect to config.txt node
                connections.send(s, "mpget", 10)
                data = connections.receive(s, 10)

                if data != "[]":
                    mempool = ast.literal_eval(data)

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

                block_send.append((str(block_timestamp), str(address[:56]), str(address[:56]), '%.8f' % float(0), str(signature_enc.decode("utf-8")), str(public_key_hashed), "0", str(nonce)))  # mining reward tx
                app_log.warning("Block to send: {}".format(block_send))
                #  claim reward
                # include data

                tries = 0

                #submit mined block to node

                if sync_conf == 1:
                    check_uptodate(300, app_log)

                if pool_conf == 1:
                    s = socks.socksocket()
                    s.settimeout(0.3)
                    if tor_conf == 1:
                        s.setproxy(socks.PROXY_TYPE_SOCKS5, "127.0.0.1", 9050)
                    s.connect((mining_ip_conf, 8525))  # connect to pool
                    app_log.warning("Connected")

                    app_log.warning("Miner: Proceeding to submit mined block to pool")

                    connections.send(s, "block", 10)
                    connections.send(s, self_address, 10)
                    connections.send(s, block_send, 10)

                    app_log.warning("Miner: Block submitted to pool")

                else:

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
                                s = socks.socksocket()
                                s.settimeout(0.3)
                                if tor_conf == 1:
                                    s.setproxy(socks.PROXY_TYPE_SOCKS5, "127.0.0.1", 9050)
                                s.connect((peer_ip, int(peer_port)))  # connect to node in peerlist
                                app_log.warning("Connected")

                                app_log.warning("Miner: Proceeding to submit mined block")

                                connections.send(s, "block", 10)
                                connections.send(s, block_send, 10)

                                app_log.warning("Miner: Block submitted to {}".format(peer_ip))
                            except Exception as e:
                                app_log.warning("Miner: Could not submit block to {} because {}".format(peer_ip,e))
                                pass

                #submit mined block to node

                    #break
        except Exception as e:
            print (e)
            time.sleep(0.1)
            raise

if __name__ == '__main__':
    freeze_support()  # must be this line, dont move ahead

    app_log = log.log("miner.log",debug_level_conf)

    (key, private_key_readable, public_key_readable, public_key_hashed, address) = keys.read()
    #print(private_key_readable.encode("utf-8"))

    #print 'Number of arguments:', len(sys.argv), 'arguments.'
    #print 'Argument List:', str(sys.argv)

    #try:
    #    address = sys.argv[1]
    #except:
    #    address = address

    # verify connection

    connected = 0
    while connected == 0:
        try:
            s = socks.socksocket()
            if tor_conf == 1:
                s.setproxy(socks.PROXY_TYPE_SOCKS5, "127.0.0.1", 9050)
            s.connect((mining_ip_conf, int(port)))
            app_log.warning("Connected")
            connected = 1
            s.close()
        except Exception as e:
            print (e)
            app_log.warning(
                "Miner: Please start your node for the block to be submitted or adjust mining ip in settings.")
            time.sleep(1)
    # verify connection
    if sync_conf == 1:
        check_uptodate(120, app_log)

    instances = range(int(mining_threads_conf))
    print (instances)
    for q in instances:

        p = Process(target=miner,args=(str(q+1),private_key_readable, public_key_hashed, address))
        #p.daemon = True
        p.start()
        print ("thread "+str(p)+ " started")
