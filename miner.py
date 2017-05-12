import base64, sqlite3, os, hashlib, time, socks, keys, log
from Crypto.Signature import PKCS1_v1_5
from Crypto.Hash import SHA
from Crypto import Random
from multiprocessing import Process, freeze_support

try:
    from fastminer import fastminer
except ImportError:
    fastminer = None

# load config
lines = [line.rstrip('\n') for line in open('config.txt')]
for line in lines:
    if "port=" in line:
        port = line.strip('port=')
    if "mining_ip=" in line:
        mining_ip_conf = line.strip("mining_ip=")
    if "mining_threads=" in line:
        mining_threads_conf = line.strip('mining_threads=')
    if "diff_recalc=" in line:
        diff_recalc_conf = line.strip('diff_recalc=')
    if "tor=" in line:
        tor_conf = int(line.strip('tor='))
    if "miner_sync=" in line:
        sync_conf = int(line.strip('miner_sync='))
    if "debug_level=" in line:
        debug_level_conf = line.strip('debug_level=')
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
        except Exception, e:
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
        except Exception, e:
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

    if fastminer:
        app_log.warning('Using FastBismuth miner!')

    while True:
        try:

            # calculate new hash

            if tries % int(diff_recalc_conf) == 0 or firstrun: #only do this ever so often
                firstrun = False
                now = time.time()
                block_timestamp = '%.2f' % time.time()
                conn = sqlite3.connect("static/ledger.db") #open to select the last tx to create a new hash from
                conn.text_factory = str
                c = conn.cursor()
                execute(c ,("SELECT block_hash, timestamp FROM transactions WHERE reward != 0 ORDER BY block_height DESC LIMIT 1;"), app_log)
                result = c.fetchall()
                db_block_hash = result[0][0]
                timestamp_last_block = float(result[0][1])

                # calculate difficulty
                execute_param(c, ("SELECT block_height FROM transactions WHERE CAST(timestamp AS INTEGER) > ? AND reward != 0"), (timestamp_last_block - 1800,), app_log)  # 1800=30 min
                blocks_per_30 = len(c.fetchall())

                diff = blocks_per_30 * 2

                # drop diff per minute if over target
                time_drop = time.time()

                drop_factor = 120  # drop 0,5 diff per minute

                if time_drop > timestamp_last_block + 120:  # start dropping after 2 minutes
                    diff = diff - (time_drop - timestamp_last_block) / drop_factor  # drop 0,5 diff per minute (1 per 2 minutes)

                if time_drop > timestamp_last_block + 300 or diff < 37:  # 5 m lim
                    diff = 37  # 5 m lim
                        # drop diff per minute if over target
                cycles_per_second = tries / (now - begin)
                begin = now
                tries = 0
                app_log.warning("Mining, {:.2f} cycles/second {}, difficulty: {}, {:.2f} blocks per minute".format(cycles_per_second, q, diff, blocks_per_30/30.0))

            diff = int(diff)

            nonce = hashlib.sha224(rndfile.read(16)).hexdigest()[:32]

            if fastminer:
                fastminer_cycles = 500000
                nonce = fastminer(diff, address, db_block_hash, fastminer_cycles, rndfile.read(32))
                tries += fastminer_cycles
            else:
                tries = tries +1

            if nonce is None:
                nonce = hashlib.sha224(rndfile.read(16)).hexdigest()[:32]

            #block_hash = hashlib.sha224(str(block_send) + db_block_hash).hexdigest()
            mining_hash = bin_convert(hashlib.sha224(address + nonce + db_block_hash).hexdigest())
            mining_condition = bin_convert(db_block_hash)[0:diff]

            if mining_condition in mining_hash:
                app_log.warning("Thread {} found a good block hash in {} cycles".format(q,tries))

                # serialize txs
                mempool = sqlite3.connect("mempool.db")
                mempool.text_factory = str
                m = mempool.cursor()
                execute(m, ("SELECT * FROM transactions ORDER BY timestamp;"), app_log)
                result = m.fetchall()  # select all txs from mempool
                mempool.close()

                #include data
                block_send = []
                del block_send[:]  # empty
                removal_signature = []
                del removal_signature[:]  # empty

                for dbdata in result:
                    transaction = (
                        str(dbdata[0]), str(dbdata[1][:56]), str(dbdata[2][:56]), '%.8f' % float(dbdata[3]), str(dbdata[4]), str(dbdata[5]), str(dbdata[6]),
                        str(dbdata[7]))  # create tuple
                    # print transaction
                    block_send.append(transaction)  # append tuple to list for each run
                    removal_signature.append(str(dbdata[4]))  # for removal after successful mining

                # claim reward
                transaction_reward = tuple
                transaction_reward = (str(block_timestamp), str(address[:56]), str(address[:56]), '%.8f' % float(0), "0", str(nonce))  # only this part is signed!
                # print transaction_reward

                h = SHA.new(str(transaction_reward))
                signer = PKCS1_v1_5.new(key)
                signature = signer.sign(h)
                signature_enc = base64.b64encode(signature)

                block_send.append((str(block_timestamp), str(address[:56]), str(address[:56]), '%.8f' % float(0), str(signature_enc),
                                   str(public_key_hashed), "0", str(nonce)))  # mining reward tx
                # claim reward
                # include data

                tries = 0

                #submit mined block to node

                if sync_conf == 1:
                    check_uptodate(300, app_log)

                submitted = 0
                while submitted == 0:
                    try:
                        s = socks.socksocket()
                        s.connect((mining_ip_conf, int(port)))  # connect to local node
                        app_log.warning("Connected")

                        app_log.warning("Miner: Proceeding to submit mined block")

                        send(s, (str(len("block"))).zfill(10))
                        send(s, "block")
                        send(s, (str(len(str(block_send)))).zfill(10))
                        send(s, str(block_send))


                        submitted = 1
                        app_log.warning("Miner: Block submitted")

                    except Exception, e:
                        print e
                        app_log.warning("Miner: Please start your node for the block to be submitted or adjust mining ip in settings.")
                        time.sleep(1)

                #remove sent from mempool

                mempool = sqlite3.connect("mempool.db")
                mempool.text_factory = str
                m = mempool.cursor()
                for x in removal_signature:
                    execute_param(m,("DELETE FROM transactions WHERE signature =?;"),(x,), app_log)
                    app_log.warning("Removed a transaction with the following signature from mempool: {}".format(x))
                mempool.commit()
                mempool.close()

                #remove sent from mempool

            #submit mined block to node

                #break
        except Exception, e:
            print e
            time.sleep(0.1)
            raise

