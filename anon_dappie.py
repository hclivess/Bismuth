import socketserver, connections, time, options, log, sqlite3, ast, socks, hashlib, os, random, re, keys, base64
from Crypto.Signature import PKCS1_v1_5
from Crypto.Hash import SHA
(port, genesis_conf, verify_conf, version_conf, thread_limit_conf, rebuild_db_conf, debug_conf, purge_conf, pause_conf, ledger_path_conf, hyperblocks_conf, warning_list_limit_conf, tor_conf, debug_level_conf, allowed, mining_ip_conf, sync_conf, mining_threads_conf, diff_recalc_conf, pool_conf, pool_address, ram_conf) = options.read()
(key, private_key_readable, public_key_readable, public_key_hashed, address) = keys.read() #import keys
app_log = log.log("anon.log",debug_level_conf)

def randomize(anon_amount, anon_recipient, identifier, anon_sender):
    divider = int(random.uniform(2, 4))
    per_tx = int(anon_amount/divider) #how much per tx
    tx_count = int(anon_amount/per_tx) #how many txs
    remainder = anon_amount - per_tx*tx_count #remainder
    print(divider, tx_count, per_tx, remainder, identifier, anon_sender)

    anonymize(tx_count, per_tx, remainder, anon_recipient, identifier, anon_sender)
    return

def anonymize(tx_count, per_tx, remainder, anon_recipient, identifier, anon_sender):
    # return remainder to source!
    a.execute("SELECT * FROM transactions WHERE openfield = ?", (identifier,))
    try:
        exists = a.fetchall()[0]

    except:#if payout didn't happen yet
        print(tx_count, per_tx, remainder, identifier)
        for tx in range(tx_count):
            #construct tx
            openfield = "mixer"
            keep = 0
            fee = float('%.8f' % float(0.01 + (float(per_tx) * 0.001) + (float(len(openfield)) / 100000) + (float(keep) / 10)))  # 0.1% + 0.01 dust

            timestamp = '%.2f' % time.time()
            transaction = (str(timestamp), str(address), str(anon_recipient), '%.8f' % float(per_tx - fee), str(keep), str(openfield))  # this is signed
            # print transaction

            h = SHA.new(str(transaction).encode("utf-8"))
            signer = PKCS1_v1_5.new(key)
            signature = signer.sign(h)
            signature_enc = base64.b64encode(signature)
            print("Encoded Signature: {}".format(signature_enc.decode("utf-8")))

            verifier = PKCS1_v1_5.new(key)
            if verifier.verify(h, signature) == True:
                print("The signature is valid, proceeding to save transaction to mempool")
            #construct tx

            a.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?)", (str(timestamp), str(address), str(anon_recipient), '%.8f' % float(per_tx - fee), str(signature_enc.decode("utf-8")), str(public_key_hashed), str(keep), str(identifier)))
            anon.commit()
            m.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?)", (str(timestamp), str(address), str(anon_recipient), '%.8f' % float(per_tx - fee), str(signature_enc.decode("utf-8")), str(public_key_hashed), str(keep), str(openfield)))
            mempool.commit()


        openfield = "mixer"
        keep = 0
        fee = float('%.8f' % float(0.01 + (float(remainder) * 0.001) + (float(len(openfield)) / 100000) + (float(keep) / 10)))  # 0.1% + 0.01 dust
        timestamp = '%.2f' % time.time()
        transaction = (str(timestamp), str(address), str(anon_sender), '%.8f' % float(remainder - fee), str(keep), str(openfield))  # this is signed
        m.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?)", (str(timestamp), str(address), str(anon_sender), '%.8f' % float(remainder - fee), str(signature_enc.decode("utf-8")), str(public_key_hashed), str(keep), str(openfield)))
        mempool.commit()
    return

if not os.path.exists('anon.db'):
    # create empty mempool
    anon = sqlite3.connect('anon.db',timeout=1)
    anon.text_factory = str
    a = anon.cursor()
    a.execute("CREATE TABLE IF NOT EXISTS transactions (timestamp, address, recipient, amount, signature, public_key, keep, openfield)")
    anon.commit()
    print("Created anon file")

anon = sqlite3.connect('anon.db')
anon.text_factory = str
a = anon.cursor()

conn = sqlite3.connect('static/ledger.db')
conn.text_factory = str
c = conn.cursor()

mempool = sqlite3.connect('mempool.db')
mempool.text_factory = str
m = mempool.cursor()

while True:
    for row in c.execute("SELECT * FROM transactions WHERE recipient = ? and openfield LIKE ?", (address,)+("enc="+'%',)):
        anon_sender = row[2]

        try:
            #print (row)
            anon_recipient_encrypted = (row[11].lstrip("enc="))
            #print(anon_recipient_encrypted)
            anon_recipient = key.decrypt(ast.literal_eval(anon_recipient_encrypted)).decode("utf-8").split(":")[1]
            #print(anon_recipient)

            if len(anon_recipient) == 56:
                anon_amount = float(row[4])
                identifier = row[5][:8] #only save locally
                #print (anon_sender, anon_recipient, anon_amount, identifier)

                randomize(float(anon_amount), anon_recipient, identifier, anon_sender)
            else:
                print ("Wrong target address length")
        except Exception as e:
            print (e)
            #print("issue occured")

    time.sleep(15)




