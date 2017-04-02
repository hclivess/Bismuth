import math, base64, sqlite3, os, hashlib, time, socks, keys, log
from Crypto.Signature import PKCS1_v1_5
from Crypto.Hash import SHA
from Crypto import Random

app_log = log.log("miner.log")

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
# load config

def send(sdef, data):
    sdef.sendall(data)

def bin_convert(string):
    return ''.join(format(ord(x), 'b') for x in string)

(key, private_key_readable, public_key_readable, public_key_hashed, address) = keys.read()

# verify connection
connected = 0
while connected == 0:
    try:
        s = socks.socksocket()
        if tor_conf == 1:
            s.setproxy(socks.PROXY_TYPE_SOCKS5, "127.0.0.1", 9050)
        s.connect((mining_ip_conf, int(port)))
        app_log.info("Connected")
        connected = 1
        s.close()
    except Exception, e:
        print e
        app_log.info("Miner: Please start your node for the block to be submitted or adjust mining ip in settings.")
        time.sleep(1)
# verify connection

if not os.path.exists('mempool.db'):
    # create empty mempool
    mempool = sqlite3.connect('mempool.db')
    mempool.text_factory = str
    m = mempool.cursor()
    m.execute(
        "CREATE TABLE IF NOT EXISTS transactions (timestamp, address, recipient, amount, signature, public_key, openfield)")
    mempool.commit()
    mempool.close()
    app_log.info("Core: Created mempool file")
    # create empty mempool
else:
    app_log.info("Mempool exists")

block_timestamp = 0  # init
tries = 0
rndfile = Random.new()

while True:
    try:
        tries = tries +1
        # calculate new hash

        if tries % int(diff_recalc_conf) == 0 or tries == 1: #only do this ever so often
            block_timestamp = '%.2f' % time.time()

            conn = sqlite3.connect("static/ledger.db") #open to select the last tx to create a new hash from
            conn.text_factory = str
            c = conn.cursor()
            c.execute("SELECT block_hash, block_height,timestamp FROM transactions WHERE reward != 0 ORDER BY block_height DESC LIMIT 1;")
            result = c.fetchall()
            db_block_hash = result[0][0]
            db_block_height = result[0][1]
            timestamp_last_block = float(result[0][2])

            # calculate difficulty
            c.execute("SELECT avg(timestamp) FROM transactions where block_height >= ? and reward != 0;",(str(db_block_height - 30),))
            timestamp_avg = c.fetchall()[0][0]  # select the reward block
            #print timestamp_avg

            timestamp_difference = timestamp_last_block - timestamp_avg

            diff = float(math.log(1e18 / timestamp_difference))
            if db_block_height < 50:
                diff = 33
            #if diff < 4:
            #    diff = 4
            # calculate difficulty
            conn.close()
            app_log.info("Mining, " + str(tries) + " cycles passed, difficulty: " + str(diff))
            diff = int(diff)

            # serialize txs
            mempool = sqlite3.connect("mempool.db")
            mempool.text_factory = str
            m = mempool.cursor()
            m.execute("SELECT * FROM transactions ORDER BY timestamp;")
            result = m.fetchall()  # select all txs from mempool
            mempool.close()

        block_send = []
        del block_send[:]  # empty
        removal_signature = []
        del removal_signature[:]  # empty

        for dbdata in result:
            transaction = (
            dbdata[0], dbdata[1][:56], dbdata[2][:56], '%.8f' % float(dbdata[3]), dbdata[4], dbdata[5], dbdata[6],
            dbdata[7])  # create tuple
            # print transaction
            block_send.append(transaction)  # append tuple to list for each run
            removal_signature.append(str(dbdata[4]))  # for removal after successful mining

        nonce = hashlib.sha224(rndfile.read(16)).hexdigest()[:32]

        # claim reward
        transaction_reward = tuple
        transaction_reward = (block_timestamp, address[:56], address[:56], '%.8f' % float(0), "0", nonce)  # only this part is signed!
        # print transaction_reward

        h = SHA.new(str(transaction_reward))
        signer = PKCS1_v1_5.new(key)
        signature = signer.sign(h)
        signature_enc = base64.b64encode(signature)

        block_send.append((block_timestamp, address[:56], address[:56], '%.8f' % float(0), signature_enc,
                           public_key_hashed, "0", nonce))  # mining reward tx
        # claim reward

        #block_hash = hashlib.sha224(str(block_send) + db_block_hash).hexdigest()
        mining_hash = bin_convert(hashlib.sha224(nonce+db_block_hash).hexdigest())
        mining_condition = bin_convert(db_block_hash)[0:diff]

        if mining_condition in mining_hash:

            app_log.info("Miner: Found a good block_hash in "+str(tries)+" cycles")
            tries = 0

            #submit mined block to node

            submitted = 0
            while submitted == 0:
                try:
                    s = socks.socksocket()
                    s.connect((mining_ip_conf, int(port)))  # connect to local node
                    app_log.info("Connected")

                    app_log.info("Miner: Proceeding to submit mined block")

                    send(s, (str(len("block"))).zfill(10))
                    send(s, "block")
                    send(s, (str(len(str(block_send)))).zfill(10))
                    send(s, str(block_send))

                    submitted = 1
                    app_log.info("Miner: Block submitted")

                except Exception, e:
                    print e
                    app_log.info("Miner: Please start your node for the block to be submitted or adjust mining ip in settings.")
                    time.sleep(1)

            #remove sent from mempool

            mempool = sqlite3.connect("mempool.db")
            mempool.text_factory = str
            m = mempool.cursor()
            for x in removal_signature:
                m.execute("DELETE FROM transactions WHERE signature =?;",(x,))
                app_log.info("Removed a transaction with the following signature from mempool: "+str(x))
            mempool.commit()
            mempool.close()

            #remove sent from mempool

        #submit mined block to node

            #break
    except Exception, e:
        print e
        time.sleep(0.1)
        raise