if __name__ == '__main__':
    freeze_support()  # must be this line, dont move ahead

    app_log = log.log("miner.log",debug_level_conf)
    (key, private_key_readable, public_key_readable, public_key_hashed, address) = keys.read()

    if not os.path.exists('mempool.db'):
        # create empty mempool
        mempool = sqlite3.connect('mempool.db')
        mempool.text_factory = str
        m = mempool.cursor()
        execute(m,("CREATE TABLE IF NOT EXISTS transactions (timestamp, address, recipient, amount, signature, public_key, openfield)"), app_log)
        mempool.commit()
        mempool.close()
        app_log.warning("Core: Created mempool file")
        # create empty mempool
    else:
        app_log.warning("Mempool exists")

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
        except Exception, e:
            print e
            app_log.warning(
                "Miner: Please start your node for the block to be submitted or adjust mining ip in settings.")
            time.sleep(1)
    # verify connection
    if sync_conf == 1:
        check_uptodate(120, app_log)

    instances = range(int(mining_threads_conf))
    print instances
    for q in instances:
        p = Process(target=miner,args=(str(q+1),private_key_readable, public_key_hashed, address))
        p.daemon = True
        p.start()
        print "thread "+str(p)+ " started"
    for q in instances:
        p.join()
        p.terminate()


