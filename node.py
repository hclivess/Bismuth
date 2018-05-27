# never remove the str() conversion in data evaluation or database inserts or you will debug for 14 days as signed types mismatch
# if you raise in the server thread, the server will die and node will stop
# never use codecs, they are bugged and do not provide proper serialization
# must unify node and client now that connections parameters are function parameters
# if you have a block of data and want to insert it into sqlite, you must use a single "commit" for the whole batch, it's 100x faster
# do not isolation_level=None/WAL hdd levels, it makes saving slow
# rolling back indexes: 1424 and 945


VERSION = "4.2.4.7"

# Bis specific modules
import log, options, connections, peershandler, apihandler

import shutil, socketserver, base64, hashlib, os, re, sqlite3, sys, threading, time, socks, random, keys, math, requests, tarfile, essentials, glob
from decimal import *
import tokensv2
import aliases
from quantizer import *
from ann import ann_get, ann_ver_get
from essentials import fee_calculate

from Crypto.Hash import SHA
from Crypto.PublicKey import RSA
from Crypto.Signature import PKCS1_v1_5

import mempool as mp


# load config
# global ban_threshold

# remove after hf - WARNING : dup in mempool.py
global drift_limit
drift_limit = 30
#remove after hf


getcontext().rounding=ROUND_HALF_EVEN


global hdd_block
global last_block
last_block = 0

dl_lock = threading.Lock()
db_lock = threading.Lock()
#mem_lock = threading.Lock()
# peersync_lock = threading.Lock()

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
hyper_path_conf = config.hyper_path_conf
hyper_recompress_conf = config.hyper_recompress_conf
# ban_threshold = config.ban_threshold
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
version_allow = config.version_allow
full_ledger = config.full_ledger_conf
reveal_address = config.reveal_address
accept_peers = config.accept_peers
#mempool_allowed = config.mempool_allowed
terminal_output = config.terminal_output
#mempool_ram_conf = config.mempool_ram_conf


# nodes_ban_reset=config.nodes_ban_reset

# global banlist
# banlist=config.banlist

# global whitelist
# whitelist=config.whitelist

global peers


def tokens_rollback(height, app_log):
    """rollback token index"""
    tok = sqlite3.connect("static/index.db")
    tok.text_factory = str
    t = tok.cursor()
    execute_param(t, ("DELETE FROM tokens WHERE block_height >= ?;"), (height-1,))
    commit(tok)
    t.close()
    app_log.warning("Rolled back the token index to {}".format(height-1))


def aliases_rollback(height, app_log):
    """rollback alias index"""
    ali = sqlite3.connect("static/index.db")
    ali.text_factory = str
    a = ali.cursor()
    execute_param(a, ("DELETE FROM aliases WHERE block_height >= ?;"), (height-1,))
    commit(ali)
    a.close()
    app_log.warning("Rolled back the alias index to {}".format(height-1))

def sendsync(sdef, peer_ip, status, provider):

    app_log.warning("Outbound: Synchronization with {} finished after: {}, sending new sync request".format(peer_ip, status))

    if provider:
        peers.peers_save(peerlist, peer_ip)

    time.sleep(Decimal(pause_conf))
    while db_lock.locked():
        time.sleep(Decimal(pause_conf))

    connections.send(sdef, "sendsync", 10)


def validate_pem(public_key):
    # verify pem as cryptodome does
    pem_data = base64.b64decode(public_key).decode("utf-8")
    regex = re.compile("\s*-----BEGIN (.*)-----\s+")
    match = regex.match(pem_data)
    if not match:
        raise ValueError("Not a valid PEM pre boundary")

    marker = match.group(1)

    regex = re.compile("-----END (.*)-----\s*$")
    match = regex.search(pem_data)
    if not match or match.group(1) != marker:
        raise ValueError("Not a valid PEM post boundary")
        # verify pem as cryptodome does


def download_file(url, filename):
    try:
        r = requests.get(url, stream=True)
        total_size = int(r.headers.get('content-length')) / 1024

        with open(filename, 'wb') as filename:
            chunkno = 0
            for chunk in r.iter_content(chunk_size=1024):
                if chunk:
                    chunkno = chunkno + 1
                    if chunkno % 10000 == 0:  # every x chunks
                        print("Downloaded {} %".format(int(100 * ((chunkno) / total_size))))

                    filename.write(chunk)
                    filename.flush()
            print("Downloaded 100 %")

        return filename
    except:
        raise


# load config

def most_common(lst):
    return max(set(lst), key=lst.count)


def bootstrap():
    try:
        types = ['static/*.db-wal', 'static/*.db-shm']
        for type in types:
            for file in glob.glob (type):
                os.remove (file)
                print(file,"deleted")

        archive_path = ledger_path_conf + ".tar.gz"
        download_file("https://bismuth.cz/ledger.tar.gz", archive_path)

        tar = tarfile.open(archive_path)
        tar.extractall("static/")  # NOT COMPATIBLE WITH CUSTOM PATH CONFS
        tar.close()

    except:
        app_log.warning("Something went wrong during bootstrapping, aborted")
        raise

def check_integrity(database):
    # check ledger integrity
    ledger_check = sqlite3.connect(database)
    ledger_check.text_factory = str

    l = ledger_check.cursor()

    try:
        l.execute("PRAGMA table_info('transactions')")
        redownload = 0
    except:
        redownload = 1

    if len(l.fetchall()) != 12:
        app_log.warning("Status: Integrity check on database {} failed, bootstrapping from the website".format(database))
        redownload = 1
    else:
        ledger_check.close()

    if redownload == 1:
        bootstrap()


def percentage(percent, whole):
    return ((Decimal(percent) * Decimal(whole)) / 100)


def db_to_drive(hdd, h, hdd2, h2):
    global hdd_block

    app_log.warning("Moving new data to HDD")

    if ram_conf == 1:  # select RAM as source database
        source_db = sqlite3.connect(ledger_ram_file, uri=True, timeout=1)
    else:  # select hyper.db as source database
        source_db = sqlite3.connect(hyper_path_conf, timeout=1)

    source_db.text_factory = str
    sc = source_db.cursor()

    execute_param(sc, ("SELECT * FROM transactions WHERE block_height > ? ORDER BY block_height ASC"), (hdd_block,))
    result1 = sc.fetchall()

    if full_ledger == 1:  # we want to save to ledger.db
        for x in result1:
            h.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (x[0], x[1], x[2], x[3], x[4], x[5], x[6], x[7], x[8], x[9], x[10], x[11]))
        commit(hdd)

    if ram_conf == 1:  # we want to save to hyper.db from RAM/hyper.db depending on ram conf
        for x in result1:
            h2.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (x[0], x[1], x[2], x[3], x[4], x[5], x[6], x[7], x[8], x[9], x[10], x[11]))
        commit(hdd2)

    execute_param(sc, ("SELECT * FROM misc WHERE block_height > ? ORDER BY block_height ASC"), (hdd_block,))
    result2 = sc.fetchall()

    if full_ledger == 1:  # we want to save to ledger.db from RAM/hyper.db depending on ram conf
        for x in result2:
            h.execute("INSERT INTO misc VALUES (?,?)", (x[0], x[1]))
        commit(hdd)

    if ram_conf == 1:  # we want to save to hyper.db from RAM
        for x in result2:
            h2.execute("INSERT INTO misc VALUES (?,?)", (x[0], x[1]))
        commit(hdd2)

    # reward
    execute_param(sc, ('SELECT * FROM transactions WHERE address = "Development Reward" AND CAST(openfield AS INTEGER) > ?'), (hdd_block,))
    result3 = sc.fetchall()
    if full_ledger == 1:  # we want to save to ledger.db from RAM
        for x in result3:
            h.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (x[0], x[1], x[2], x[3], x[4], x[5], x[6], x[7], x[8], x[9], x[10], x[11]))
        commit(hdd)

    if ram_conf == 1:  # we want to save to hyper.db from RAM
        for x in result3:
            h2.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (x[0], x[1], x[2], x[3], x[4], x[5], x[6], x[7], x[8], x[9], x[10], x[11]))
        commit(hdd2)
    # reward

    h2.execute("SELECT block_height FROM transactions ORDER BY block_height DESC LIMIT 1")
    hdd_block = h2.fetchone()[0]

    app_log.warning("Ledger updated successfully")

def index_define():
    if "testnet" in version:
        index_db = "static/index_test.db"
    else:
        index_db = "static/index.db"
    index = sqlite3.connect(index_db, timeout=1)
    index.text_factory = str
    index_cursor = index.cursor()
    index.execute("PRAGMA page_size = 4096;")
    return index, index_cursor

def db_h_define():
    hdd = sqlite3.connect(ledger_path_conf, timeout=1)
    hdd.text_factory = str
    h = hdd.cursor()
    hdd.execute("PRAGMA page_size = 4096;")
    return hdd, h


def db_h2_define():
    hdd2 = sqlite3.connect(hyper_path_conf, timeout=1)
    hdd2.text_factory = str
    h2 = hdd2.cursor()
    hdd2.execute("PRAGMA page_size = 4096;")
    return hdd2, h2


def db_c_define():
    global hdd_block

    try:
        if ram_conf == 1:
            conn = sqlite3.connect(ledger_ram_file, uri=True, timeout=1, isolation_level=None)
        else:
            conn = sqlite3.connect(hyper_path_conf, uri=True, timeout=1, isolation_level=None)

        conn.execute('PRAGMA journal_mode = WAL;')
        conn.execute("PRAGMA page_size = 4096;")
        conn.text_factory = str
        c = conn.cursor()

    except Exception as e:
        app_log.info(e)

    return conn, c




def ledger_compress(ledger_path_conf, hyper_path_conf):
    """conversion of normal blocks into hyperblocks from ledger.db or hyper.db to hyper.db"""
    try:

        # if os.path.exists(hyper_path_conf+".temp"):
        #    os.remove(hyper_path_conf+".temp")
        #    app_log.warning("Status: Removed old temporary hyperblock file")
        #    time.sleep(100)

        if os.path.exists(hyper_path_conf):

            if full_ledger == 1:
                # cross-integrity check
                hdd = sqlite3.connect(ledger_path_conf, timeout=1)
                hdd.text_factory = str
                h = hdd.cursor()
                h.execute("SELECT block_height FROM transactions ORDER BY block_height DESC LIMIT 1")
                hdd_block_last = h.fetchone()[0]
                hdd.close()

                hdd2 = sqlite3.connect(hyper_path_conf, timeout=1)
                hdd2.text_factory = str
                h2 = hdd2.cursor()
                h2.execute("SELECT block_height FROM transactions ORDER BY block_height DESC LIMIT 1")
                hdd2_block_last = h2.fetchone()[0]
                hdd2.close()
                # cross-integrity check

                if hdd_block_last == hdd2_block_last and hyper_recompress_conf == 1:  # cross-integrity check
                    ledger_path_conf = hyper_path_conf  # only valid within the function, this temporarily sets hyper.db as source
                    app_log.warning("Status: Recompressing hyperblocks (keeping full ledger)")
                    recompress = 1
                elif hdd_block_last == hdd2_block_last and hyper_recompress_conf == 0:
                    app_log.warning("Status: Hyperblock recompression skipped")
                    recompress = 0
                else:
                    app_log.warning("Status: Cross-integrity check failed, hyperblocks will be rebuilt from full ledger")
                    recompress = 1
            else:
                if hyper_recompress_conf == 1:
                    app_log.warning("Status: Recompressing hyperblocks (without full ledger)")
                    recompress = 1
                else:
                    app_log.warning("Status: Hyperblock recompression skipped")
                    recompress = 0
        else:
            app_log.warning("Status: Compressing ledger to Hyperblocks")
            recompress = 1

        if recompress == 1:
            depth = 10000  # REWORK TO REFLECT TIME INSTEAD OF BLOCKS

            # if os.path.exists(ledger_path_conf + '.temp'):
            #    os.remove(ledger_path_conf + '.temp')

            if full_ledger == 1:
                shutil.copy(ledger_path_conf, ledger_path_conf + '.temp')
                hyper = sqlite3.connect(ledger_path_conf + '.temp')
            else:
                shutil.copy(hyper_path_conf, ledger_path_conf + '.temp')
                hyper = sqlite3.connect(ledger_path_conf + '.temp')

            hyper.text_factory = str
            hyp = hyper.cursor()

            addresses = []

            hyp.execute("UPDATE transactions SET address = 'Hypoblock' WHERE address = 'Hyperblock'")

            hyp.execute("SELECT block_height FROM transactions ORDER BY block_height DESC LIMIT 1;")
            db_block_height = int(hyp.fetchone()[0])

            hyp.execute("SELECT distinct(recipient) FROM transactions WHERE (block_height < ?) ORDER BY block_height;", (db_block_height - depth,))
            unique_addressess = hyp.fetchall()

            for x in set(unique_addressess):

                credit = Decimal ("0")
                for entry in hyp.execute("SELECT amount,reward FROM transactions WHERE (recipient = ? AND block_height < ?);", (x[0],) + (db_block_height - depth,)):
                    try:
                        credit = quantize_eight(credit) + quantize_eight(entry[0]) + quantize_eight(entry[1])
                        credit = 0 if credit is None else credit
                    except Exception as e:
                        credit = 0

                debit = Decimal ("0")
                for entry in hyp.execute("SELECT amount,fee FROM transactions WHERE (address = ? AND block_height < ?);", (x[0],) + (db_block_height - depth,)):
                    try:
                        debit = quantize_eight(debit) + quantize_eight(entry[0]) + quantize_eight(entry[1])
                        debit = 0 if debit is None else debit
                    except Exception as e:
                        debit = 0

                end_balance = quantize_eight(credit - debit)

                # app_log.info("Address: "+ str(x))
                # app_log.info("Credit: " + str(credit))
                # app_log.info("Debit: " + str(debit))
                # app_log.info("Fees: " + str(fees))
                # app_log.info("Rewards: " + str(rewards))
                # app_log.info("Balance: " + str(end_balance))
                # test for keep positivity
                # hyp.execute("SELECT block_height FROM transactions WHERE address OR recipient = ?", (x,))
                # keep_is = 1
                # try:
                #    hyp.fetchone()[0]
                # except:
                #    keep_is = 0
                # test for keep positivity

                if end_balance > 0:
                    timestamp = str(time.time())
                    hyp.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (db_block_height - depth - 1, timestamp, "Hyperblock", x[0], str(end_balance), "0", "0", "0", "0", "0",
                                                                                              "0", "0"))
            hyper.commit()

            # keep recognized openfield data

            # keep recognized openfield data

            hyp.execute("DELETE FROM transactions WHERE block_height < ? AND address != 'Hyperblock';", (db_block_height - depth,))
            hyper.commit()

            hyp.execute("DELETE FROM misc WHERE block_height < ?;", (db_block_height - depth,))  # remove diff calc
            hyper.commit()

            hyp.execute("VACUUM")
            hyper.close()

            if os.path.exists(hyper_path_conf):
                os.remove(hyper_path_conf)  # remove the old hyperblocks

            os.rename(ledger_path_conf + '.temp', hyper_path_conf)

        if full_ledger == 0 and os.path.exists(ledger_path_conf) and "testnet" not in version:
            os.remove(ledger_path_conf)
            app_log.warning("Removed full ledger and only kept hyperblocks")

    except Exception as e:
        raise ValueError("There was an issue converting to Hyperblocks: {}".format(e))


def most_common(lst):
    return max(set(lst), key=lst.count)


def bin_convert(string):
    return ''.join(format(ord(x), '8b').replace(' ', '0') for x in string)


def commit(cursor):
    """Secure commit for slow nodes"""
    while True:
        try:
            cursor.commit()
            break
        except Exception as e:
            app_log.warning("Database cursor: {}".format(cursor))
            app_log.warning("Database retry reason: {}".format(e))
            time.sleep(0.1)


def execute(cursor, query):
    """Secure execute for slow nodes"""
    while True:
        try:
            cursor.execute(query)
            break
        except Exception as e:
            app_log.warning("Database query: {} {}".format(cursor, query))
            app_log.warning("Database retry reason: {}".format(e))
            time.sleep(0.1)
    return cursor


def execute_param(cursor, query, param):
    """Secure execute w/ param for slow nodes"""
    while True:
        try:
            cursor.execute(query, param)
            break
        except Exception as e:
            app_log.warning("Database query: {} {} {}".format(cursor, query, param))
            app_log.warning("Database retry reason: {}".format(e))
    return cursor


def difficulty(c):
    execute (c, "SELECT * FROM transactions WHERE reward != 0 ORDER BY block_height DESC LIMIT 2")
    result = c.fetchone ()
    timestamp_last = Decimal (result[1])
    block_height = int (result[0])
    timestamp_before_last = Decimal (c.fetchone ()[1])

    if is_testnet or block_height > 700000:
        execute_param(c, ("SELECT timestamp FROM transactions WHERE CAST(block_height AS INTEGER) > ? AND reward != 0 ORDER BY timestamp ASC LIMIT 2"), (block_height - 1441,))
        timestamp_1441 = Decimal(c.fetchone()[0])
        block_time_prev = (timestamp_before_last - timestamp_1441) / 1440
        timestamp_1440 = Decimal(c.fetchone()[0])
        block_time = Decimal(timestamp_last - timestamp_1440) / 1440
        execute(c, ("SELECT difficulty FROM misc ORDER BY block_height DESC LIMIT 1"))
        diff_block_previous = Decimal(c.fetchone()[0])

        time_to_generate = timestamp_last - timestamp_before_last

        hashrate = pow(2, diff_block_previous / Decimal(2.0)) / (block_time * math.ceil(28 - diff_block_previous / Decimal(16.0)))
        # Calculate new difficulty for desired blocktime of 60 seconds
        target = Decimal(60.00)
        ##D0 = diff_block_previous
        difficulty_new = Decimal((2 / math.log(2)) * math.log(hashrate * target * math.ceil(28 - diff_block_previous / Decimal(16.0))))
        # Feedback controller
        Kd = 10
        difficulty_new = difficulty_new - Kd * (block_time - block_time_prev)
        diff_adjustment = (difficulty_new - diff_block_previous) / 720  # reduce by factor of 720

        if diff_adjustment > Decimal(1.0):
            diff_adjustment = Decimal(1.0)

        difficulty_new_adjusted = quantize_ten(diff_block_previous + diff_adjustment)
        difficulty = difficulty_new_adjusted

        diff_drop_time = 180

        if Decimal (time.time()) > Decimal (timestamp_last) + Decimal (diff_drop_time):
            time_difference = quantize_two (time.time()) - quantize_two (timestamp_last)
            diff_dropped = quantize_ten (difficulty) - quantize_ten (time_difference / diff_drop_time)



        else:
            diff_dropped = difficulty

        if difficulty < 50:
            difficulty = 50
        if diff_dropped < 50:
            diff_dropped = 50

        return (float('%.10f' % difficulty),float('%.10f' % diff_dropped), float(time_to_generate), float(diff_block_previous), float(block_time), float(hashrate), float(diff_adjustment), block_height)  # need to keep float here for database inserts support





    else:
        execute_param (c, ("SELECT timestamp FROM transactions WHERE CAST(block_height AS INTEGER) > ? AND reward != 0 ORDER BY timestamp ASC LIMIT 2"), (block_height - 1441,))
        timestamp_1441 = Decimal (c.fetchone ()[0])
        block_time_prev = (timestamp_before_last - timestamp_1441) / 1440
        timestamp_1440 = Decimal (c.fetchone ()[0])
        block_time = Decimal (timestamp_last - timestamp_1440) / 1440
        execute (c, ("SELECT difficulty FROM misc ORDER BY block_height DESC LIMIT 1"))
        diff_block_previous = Decimal (c.fetchone ()[0])
        ## Assume current difficulty D is known
        ## D = diff_block_previous
        ## Assume current blocktime is known, calculcated from historic data, for example last 1440 blocks
        ##T = block_time
        # Calculate network hashrate
        time_to_generate = timestamp_last - timestamp_before_last

        hashrate = pow (2, diff_block_previous / Decimal (2.0)) / (block_time * math.ceil (28 - diff_block_previous / Decimal (16.0)))
        # Calculate new difficulty for desired blocktime of 60 seconds
        target = Decimal (60.00)
        ##D0 = diff_block_previous
        difficulty_new = Decimal ((2 / math.log (2)) * math.log (hashrate * target * math.ceil (28 - diff_block_previous / Decimal (16.0))))
        # Feedback controller
        Kd = 10
        difficulty_new = difficulty_new - Kd * (block_time - block_time_prev)
        diff_adjustment = (difficulty_new - diff_block_previous) / 720  # reduce by factor of 720
        # limit here
        difficulty_new_adjusted = quantize_ten (diff_block_previous + diff_adjustment)
        difficulty = difficulty_new_adjusted
        difficulty2 = difficulty
        time_now = time.time ()

        if block_time > 90.0:
            if time_now > timestamp_last + 300:  # if more than 5 minutes passed
                difficulty2 = difficulty - Decimal (1.0)

        if difficulty < 80:
            difficulty = 80
        if difficulty2 < 80:
            difficulty2 = 80

        return (float ('%.10f' % difficulty), float ('%.10f' % difficulty2), float (time_to_generate), float (diff_block_previous), float (block_time), float (hashrate), float (diff_adjustment), block_height)  # need to keep float here for database inserts support


def balanceget(balance_address, h3):
    # verify balance

    # app_log.info("Mempool: Verifying balance")
    # app_log.info("Mempool: Received address: " + str(balance_address))


    base_mempool = mp.MEMPOOL.fetchall ("SELECT amount, openfield FROM transactions WHERE address = ?;", (balance_address,))

    # include mempool fees

    debit_mempool = 0
    if base_mempool:
        for x in base_mempool:
            debit_tx = Decimal(x[0])
            fee = fee_calculate(x[1])
            debit_mempool = quantize_eight(debit_mempool + debit_tx + fee)
    else:
        debit_mempool = 0
    # include mempool fees

    credit_ledger = Decimal ("0")
    for entry in execute_param (h3, ("SELECT amount FROM transactions WHERE recipient = ?;"), (balance_address,)):
        try:
            credit_ledger = quantize_eight (credit_ledger) + quantize_eight (entry[0])
            credit_ledger = 0 if credit_ledger is None else credit_ledger
        except:
            credit_ledger = 0

    fees = Decimal ("0")
    debit_ledger = Decimal ("0")

    for entry in execute_param (h3, ("SELECT fee, amount FROM transactions WHERE address = ?;"), (balance_address,)):
        try:
            fees = quantize_eight (fees) + quantize_eight (entry[0])
            fees = 0 if fees is None else fees
        except:
            fees = 0

        try:
            debit_ledger = debit_ledger + Decimal (entry[1])
            debit_ledger = 0 if debit_ledger is None else debit_ledger
        except:
            debit_ledger = 0

    debit = quantize_eight (debit_ledger + debit_mempool)

    rewards = Decimal ("0")
    for entry in execute_param (h3, ("SELECT reward FROM transactions WHERE recipient = ?;"), (balance_address,)):
        try:
            rewards = quantize_eight (rewards) + quantize_eight (entry[0])
            rewards = 0 if rewards is None else rewards
        except:
            rewards = 0

    balance = quantize_eight (credit_ledger - debit - fees + rewards)
    balance_no_mempool = float(credit_ledger) - float(debit_ledger) - float(fees) + float(rewards)
    # app_log.info("Mempool: Projected transction address balance: " + str(balance))
    return str (balance), str (credit_ledger), str (debit), str (fees), str (rewards), str(balance_no_mempool)




def verify(h3):
    try:
        app_log.warning("Blockchain verification started...")
        # verify blockchain
        execute(h3, ("SELECT Count(*) FROM transactions"))
        db_rows = h3.fetchone()[0]
        app_log.warning("Total steps: {}".format(db_rows))

        # verify genesis
        if full_ledger:
            execute(h3, ("SELECT block_height, recipient FROM transactions WHERE block_height = 1"))
            result = h3.fetchall()[0]
            block_height = result[0]
            genesis = result[1]
            app_log.warning("Genesis: {}".format(genesis))
            if str(genesis) != genesis_conf and int(block_height) == 0:  # change this line to your genesis address if you want to clone
                app_log.warning("Invalid genesis address")
                sys.exit(1)
        # verify genesis

        invalid = 0
        for row in execute(h3, ('SELECT * FROM transactions WHERE block_height > 1 and reward = 0 ORDER BY block_height')):

            db_block_height = str(row[0])
            db_timestamp = '%.2f' % (quantize_two(row[1]))
            db_address = str(row[2])[:56]
            db_recipient = str(row[3])[:56]
            db_amount = '%.8f' %(quantize_eight(row[4]))
            db_signature_enc = str(row[5])[:684]
            db_public_key_hashed = str(row[6])[:1068]
            db_public_key = RSA.importKey(base64.b64decode(db_public_key_hashed))
            db_operation = str(row[10])[:30]
            db_openfield = str(row[11]) #no limit for backward compatibility


            db_transaction = (db_timestamp, db_address, db_recipient, db_amount, db_operation, db_openfield)

            db_signature_dec = base64.b64decode(db_signature_enc)
            verifier = PKCS1_v1_5.new(db_public_key)
            hash = SHA.new(str(db_transaction).encode("utf-8"))
            if verifier.verify(hash, db_signature_dec):
                pass
            else:
                app_log.warning("Signature validation problem: {} {}".format(db_block_height,db_transaction))
                invalid = invalid + 1

        if invalid == 0:
            app_log.warning("All transacitons in the local ledger are valid")

    except Exception as e:
        app_log.warning("Error: {}".format(e))
        raise


def blocknf(block_hash_delete, peer_ip, conn, c, hdd, h, hdd2, h2):
    global hdd_block

    if not db_lock.locked():
        db_lock.acquire()

        try:
            execute(c, ('SELECT * FROM transactions ORDER BY block_height DESC LIMIT 1'))
            results = c.fetchone()
            db_block_height = results[0]
            db_block_hash = results[7]

            if db_block_height < 2:
                app_log.info("Will not roll back this block")

            elif (db_block_hash != block_hash_delete):
                # print db_block_hash
                # print block_hash_delete
                app_log.info("We moved away from the block to rollback, skipping")

            else:
                # backup

                execute_param(c, ("SELECT * FROM transactions WHERE block_height >= ?;"), (str(db_block_height),))
                backup_data = c.fetchall()
                #this code continues at the bottom because of ledger presence check



                # delete followups
                execute_param(c, ("DELETE FROM transactions WHERE block_height >= ?;"), (str(db_block_height),))
                commit(conn)

                execute_param(c, ("DELETE FROM misc WHERE block_height >= ?;"), (str(db_block_height),))
                commit(conn)

                execute_param(c, ('DELETE FROM transactions WHERE address = "Development Reward" AND CAST(openfield AS INTEGER) >= ?'), (str(db_block_height),))
                commit(conn)

                app_log.warning("Node {} didn't find block {}({}), rolled back".format(peer_ip, db_block_height, db_block_hash))

                # roll back hdd too
                if full_ledger == 1:  # rollback ledger.db
                    execute_param(h, ("DELETE FROM transactions WHERE block_height >= ?;"), (str(db_block_height),))
                    commit(hdd)
                    execute_param(h, ("DELETE FROM misc WHERE block_height >= ?;"), (str(db_block_height),))
                    commit(hdd)

                if ram_conf == 1:  # rollback hyper.db
                    execute_param(h2, ("DELETE FROM transactions WHERE block_height >= ?;"), (str(db_block_height),))
                    commit(hdd2)
                    execute_param(h2, ("DELETE FROM misc WHERE block_height >= ?;"), (str(db_block_height),))
                    commit(hdd2)

                hdd_block = int(db_block_height) - 1
                # roll back hdd too

                # roll back reward too
                if full_ledger == 1:  # rollback ledger.db
                    execute_param(h, ('DELETE FROM transactions WHERE address = "Development Reward" AND CAST(openfield AS INTEGER) >= ?'), (str(db_block_height),))
                    commit(hdd)

                if ram_conf == 1:  # rollback hyper.db
                    execute_param(h2, ('DELETE FROM transactions WHERE address = "Development Reward" AND CAST(openfield AS INTEGER) >= ?'), (str(db_block_height),))
                    commit(hdd2)
                # roll back reward too

                # rollback indices
                tokens_rollback(db_block_height, app_log)
                aliases_rollback(db_block_height, app_log)
                # rollback indices


        except Exception as e:
            app_log.info (e)

        finally:
            db_lock.release ()

            try:
                for tx in backup_data:
                    tx_short = "{} - {} to {}: {} ({})".format (tx[1], tx[2], tx[3], tx[4], tx[11])
                    if tx[9] == 0:
                        app_log.info (mp.MEMPOOL.merge ((tx[1], tx[2], tx[3], tx[4], tx[5], tx[6], tx[10], tx[11]), peer_ip, c, False, revert=True))  # will get stuck if you change it to respect db_lock
                        app_log.warning ("Moved tx back to mempool: {}".format (tx_short))
            except Exception as e:
                app_log.warning ("Error during moving txs back to mempool: {}".format(e))

    else:
        app_log.info("Skipping rollback, other ledger operation in progress")



def manager(c):
    # global banlist
    global last_block

    # moved to peershandler
    # reset_time = startup_time
    # peers_test("peers.txt")
    # peers_test("suggested_peers.txt")

    until_purge = 0

    while True:
        # dict_keys = peer_dict.keys()
        # random.shuffle(peer_dict.items())
        if until_purge == 0:
            # will purge once at start, then about every hour (120 * 30 sec)
            mp.MEMPOOL.purge()
            until_purge = 120

        until_purge -= 1

        # peer management
        peers.manager_loop(target=worker)

        app_log.warning("Status: Threads at {} / {}".format(threading.active_count(), thread_limit_conf))
        app_log.info("Status: Syncing nodes: {}".format(syncing))
        app_log.info("Status: Syncing nodes: {}/3".format(len(syncing)))

        # Status display for Peers related info
        peers.status_log()
        mp.MEMPOOL.status()


        # last block
        execute(c, "SELECT block_height, timestamp FROM transactions WHERE reward != 0 ORDER BY block_height DESC LIMIT 1;")  # or it takes the first
        result = c.fetchall()[0]
        last_block = result[0]
        last_block_ago = quantize_two(result[1])
        app_log.warning("Status: Last block {} was generated {} minutes ago".format(last_block, '%.2f' % ((quantize_two(time.time()) - last_block_ago) / 60)))
        # last block

        # app_log.info(threading.enumerate() all threads)
        time.sleep(30)


def digest_block(data, sdef, peer_ip, conn, c, hdd, h, hdd2, h2, h3):
    global hdd_block
    block_size = Decimal(sys.getsizeof(str(data))) / Decimal(1000000)
    app_log.warning("Block size: {} MB".format(block_size))


    if not db_lock.locked():
        db_lock.acquire()

        while mp.MEMPOOL.lock.locked():
            time.sleep (0.1)
            app_log.info ("Waiting for mempool to unlock {}".format (peer_ip))

        block_valid = 1  # init

        app_log.info("Digesting started from {}".format(peer_ip))

        try:
            block_list = data

            # reject block with duplicate transactions
            signature_list = []
            block_transactions = []

            for transaction_list in block_list:

                for entry in transaction_list:  # sig 4
                    entry_signature = entry[4]

                    if entry_signature:  # prevent empty signature database retry hack
                        signature_list.append(entry_signature)

                        # reject block with transactions which are already in the ledger
                        execute_param(h3, ("SELECT block_height FROM transactions WHERE signature = ?;"), (entry_signature,))
                        try:
                            result = h3.fetchall()[0]
                            app_log.warning("That transaction is already in our ledger, row {}".format(result[0]))
                            block_valid = 0

                        except:
                            pass
                            # reject block with transactions which are already in the ledger
                    else:
                        block_valid = 0
                        app_log.warning("Empty signature from {}".format(peer_ip))

                if len(signature_list) != len(set(signature_list)):
                    app_log.warning("There are duplicate transactions in this block, rejected")
                    block_valid = 0  # dont really need this one
                del signature_list[:]

                # reject block with duplicate transactions

                # previous block info
                execute(c, ("SELECT block_hash, block_height,timestamp FROM transactions WHERE reward != 0 ORDER BY block_height DESC LIMIT 1;"))
                result = c.fetchall()
                db_block_hash = result[0][0]
                db_block_height = result[0][1]
                db_timestamp_last = quantize_two(result[0][2])
                block_height_new = db_block_height + 1
                # previous block info

                transaction_list_converted = []  # makes sure all the data are properly converted as in the previous lines
                for transaction in transaction_list:
                    # verify signatures
                    received_timestamp = '%.2f' % (quantize_two(transaction[0]))
                    received_address = str(transaction[1])[:56]
                    received_recipient = str(transaction[2])[:56]
                    received_amount = '%.8f' %(quantize_eight(transaction[3]))
                    received_signature_enc = str(transaction[4])[:684]
                    received_public_key_hashed = str(transaction[5])[:1068]
                    received_operation = str(transaction[6])[:30]
                    received_openfield = str(transaction[7])[:100000]

                    transaction_list_converted.append((received_timestamp, received_address, received_recipient, received_amount, received_signature_enc, received_public_key_hashed, received_operation, received_openfield))

                    received_public_key = RSA.importKey(base64.b64decode(received_public_key_hashed))  # convert readable key to instance

                    received_signature_dec = base64.b64decode(received_signature_enc)
                    verifier = PKCS1_v1_5.new(received_public_key)

                    validate_pem(received_public_key_hashed)

                    hash = SHA.new(str((received_timestamp, received_address, received_recipient, received_amount, received_operation, received_openfield)).encode("utf-8"))
                    if not verifier.verify(hash, received_signature_dec):
                        app_log.warning("Invalid signature")
                        # print(received_timestamp +"\n"+ received_address +"\n"+ received_recipient +"\n"+ received_amount +"\n"+ received_operation +"\n"+ received_openfield)
                        block_valid = 0
                    else:
                        app_log.info("Valid signature")

                    #if received_operation != "1" and received_operation != "0":
                    #    block_valid = 0
                    #    # print (type(received_operation))
                    #    app_log.warning("Wrong keep value {}".format(received_operation))

                    if quantize_eight(received_amount) < 0:
                        block_valid = 0
                        app_log.warning("Negative balance spend attempt")

                    #if transaction != transaction_list[-1]:  # non-mining txs / disabled (per pool request)

                    if received_address != hashlib.sha224(base64.b64decode(received_public_key_hashed)).hexdigest():
                        app_log.warning("Attempt to spend from a wrong address")
                        block_valid = 0

                    if db_block_height > 700000: # start validating
                        if not essentials.address_validate(received_address) or not essentials.address_validate(received_recipient):
                            app_log.warning("Not a valid address")
                            block_valid = 0

                    if transaction == transaction_list[-1]:  # recognize the last transaction as the mining reward transaction
                        block_timestamp = received_timestamp
                        nonce = received_openfield[:128]
                        miner_address = received_address

                        # if float(db_timestamp_last) + 30 > float(block_timestamp): #if block comes 0-30 seconds after the previous one
                        #    error_msg = "The mined block is too close to the previous one"
                        #    block_valid = 0

                    time_now = time.time()

                    global drift_limit
                    if "testnet" in version or db_block_height > 700000:
                        drift_limit = 0

                    if quantize_two(time_now) + drift_limit < quantize_two(received_timestamp):
                        app_log.warning("Future transaction not allowed, timestamp {} minutes in the future".format(quantize_two((quantize_two(received_timestamp) - quantize_two(time_now)) / 60)))
                        block_valid = 0
                    if quantize_two(db_timestamp_last) - 86400 > quantize_two(received_timestamp):
                        app_log.warning("Transaction older than 24h not allowed.")
                        block_valid = 0
                        # verify signatures

                # reject blocks older than latest block
                if quantize_two(block_timestamp) <= quantize_two(db_timestamp_last):
                    block_valid = 0
                    app_log.warning("Block is older than the previous one, will be rejected")
                # reject blocks older than latest block

                if block_valid == 1:
                    # calculate difficulty

                    diff = difficulty(c)

                    app_log.warning ("Time to generate block {}: {:.2f}".format (db_block_height+1, diff[2]))
                    app_log.warning ("Current difficulty: {}".format (diff[3]))
                    app_log.warning ("Current blocktime: {}".format (diff[4]))
                    app_log.warning ("Current hashrate: {}".format (diff[5]))
                    app_log.warning ("New difficulty after adjustment: {}".format (diff[6]))
                    app_log.warning ("Difficulty: {} {}".format (diff[0], diff[1]))

                    # app_log.info("Transaction list: {}".format(transaction_list_converted))
                    block_hash = hashlib.sha224((str(transaction_list_converted) + db_block_hash).encode("utf-8")).hexdigest()
                    # app_log.info("Last block hash: {}".format(db_block_hash))
                    app_log.info("Calculated block hash: {}".format(block_hash))
                    # app_log.info("Nonce: {}".format(nonce))

                    # check if we already have the hash
                    execute_param(h3, ("SELECT block_hash FROM transactions WHERE block_hash = ?;"), (block_hash,))
                    try:
                        dummy = c.fetchone()[0]
                        block_valid = 0
                        app_log.warning("Skipping digestion of block {} from {}, because we already have it".format(peer_ip,dummy))
                    except:
                        pass
                    # check if we already have the hash

                if block_valid == 1:
                    mining_hash = bin_convert(hashlib.sha224((miner_address + nonce + db_block_hash).encode("utf-8")).hexdigest())

                    diff_drop_time = 300
                    if is_testnet or db_block_height > 700000:
                        diff_drop_time = 180

                    mining_condition = bin_convert(db_block_hash)[0:int(diff[0])]

                    if "testnet" in version or db_block_height > 700000:
                        if mining_condition in mining_hash:  # simplified comparison, no backwards mining
                            app_log.info ("Difficulty requirement satisfied for block {} from {}".format (block_height_new, peer_ip))
                            diff_save = diff[0]

                            """
                            time_difference = quantize_two(received_timestamp) - quantize_two(db_timestamp_last)

                            diff_dropped = quantize_ten(diff[0])-quantize_ten(time_difference/600)
                            if diff_dropped < 50:
                                diff_dropped = 50
                            app_log.warning(diff_dropped)
                            """

                        elif Decimal(received_timestamp) > Decimal(db_timestamp_last) + Decimal(diff_drop_time): #uses block timestamp, dont merge with diff() for security reasons
                            time_difference = quantize_two(received_timestamp) - quantize_two(db_timestamp_last)
                            diff_dropped = quantize_ten(diff[0])-quantize_ten(time_difference/diff_drop_time)
                            if diff_dropped < 50:
                                diff_dropped = 50

                            mining_condition = bin_convert (db_block_hash)[0:int (diff_dropped)]

                            if mining_condition in mining_hash:  # simplified comparison, no backwards mining
                                app_log.info ("Readjusted difficulty requirement satisfied for block {} from {}".format (block_height_new, peer_ip))
                                diff_save = diff[0] #lie about what diff was matched not to mess up the diff algo
                            else:
                                # app_log.info("Digest: Difficulty requirement not satisfied: " + bin_convert(miner_address) + " " + bin_convert(block_hash))
                                app_log.warning ("Readjusted difficulty too low for block {} from {}, should be at least {}".format (block_height_new, peer_ip, diff_dropped))
                                block_valid = 0
                        else:
                            # app_log.info("Digest: Difficulty requirement not satisfied: " + bin_convert(miner_address) + " " + bin_convert(block_hash))
                            app_log.warning("Difficulty too low for block {} from {}, should be at least {}".format(block_height_new, peer_ip, diff[0]))
                            block_valid = 0


                    else:


                        if mining_condition in mining_hash:  # simplified comparison, no backwards mining
                            app_log.info("Difficulty requirement satisfied for block {} from {}".format(block_height_new, peer_ip))
                            diff_save = diff[0]

                        elif time_now > db_timestamp_last + diff_drop_time:

                            mining_condition = bin_convert(db_block_hash)[0:int(diff[1])]
                            if mining_condition in mining_hash:  # simplified comparison, no backwards mining
                                app_log.info("Readjusted difficulty requirement satisfied for block {} from {}".format(block_height_new, peer_ip))
                                diff_save = diff[1]
                            else:
                                # app_log.info("Digest: Difficulty requirement not satisfied: " + bin_convert(miner_address) + " " + bin_convert(block_hash))
                                app_log.warning("Readjusted difficulty too low for block {} from {}, should be at least {}".format(block_height_new, peer_ip, diff[1]))
                                block_valid = 0

                        else:
                            # app_log.info("Digest: Difficulty requirement not satisfied: " + bin_convert(miner_address) + " " + bin_convert(block_hash))
                            app_log.warning("Difficulty too low for block {} from {}, should be at least {}".format(block_height_new, peer_ip, diff[0]))
                            block_valid = 0

                        # print data
                        # print transaction_list
                    # match difficulty

                    fees_block = []

                    if peers.is_banned(peer_ip):
                        block_valid = 0
                        app_log.warning("Cannot accept blocks form a banned peer")

                if block_valid == 0:
                    #app_log.warning("Check 1: A part of the block is invalid, rejected: {}".format(error_msg))
                    #app_log.info("Check 1: Complete rejected data: {}".format(data))
                    if peers.warning(sdef, peer_ip, "Rejected block", 2):
                        raise ValueError("{} banned".format(peer_ip))
                    raise ValueError ("Block digestion aborted")

                if block_valid == 1:
                    for transaction in transaction_list:
                        db_timestamp = '%.2f' % quantize_two(transaction[0])
                        db_address = str(transaction[1])[:56]
                        db_recipient = str(transaction[2])[:56]
                        db_amount = '%.8f' % quantize_eight(transaction[3])
                        db_signature = str(transaction[4])[:684]
                        db_public_key_hashed = str(transaction[5])[:1068]
                        db_operation = str(transaction[6])[:30]
                        db_openfield = str(transaction[7])[:100000]

                        # print "sync this"
                        # print block_timestamp
                        # print transaction_list
                        # print db_block_hash
                        # print (str((block_timestamp,transaction_list,db_block_hash)))

                        # app_log.info("Digest: tx sig not found in the local ledger, proceeding to check before insert")

                        # app_log.info("Digest: Verifying balance")
                        # app_log.info("Digest: Received address: " + str(db_address))

                        # include the new block

                        # if float(db_amount) > 0: todo: only check balances if user is spending

                        block_debit_address = 0
                        block_fees_address = 0

                        for x in transaction_list:
                            if x[1] == db_address:  # make calculation relevant to a particular address in the block
                                block_debit_address = quantize_eight(Decimal(block_debit_address) + Decimal(x[3]))

                                if x != transaction_list[-1]:
                                    block_fees_address = quantize_eight(Decimal(block_fees_address) + Decimal(fee_calculate(db_openfield)))  # exclude the mining tx from fees
                        # print("block_fees_address", block_fees_address, "for", db_address)

                        # app_log.info("Digest: Inbound block credit: " + str(block_credit))
                        # app_log.info("Digest: Inbound block debit: " + str(block_debit))
                        # include the new block

                        credit_ledger = Decimal("0")
                        for entry in execute_param(c, ("SELECT amount FROM transactions WHERE recipient = ?;"), (db_address,)):
                            credit_ledger = quantize_eight(credit_ledger) + quantize_eight(entry[0])
                            credit_ledger = 0 if credit_ledger is None else quantize_eight(credit_ledger)

                        credit = credit_ledger

                        debit_ledger = Decimal("0")
                        for entry in execute_param(c, ("SELECT amount FROM transactions WHERE address = ?;"), (db_address,)):
                            debit_ledger = quantize_eight(debit_ledger) + quantize_eight(entry[0])
                            debit_ledger = 0 if debit_ledger is None else quantize_eight(debit_ledger)

                        debit = quantize_eight(debit_ledger + block_debit_address)

                        fees = Decimal("0")
                        for entry in execute_param(c, ("SELECT fee FROM transactions WHERE address = ?;"), (db_address,)):
                            try:
                                fees = quantize_eight(fees) + quantize_eight(entry[0])
                                fees = 0 if fees is None else fees
                            except:
                                fees = 0

                        rewards = Decimal("0")
                        for entry in execute_param(c, ("SELECT reward FROM transactions WHERE recipient = ?;"), (db_address,)):
                            try:
                                rewards = quantize_eight(rewards) + quantize_eight(entry[0])
                                rewards = 0 if rewards is None else rewards
                            except:
                                rewards = 0

                        # app_log.info("Digest: Total credit: " + str(credit))
                        # app_log.info("Digest: Total debit: " + str(debit))
                        balance_pre = quantize_eight(credit_ledger - debit_ledger - fees + rewards)  # without projection
                        balance = quantize_eight(credit - debit - fees + rewards)
                        # app_log.info("Digest: Projected transction address balance: " + str(balance))

                        fee = fee_calculate(db_openfield)
                        # fee = '%.8f' % float(0.01 + (float(len(db_openfield)) / 100000) + int(db_operation))  # 0.01 dust

                        fees_block.append(quantize_eight(fee))
                        # app_log.info("Fee: " + str(fee))

                        # decide reward

                        if transaction == transaction_list[-1]:
                            db_amount = 0  # prevent spending from another address, because mining txs allow delegation
                            if db_block_height <= 10000000:
                                mining_reward = 15 - (quantize_eight(block_height_new) / quantize_eight(1000000))  # one zero less
                            else:
                                mining_reward = 0

                            reward = quantize_eight(mining_reward + sum(fees_block[:-1]))
                            fee = 0

                        else:
                            reward = 0

                            # dont request a fee for mined block so new accounts can mine

                        if quantize_eight(balance_pre) < quantize_eight(db_amount):
                            app_log.warning("sending more than owned".format(db_address))
                            block_valid = 0

                        elif quantize_eight(balance) - quantize_eight(block_fees_address) < 0:  # exclude fee check for the mining/header tx
                            app_log.warning("{} Cannot afford to pay fees".format(db_address))
                            block_valid = 0

                        else:
                            # append, but do not insert to ledger before whole block is validated, not that it takes already validated values (decimals, length)
                            app_log.info("Digest: Appending transaction back to block with {} transactions in it".format(len(block_transactions)))
                            block_transactions.append((block_height_new, db_timestamp, db_address, db_recipient, db_amount, db_signature, db_public_key_hashed, block_hash, fee, reward, db_operation, db_openfield))

                        try:
                            mp.MEMPOOL.delete_transaction(db_signature)
                            app_log.info("Digest: Removed processed transaction from the mempool")
                        except:
                            # tx was not in the local mempool
                            pass

                    # whole block validation
                    if block_valid == 0:
                        #app_log.info("Check 2: A part of the block is invalid, rejected: {}".format(error_msg))
                        #app_log.info("Check 2: Complete rejected block: {}".format(data))
                        if peers.warning(sdef, peer_ip, "Rejected block", 2):
                            raise ValueError("{} banned".format(peer_ip))
                        raise ValueError("Block digestion aborted")

                    if block_valid == 1:

                        # save diff
                        execute_param(c, "INSERT INTO misc VALUES (?, ?)", (block_height_new, diff_save))
                        commit(conn)
                        # save diff

                        for transaction in block_transactions:
                            execute_param(c, "INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
                                str(transaction[0]), str(transaction[1]),
                                str(transaction[2]), str(transaction[3]),
                                str(transaction[4]), str(transaction[5]),
                                str(transaction[6]), str(transaction[7]),
                                str(transaction[8]), str(transaction[9]),
                                str(transaction[10]), str(transaction[11])))
                            # secure commit for slow nodes
                            commit(conn)

                            # dev reward
                            if int(block_height_new) % 10 == 0:  # every 10 blocks
                                if transaction == block_transactions[-1]:  # put at the end
                                    execute_param(c, "INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                                  ("0", str(time_now), "Development Reward", str(genesis_conf), str(mining_reward),
                                                   "0", "0", "0", "0", "0", "0", str(block_height_new)))
                                    commit(conn)
                            # dev reward

                        app_log.warning("Block {} valid and saved from {}".format(block_height_new, peer_ip))

                        del block_transactions[:]
                        peers.unban(peer_ip)
                        # peers will test himself. Anyway, unban was only unwarning, not unbanning.
                        # if peer_ip in warning_list or peer_ip in banlist:
                        #    unban(peer_ip)

                        # whole block validation

        except Exception as e:
            app_log.warning("Block processing failed: {}".format(e))

            if debug_conf == 1:
                raise  # major debug client
            else:
                pass

        finally:
            if full_ledger == 1 or ram_conf == 1:  # first case move stuff from hyper.db to ledger.db; second case move stuff from ram to both
                db_to_drive(hdd, h, hdd2, h2)
            app_log.info("Digesting complete")
            db_lock.release()

    else:
        app_log.info("Skipping block processing from {}, someone delivered data faster".format(peer_ip))


def coherence_check():
    app_log.warning("Status: Testing chain coherence")
    if full_ledger == 1:
        chains_to_check = [ledger_path_conf, hyper_path_conf]
    else:
        chains_to_check = [hyper_path_conf]

    for chain in chains_to_check:
        conn = sqlite3.connect(chain)
        c = conn.cursor()

        # perform test on transaction table
        y = None
        for row in c.execute("SELECT block_height FROM transactions WHERE reward != 0 AND block_height != (0 OR 1) ORDER BY block_height ASC"):
            y_init = row[0]

            if y is None:
                y = y_init

            if row[0] != y:
                for chain2 in chains_to_check:
                    conn2 = sqlite3.connect(chain2)
                    c2 = conn2.cursor()
                    app_log.warning("Status: Chain {} transaction coherence error at: {}".format(chain, row[0]-1))
                    c2.execute("DELETE FROM transactions WHERE block_height >= ?", (row[0]-1,))
                    conn2.commit()
                    c2.execute("DELETE FROM misc WHERE block_height >= ?", (row[0]-1,))
                    conn2.commit()

                    execute_param(conn2, ('DELETE FROM transactions WHERE address = "Development Reward" AND CAST(openfield AS INTEGER) >= ?'), (row[0]-1,))
                    commit(conn2)
                    conn2.close()

                    # rollback indices
                    tokens_rollback(y, app_log)
                    aliases_rollback(y, app_log)
                    # rollback indices



                    app_log.warning("Status: Due to a coherence issue at block {}, {} has been rolled back and will be resynchronized".format(y, chain))
                break

            y = y + 1

        # perform test on misc table
        y = None

        for row in c.execute("SELECT block_height FROM misc WHERE block_height > ? ORDER BY block_height ASC", (300000,)):
            y_init = row[0]

            if y is None:
                y = y_init
                # print("assigned")
                # print (row[0], y)

            if row[0] != y:
                #print(row[0], y)
                for chain2 in chains_to_check:
                    conn2 = sqlite3.connect(chain2)
                    c2 = conn2.cursor()
                    app_log.warning("Status: Chain {} difficulty coherence error at: {}".format(chain, row[0]-1))
                    c2.execute("DELETE FROM transactions WHERE block_height >= ?", (row[0]-1,))
                    conn2.commit()
                    c2.execute("DELETE FROM misc WHERE block_height >= ?", (row[0]-1,))
                    conn2.commit()

                    execute_param(conn2, ('DELETE FROM transactions WHERE address = "Development Reward" AND CAST(openfield AS INTEGER) >= ?'), (row[0]-1,))
                    commit(conn2)
                    conn2.close()

                    # rollback indices
                    tokens_rollback(y, app_log)
                    aliases_rollback(y, app_log)
                    # rollback indices

                    app_log.warning("Status: Due to a coherence issue at block {}, {} has been rolled back and will be resynchronized".format(y, chain))
                break

            y = y + 1

        app_log.warning("Status: Chain coherence test complete for {}".format(chain))
        conn.close()


# init
def db_maintenance(conn):
    # db maintenance
    app_log.warning("Status: Database maintenance started")
    execute(conn, "VACUUM")
    mp.MEMPOOL.vacuum()
    app_log.warning("Status: Database maintenance finished")



class ThreadedTCPRequestHandler(socketserver.BaseRequestHandler):
    def handle(self):

        # global banlist
        # global ban_threshold
        global peers
        global apihandler


        peer_ip = self.request.getpeername()[0]

        # if threading.active_count() < thread_limit_conf or peer_ip == "127.0.0.1":
        # Always keep a slot for whitelisted (wallet could be there)
        if threading.active_count() < thread_limit_conf/3*2 or peers.is_whitelisted(peer_ip): #inbound
            capacity = 1
        else:
            capacity = 0
            try:
                self.request.close()
                app_log.info("Free capacity for {} unavailable, disconnected".format(peer_ip))
                # if you raise here, you kill the whole server
            except:
                pass
            finally:
                return

        banned = 0
        if peers.is_banned(peer_ip):
            banned = 1
            try:
                self.request.close()
                app_log.info("IP {} banned, disconnected".format(peer_ip))
            except:
                pass
            finally:
                return

        timeout_operation = 120  # timeout
        timer_operation = time.time()  # start counting

        while banned == 0 and capacity == 1:
            try:
                hdd2, h2 = db_h2_define()
                conn, c = db_c_define()
                if full_ledger == 1:
                    hdd, h = db_h_define()
                    h3 = h
                else:
                    hdd, h = None, None
                    h3 = h2

                index, index_cursor = index_define()

                # Failsafe
                if self.request == -1:
                    raise ValueError("Inbound: Closed socket from {}".format(peer_ip))
                    return
                if not time.time() <= timer_operation + timeout_operation:  # return on timeout
                    if warning(self.request, peer_ip, "Operation timeout", 2):
                        app_log.info("{} banned".format(peer_ip))
                        break

                    raise ValueError("Inbound: Operation timeout from {}".format(peer_ip))

                data = connections.receive(self.request, 10)

                app_log.info("Inbound: Received: {} from {}".format(data, peer_ip))  # will add custom ports later

                if data == 'version':
                    data = connections.receive(self.request, 10)
                    if data not in version_allow:
                        app_log.warning("Protocol version mismatch: {}, should be {}".format(data, version_allow))
                        connections.send(self.request, "notok", 10)
                        return
                    else:
                        app_log.warning("Inbound: Protocol version matched: {}".format(data))
                        connections.send(self.request, "ok", 10)

                elif data == 'mempool':

                    # receive theirs
                    segments = connections.receive(self.request, 10)
                    app_log.info(mp.MEMPOOL.merge(segments, peer_ip, c, False))

                    # receive theirs

                    #execute_param (m, ('SELECT timestamp,address,recipient,amount,signature,public_key,operation,openfield FROM transactions WHERE timeout < ? ORDER BY amount DESC;'), (int(time.time () - 5),))
                    if mp.MEMPOOL.sendable(peer_ip):
                        # Only send the diff
                        mempool_txs = mp.MEMPOOL.tx_to_send(peer_ip, segments)
                        # and note the time
                        mp.MEMPOOL.sent(peer_ip)
                    else:
                        # We already sent not long ago, send empy
                        mempool_txs = []


                    # send own
                    # app_log.info("Inbound: Extracted from the mempool: " + str(mempool_txs))  # improve: sync based on signatures only


                    # if len(mempool_txs) > 0: same as the other
                    connections.send(self.request, mempool_txs, 10)

                    # send own

                elif data == 'hello':

                    connections.send(self.request, "peers", 10)
                    connections.send(self.request, peers.peer_list(peerlist), 10)

                    while db_lock.locked():
                        time.sleep(quantize_two(pause_conf))
                    app_log.info("Inbound: Sending sync request")

                    connections.send(self.request, "sync", 10)

                elif data == "sendsync":
                    while db_lock.locked():
                        time.sleep(quantize_two(pause_conf))

                    global syncing
                    while len(syncing) >= 3:
                        time.sleep(int(pause_conf))

                    connections.send(self.request, "sync", 10)


                elif data == "blocksfnd":
                    app_log.info("Inbound: Client {} has the block(s)".format(peer_ip))  # node should start sending txs in this step

                    # app_log.info("Inbound: Combined segments: " + segments)
                    # print peer_ip
                    if db_lock.locked():
                        app_log.info("Skipping sync from {}, syncing already in progress".format(peer_ip))

                    else:
                        execute(c, "SELECT timestamp FROM transactions WHERE reward != 0 ORDER BY block_height DESC LIMIT 1;")  # or it takes the first
                        last_block_ago = quantize_two(c.fetchone()[0])

                        if last_block_ago < time.time() - 600:
                            # block_req = most_common(consensus_blockheight_list)
                            block_req = peers.consensus_most_common
                            app_log.warning("Most common block rule triggered")

                        else:
                            # block_req = max(consensus_blockheight_list)
                            block_req = peers.consensus_max
                            app_log.warning("Longest chain rule triggered")

                        if int(received_block_height) >= block_req:

                            try:  # they claim to have the longest chain, things must go smooth or ban
                                app_log.warning("Confirming to sync from {}".format(peer_ip))
                                connections.send(self.request, "blockscf", 10)

                                segments = connections.receive(self.request, 10)

                            except:
                                if peers.warning(self.request, peer_ip, "Failed to deliver the longest chain", 10):
                                    app_log.info("{} banned".format(peer_ip))
                                    break
                            else:
                                digest_block(segments, self.request, peer_ip, conn, c, hdd, h, hdd2, h2, h3)

                                # receive theirs
                        else:
                            app_log.warning("Rejecting to sync from {}".format(peer_ip))
                            connections.send(self.request, "blocksrj", 10)
                            app_log.info("Inbound: Distant peer {} is at {}, should be at least {}".format(peer_ip, received_block_height, block_req))

                    connections.send(self.request, "sync", 10)

                elif data == "blockheight":
                    try:
                        received_block_height = connections.receive(self.request, 10)  # receive client's last block height
                        app_log.info("Inbound: Received block height {} from {} ".format(received_block_height, peer_ip))

                        # consensus pool 1 (connection from them)
                        consensus_blockheight = int(received_block_height)  # str int to remove leading zeros
                        # consensus_add(peer_ip, consensus_blockheight, self.request)
                        peers.consensus_add(peer_ip, consensus_blockheight, self.request, last_block)
                        # consensus pool 1 (connection from them)

                        execute(c, ('SELECT block_height FROM transactions ORDER BY block_height DESC LIMIT 1'))
                        db_block_height = c.fetchone()[0]

                        # append zeroes to get static length
                        connections.send(self.request, db_block_height, 10)
                        # send own block height

                        if int(received_block_height) > db_block_height:
                            app_log.warning("Inbound: Client has higher block")

                            execute(c, ('SELECT block_hash FROM transactions ORDER BY block_height DESC LIMIT 1'))
                            db_block_hash = c.fetchone()[0]  # get latest block_hash

                            app_log.info("Inbound: block_hash to send: " + str(db_block_hash))
                            connections.send(self.request, db_block_hash, 10)

                            # receive their latest hash
                            # confirm you know that hash or continue receiving

                        elif int(received_block_height) <= db_block_height:
                            if int(received_block_height) == db_block_height:
                                app_log.info("Inbound: We have the same height as {} ({}), hash will be verified".format(peer_ip, received_block_height))
                            else:
                                app_log.warning("Inbound: We have higher ({}) block height than {} ({}), hash will be verified".format(db_block_height, peer_ip, received_block_height))

                            data = connections.receive(self.request, 10)  # receive client's last block_hash
                            # send all our followup hashes

                            app_log.info("Inbound: Will seek the following block: {}".format(data))

                            try:
                                execute_param(h3, ("SELECT block_height FROM transactions WHERE block_hash = ?;"), (data,))
                                client_block = h3.fetchone()[0]

                                app_log.info("Inbound: Client is at block {}".format(client_block))  # now check if we have any newer

                                execute(h3, ('SELECT block_hash FROM transactions ORDER BY block_height DESC LIMIT 1'))
                                db_block_hash = h3.fetchone()[0]  # get latest block_hash
                                if db_block_hash == data:
                                    app_log.info("Inbound: Client {} has the latest block".format(peer_ip))
                                    time.sleep(int(pause_conf))  # reduce CPU usage
                                    connections.send(self.request, "nonewblk", 10)

                                else:

                                    blocks_fetched = []
                                    del blocks_fetched[:]
                                    while len(str(blocks_fetched)) < 500000:  # limited size based on txs in blocks
                                        # execute_param(h3, ("SELECT block_height, timestamp,address,recipient,amount,signature,public_key,keep,openfield FROM transactions WHERE block_height > ? AND block_height <= ?;"),(str(int(client_block)),) + (str(int(client_block + 1)),))
                                        execute_param(h3, ("SELECT timestamp,address,recipient,amount,signature,public_key,cast(operation as TEXT),openfield FROM transactions WHERE block_height > ? AND block_height <= ?;"), (str(int(client_block)),str(int(client_block + 1)),))
                                        result = h3.fetchall()
                                        if not result:
                                            break
                                        blocks_fetched.extend([result])
                                        client_block = int(client_block) + 1

                                    #blocks_send = [[l[1:] for l in group] for _, group in groupby(blocks_fetched, key=itemgetter(0))]  # remove block number

                                    app_log.info("Inbound: Selected " + str(blocks_fetched) + " to send")

                                    connections.send(self.request, "blocksfnd", 10)

                                    confirmation = connections.receive(self.request, 10)

                                    if confirmation == "blockscf":
                                        app_log.info("Inbound: Client confirmed they want to sync from us")
                                        connections.send(self.request, blocks_fetched, 10)

                                    elif confirmation == "blocksrj":
                                        app_log.info("Inbound: Client rejected to sync from us because we're don't have the latest block")
                                        pass

                                        # send own

                            except Exception as e:
                                app_log.warning("Inbound: Block {} of {} not found".format(data[:8], peer_ip))
                                connections.send(self.request, "blocknf", 10)
                                connections.send(self.request, data, 10)
                    except Exception as e:
                        app_log.info("Inbound: Sync failed {}".format(e))


                elif data == "nonewblk":
                    connections.send(self.request, "sync", 10)

                elif data == "blocknf":
                    block_hash_delete = connections.receive(self.request, 10)
                    # print peer_ip
                    if consensus_blockheight == peers.consensus_max:
                        blocknf(block_hash_delete, peer_ip, conn, c, hdd, h, hdd2, h2)
                        if peers.warning(self.request, peer_ip, "Rollback", 2):
                            app_log.info("{} banned".format(peer_ip))
                            break
                    app_log.info("Outbound: Deletion complete, sending sync request")

                    while db_lock.locked():
                        time.sleep(quantize_two(pause_conf))
                    connections.send(self.request, "sync", 10)

                elif data == "block":
                    # if (peer_ip in allowed or "any" in allowed):  # from miner
                    if peers.is_allowed(peer_ip, data):  # from miner
                        # TODO: rights management could be done one level higher instead of repeating the same check everywhere

                        app_log.info("Outbound: Received a block from miner {}".format(peer_ip))
                        # receive block
                        segments = connections.receive(self.request, 10)
                        # app_log.info("Inbound: Combined mined segments: " + segments)

                        # check if we have the latest block

                        execute(c, ('SELECT block_height FROM transactions ORDER BY block_height DESC LIMIT 1'))
                        db_block_height = c.fetchone()[0]

                        # check if we have the latest block

                        if "testnet" not in version:
                            if len(peers.connection_pool) < 5 and not peers.is_whitelisted(peer_ip):
                                app_log.info("Outbound: Mined block ignored, insufficient connections to the network")
                            elif int(db_block_height) >= int(peers.consensus_max) - 3 and not db_lock.locked():
                                app_log.info("Outbound: Processing block from miner")
                                digest_block(segments, self.request, peer_ip, conn, c, hdd, h, hdd2, h2, h3)
                            elif db_lock.locked():
                                app_log.warning("Outbound: Block from miner skipped because we are digesting already")

                            # receive theirs
                            else:
                                app_log.info("Outbound: Mined block was orphaned because node was not synced, we are at block {}, should be at least {}".format(db_block_height, peers.consensus_max - 3))
                        else:
                            digest_block(segments, self.request, peer_ip, conn, c, hdd, h, hdd2, h2, h3)
                    else:
                        connections.receive(self.request, 10)  # receive block, but do nothing about it
                        app_log.info("{} not whitelisted for block command".format(peer_ip))

                elif data == "blocklast":
                    # if (peer_ip in allowed or "any" in allowed):  # only sends the miner part of the block!
                    if peers.is_allowed(peer_ip, data):
                        execute(c, ("SELECT * FROM transactions WHERE reward != 0 ORDER BY block_height DESC LIMIT 1;"))
                        block_last = c.fetchall()[0]

                        connections.send(self.request, block_last, 10)
                    else:
                        app_log.info("{} not whitelisted for blocklast command".format(peer_ip))

                elif data == "blockget":
                    # if (peer_ip in allowed or "any" in allowed):
                    if peers.is_allowed(peer_ip, data):
                        block_desired = str(connections.receive(self.request, 10))

                        execute_param(h3, ("SELECT * FROM transactions WHERE block_height = ?;"), (block_desired,))
                        block_desired_result = h3.fetchall()

                        connections.send(self.request, block_desired_result, 10)
                    else:
                        app_log.info("{} not whitelisted for blockget command".format(peer_ip))

                elif data == "mpinsert":
                    # if (peer_ip in allowed or "any" in allowed):
                    if peers.is_allowed(peer_ip, data):
                        mempool_insert = list(connections.receive(self.request, 10))
                        app_log.warning("mpinsert command")

                        mpinsert_result = mp.MEMPOOL.merge(mempool_insert, peer_ip, c, True, True)
                        app_log.warning("mpinsert result: {}".format(mpinsert_result))
                        connections.send(self.request, mpinsert_result, 10)
                    else:
                        app_log.info("{} not whitelisted for mpinsert command".format(peer_ip))

                elif data == "balanceget":
                    # if (peer_ip in allowed or "any" in allowed):
                    if peers.is_allowed(peer_ip, data):
                        balance_address = str(connections.receive(self.request, 10))  # for which address

                        balanceget_result = balanceget(balance_address, h3)

                        connections.send(self.request, balanceget_result, 10)  # return balance of the address to the client, including mempool
                        # connections.send(self.request, balance_pre, 10)  # return balance of the address to the client, no mempool
                    else:
                        app_log.info("{} not whitelisted for balanceget command".format(peer_ip))

                elif data == "mpget" and peers.is_allowed(peer_ip, data):
                    mempool_txs = mp.MEMPOOL.fetchall (mp.SQL_SELECT_TX_TO_SEND)

                    # app_log.info("Outbound: Extracted from the mempool: " + str(mempool_txs))  # improve: sync based on signatures only

                    # if len(mempool_txs) > 0: #wont sync mempool until we send something, which is bad
                    # send own
                    connections.send(self.request, mempool_txs, 10)

                elif data == "mpclear" and peer_ip == "127.0.0.1": #reserved for localhost
                    mp.MEMPOOL.clear()
                    commit(mempool)


                elif data == "keygen":
                    # if (peer_ip in allowed or "any" in allowed):
                    if peers.is_allowed(peer_ip, data):
                        (gen_private_key_readable, gen_public_key_readable, gen_address) = keys.generate()
                        connections.send(self.request, (gen_private_key_readable, gen_public_key_readable, gen_address), 10)
                        (gen_private_key_readable, gen_public_key_readable, gen_address) = (None, None, None)
                    else:
                        app_log.info("{} not whitelisted for keygen command".format(peer_ip))

                elif data == "addlist":
                    # if (peer_ip in allowed or "any" in allowed):
                    if peers.is_allowed(peer_ip, data):
                        address_tx_list = str(connections.receive(self.request, 10))
                        execute_param(h3, ("SELECT * FROM transactions WHERE (address = ? OR recipient = ?) ORDER BY block_height DESC"), (address_tx_list, address_tx_list,))
                        result = h3.fetchall()
                        connections.send(self.request, result, 10)
                    else:
                        app_log.info("{} not whitelisted for addlist command".format(peer_ip))

                elif data == "listlim":
                    # if (peer_ip in allowed or "any" in allowed):
                    if peers.is_allowed(peer_ip, data):
                        list_limit = str(connections.receive(self.request, 10))
                        # print (address_tx_list_limit)
                        execute_param(h3, ("SELECT * FROM transactions ORDER BY block_height DESC LIMIT ?"), (list_limit,))
                        result = h3.fetchall()
                        connections.send(self.request, result, 10)
                    else:
                        app_log.info("{} not whitelisted for listlim command".format(peer_ip))

                elif data == "addlistlim":
                    # if (peer_ip in allowed or "any" in allowed):
                    if peers.is_allowed(peer_ip, data):
                        address_tx_list = str(connections.receive(self.request, 10))
                        address_tx_list_limit = str(connections.receive(self.request, 10))
                        # print (address_tx_list_limit)
                        execute_param(h3, ("SELECT * FROM transactions WHERE (address = ? OR recipient = ?) ORDER BY block_height DESC LIMIT ?"), (address_tx_list,address_tx_list,address_tx_list_limit,))
                        result = h3.fetchall()
                        connections.send(self.request, result, 10)
                    else:
                        app_log.info("{} not whitelisted for addlistlim command".format(peer_ip))

                elif data == "aliasget":  # all for a single address, no protection against overlapping
                    # if (peer_ip in allowed or "any" in allowed):
                    if peers.is_allowed(peer_ip, data):
                        aliases.aliases_update (index_db, ledger_path_conf, "normal", app_log)

                        alias_address = str(connections.receive(self.request, 10))

                        execute_param(index_cursor, ("SELECT alias FROM aliases WHERE address = ? "), (alias_address,))

                        result = index_cursor.fetchall()

                        if not result:
                            result = [[alias_address]]

                        connections.send(self.request, result, 10)
                    else:
                        app_log.info("{} not whitelisted for aliasget command".format(peer_ip))


                elif data == "aliasesget":  # only gets the first one, for multiple addresses
                    # if (peer_ip in allowed or "any" in allowed):
                    if peers.is_allowed(peer_ip, data):
                        aliases.aliases_update (index_db, ledger_path_conf, "normal", app_log)

                        aliases_request = list(connections.receive(self.request, 10))

                        results = []
                        for alias_address in aliases_request:
                            execute_param (index_cursor, ("SELECT alias FROM aliases WHERE address = ? ORDER BY block_height ASC LIMIT 1"), (alias_address,))
                            try:
                                result = index_cursor.fetchall()[0][0]
                            except:
                                result = alias_address
                            results.append(result)
                            print(result)

                        connections.send(self.request, results, 10)
                    else:
                        app_log.info("{} not whitelisted for aliasesget command".format(peer_ip))

                elif data == "tokensget":
                    # if (peer_ip in allowed or "any" in allowed):

                    if peers.is_allowed(peer_ip, data):
                        tokens.tokens_update (index_db, ledger_path_conf, "normal", app_log)
                        tokens_address = str(connections.receive(self.request, 10))

                        index_cursor.execute ("SELECT DISTINCT token FROM tokens WHERE address OR recipient = ?", (tokens_address,))
                        tokens_user = index_cursor.fetchall ()

                        tokens_list = []
                        for token in tokens_user:
                            token = token[0]
                            index_cursor.execute ("SELECT sum(amount) FROM tokens WHERE recipient = ? AND token = ?;", (tokens_address,) + (token,))
                            credit = index_cursor.fetchone ()[0]
                            index_cursor.execute ("SELECT sum(amount) FROM tokens WHERE address = ? AND token = ?;", (tokens_address,) + (token,))
                            debit = index_cursor.fetchone ()[0]

                            debit = 0 if debit is None else debit
                            credit = 0 if credit is None else credit

                            balance = str(Decimal (credit) - Decimal (debit))

                            tokens_list.append((token,balance))

                        connections.send(self.request, tokens_list, 10)
                    else:
                        app_log.info("{} not whitelisted for tokensget command".format(peer_ip))

                elif data == "addfromalias":
                    if peers.is_allowed(peer_ip, data):

                        aliases.aliases_update(index_db, ledger_path_conf, "normal", app_log)


                        alias_address = str(connections.receive(self.request, 10))
                        index_cursor.execute("SELECT address FROM aliases WHERE alias = ? ORDER BY block_height ASC LIMIT 1;", (alias_address,))  # asc for first entry
                        try:
                            address_fetch = index_cursor.fetchone()[0]
                        except:
                            address_fetch = "No alias"
                        app_log.warning("Fetched the following alias address: {}".format(address_fetch))

                        connections.send(self.request, address_fetch, 10)

                        ali.close()

                    else:
                        app_log.info("{} not whitelisted for addfromalias command".format(peer_ip))

                elif data == "pubkeyget":
                    # if (peer_ip in allowed or "any" in allowed):
                    if peers.is_allowed(peer_ip, data):
                        pub_key_address = str(connections.receive(self.request, 10))

                        c.execute("SELECT public_key FROM transactions WHERE address = ? and reward = 0", (pub_key_address,))
                        target_public_key_hashed = c.fetchone()[0]
                        connections.send(self.request, target_public_key_hashed, 10)

                    else:
                        app_log.info("{} not whitelisted for pubkeyget command".format(peer_ip))



                elif data == "aliascheck":
                    # if (peer_ip in allowed or "any" in allowed):
                    if peers.is_allowed(peer_ip, data):
                        reg_string = str(connections.receive(self.request, 10))

                        registered_pending = mp.MEMPOOL.fetchone(
                            "SELECT timestamp FROM transactions WHERE openfield = ?;",
                            ("alias=" + reg_string, ))

                        h3.execute("SELECT timestamp FROM transactions WHERE openfield = ?;", ("alias=" + reg_string,))
                        registered_already = h3.fetchone()

                        if registered_already is None and registered_pending is None:
                            connections.send(self.request, "Alias free", 10)
                        else:
                            connections.send(self.request, "Alias registered", 10)
                    else:
                        app_log.info("{} not whitelisted for aliascheck command".format(peer_ip))


                elif data == "txsend":
                    # if (peer_ip in allowed or "any" in allowed):
                    if peers.is_allowed(peer_ip, data):
                        tx_remote = list(connections.receive(self.request, 10))

                        # receive data necessary for remote tx construction
                        remote_tx_timestamp = tx_remote[0]
                        remote_tx_privkey = tx_remote[1]
                        remote_tx_recipient = tx_remote[2]
                        remote_tx_amount = tx_remote[3]
                        remote_tx_operation = tx_remote[4]
                        remote_tx_openfield = tx_remote[5]
                        # receive data necessary for remote tx construction

                        # derive remaining data
                        tx_remote_key = RSA.importKey(remote_tx_privkey)
                        remote_tx_pubkey = tx_remote_key.publickey().exportKey().decode("utf-8")

                        remote_tx_pubkey_hashed = base64.b64encode(remote_tx_pubkey.encode('utf-8')).decode("utf-8")

                        remote_tx_address = hashlib.sha224(remote_tx_pubkey.encode("utf-8")).hexdigest()
                        # derive remaining data

                        # construct tx
                        remote_tx = (str(remote_tx_timestamp), str(remote_tx_address), str(remote_tx_recipient), '%.8f' % quantize_eight(remote_tx_amount), str(remote_tx_operation), str(remote_tx_openfield))  # this is signed

                        remote_hash = SHA.new(str(remote_tx).encode("utf-8"))
                        remote_signer = PKCS1_v1_5.new(tx_remote_key)
                        remote_signature = remote_signer.sign(remote_hash)
                        remote_signature_enc = base64.b64encode(remote_signature).decode("utf-8")
                        # construct tx

                        # insert to mempool, where everything will be verified
                        mempool_data = ((str(remote_tx_timestamp), str(remote_tx_address), str(remote_tx_recipient), '%.8f' % quantize_eight(remote_tx_amount), str(remote_signature_enc), str(remote_tx_pubkey_hashed), str(remote_tx_operation), str(remote_tx_openfield)))

                        app_log.info(mp.MEMPOOL.merge(mempool_data, peer_ip, c, True, True))

                        connections.send(self.request, str(remote_signature_enc), 10)
                        # wipe variables
                        (tx_remote, remote_tx_privkey, tx_remote_key) = (None, None, None)
                    else:
                        app_log.info("{} not whitelisted for txsend command".format(peer_ip))

                # less important methods
                elif data == "addvalidate":
                    # if (peer_ip in allowed or "any" in allowed):
                    if peers.is_allowed(peer_ip, data):

                        address_to_validate = str(connections.receive(self.request, 10))
                        if essentials.address_validate(address_to_validate):
                            result = "valid"
                        else:
                            result = "invalid"

                        connections.send(self.request, result, 10)
                    else:
                        app_log.info("{} not whitelisted for addvalidate command".format(peer_ip))


                elif data == "annget":
                    # if (peer_ip in allowed or "any" in allowed):
                    if peers.is_allowed(peer_ip, data):

                        # with open(peerlist, "r") as peer_list:
                        #    peers_file = peer_list.read()
                        connections.send(self.request, ann_get(h3,genesis_conf), 10)
                    else:
                        app_log.info("{} not whitelisted for annget command".format(peer_ip))

                elif data == "annverget":
                    # if (peer_ip in allowed or "any" in allowed):
                    if peers.is_allowed(peer_ip, data):

                        # with open(peerlist, "r") as peer_list:
                        #    peers_file = peer_list.read()
                        connections.send(self.request, ann_ver_get(h3,genesis_conf), 10)

                    else:
                        app_log.info("{} not whitelisted for annget command".format(peer_ip))

                elif data == "peersget":
                    # if (peer_ip in allowed or "any" in allowed):
                    if peers.is_allowed(peer_ip, data):

                        # with open(peerlist, "r") as peer_list:
                        #    peers_file = peer_list.read()
                        connections.send(self.request, peers.peer_list(peerlist), 10)

                    else:
                        app_log.info("{} not whitelisted for peersget command".format(peer_ip))

                elif data == "statusget":
                    # if (peer_ip in allowed or "any" in allowed):
                    if peers.is_allowed(peer_ip, data):

                        nodes_count = peers.consensus_size
                        nodes_list = peers.peer_ip_list
                        threads_count = threading.active_count()
                        uptime = int(time.time() - startup_time)
                        diff = difficulty(c)

                        if reveal_address:
                            revealed_address = address
                        else:
                            revealed_address = "private"

                        connections.send(self.request, (revealed_address, nodes_count, nodes_list, threads_count, uptime, peers.consensus, peers.consensus_percentage, VERSION, diff), 10)

                    else:
                        app_log.info("{} not whitelisted for statusget command".format(peer_ip))


                elif data == "statusjson":
                    if peers.is_allowed(peer_ip, data):
                        uptime = int(time.time() - startup_time)
                        tempdiff = difficulty(c)
                        status = {"protocolversion": config.version_conf, "walletversion": VERSION, "testnet": peers.is_testnet,  # config data
                                  "blocks": last_block, "timeoffset": 0, "connections": peers.consensus_size, "difficulty": tempdiff[0],  # live status, bitcoind format
                                  "threads": threading.active_count(), "uptime": uptime, "consensus": peers.consensus, "consensus_percent": peers.consensus_percentage}  # extra data
                        connections.send(self.request, status, 10)
                    else:
                        app_log.info("{} not whitelisted for statusjson command".format(peer_ip))
                elif data[:4] == 'api_':
                    if peers.is_allowed(peer_ip, data):
                        try:
                            apihandler.dispatch(data, self.request, h3, peers)
                        except Exception as e:
                            print(e)

                elif data == "diffget":
                    # if (peer_ip in allowed or "any" in allowed):
                    if peers.is_allowed(peer_ip, data):
                        diff = difficulty(c)
                        connections.send(self.request, diff, 10)
                    else:
                        app_log.info("{} not whitelisted for diffget command".format(peer_ip))

                elif data == "difflast":
                    # if (peer_ip in allowed or "any" in allowed):
                    if peers.is_allowed(peer_ip, data):

                        execute(h3, ("SELECT block_height, difficulty FROM misc ORDER BY block_height DESC LIMIT 1"))
                        difflast = h3.fetchone()
                        connections.send(self.request, difflast, 10)
                    else:
                        app_log.info("{} not whitelisted for difflastget command".format(peer_ip))

                # elif data == "*":
                #    app_log.info(">> inbound sending ping to {}".format(peer_ip))
                #    connections.send(self.request, "ping", 10)

                # elif data == "ping":
                #    app_log.info(">> Inbound got ping from {}".format(peer_ip))

                else:
                    if data == '*':
                        raise ValueError("Broken pipe")
                    raise ValueError("Unexpected error, received: " + str(data)[:32]+' ...')


                if not time.time() <= timer_operation + timeout_operation:
                    timer_operation = time.time()  # reset timer
                # time.sleep(float(pause_conf))  # prevent cpu overload
                app_log.info("Server loop finished for {}".format(peer_ip))

            except Exception as e:
                app_log.info("Inbound: Lost connection to {}".format(peer_ip))
                app_log.info("Inbound: {}".format(e))

                # remove from consensus (connection from them)
                peers.consensus_remove(peer_ip)
                # remove from consensus (connection from them)
                if self.request:
                    self.request.close()

                if debug_conf == 1:
                    raise  # major debug client
                else:
                    return

            finally:
                # cleanup

                try:
                    if conn:
                        conn.close()
                except Exception as e:
                    app_log.info("Error closing conn {}".format(e))


# client thread
# if you "return" from the function, the exception code will node be executed and client thread will hang
def worker(HOST, PORT):
    global peers
    timeout_operation = 60  # timeout
    timer_operation = time.time()  # start counting

    try:
        this_client = (HOST + ":" + str(PORT))
        s = socks.socksocket()
        if tor_conf == 1:
            s.setproxy(socks.PROXY_TYPE_SOCKS5, "127.0.0.1", 9050)
        # s.setblocking(0)
        s.connect((HOST, PORT))
        app_log.info("Outbound: Connected to {}".format(this_client))

        # communication starter

        connections.send(s, "version", 10)
        connections.send(s, version, 10)

        data = connections.receive(s, 10)

        if (data == "ok"):
            app_log.info("Outbound: Node protocol version of {} matches our client".format(this_client))
        else:
            raise ValueError("Outbound: Node protocol version of {} mismatch".format(this_client))

        connections.send(s, "hello", 10)

        # communication starter

    except Exception as e:
        app_log.info("Could not connect to {}: {}".format(this_client, e))
        return  # can return here, because no lists are affected yet

    banned = 0
    peer_ip = s.getpeername()[0]
    if peers.is_banned(peer_ip):
        banned = 1
        s.close()
        app_log.warning("IP {} banned, disconnected".format(peer_ip))

    while banned == 0:
        try:
            if this_client not in peers.connection_pool:
                peers.append_client(this_client)
                app_log.info("Connected to {}".format(this_client))
                app_log.info("Current active pool: {}".format(peers.connection_pool))

            hdd2, h2 = db_h2_define()
            conn, c = db_c_define()

            if full_ledger == 1:
                hdd, h = db_h_define()
                h3 = h
            else:
                hdd, h = None, None
                h3 = h2

            data = connections.receive(s, 10)  # receive data, one and the only root point
            #print(data)

            if data == "peers":  # REWORK
                subdata = connections.receive(s, 10)
                peers.peersync(subdata)

            elif data == "sync":
                if not time.time() <= timer_operation + timeout_operation:
                    timer_operation = time.time()  # reset timer

                try:

                    global syncing

                    while len(syncing) >= 3:
                        time.sleep(int(pause_conf))

                    syncing.append(peer_ip)
                    # sync start

                    # send block height, receive block height
                    connections.send(s, "blockheight", 10)

                    execute(c, ('SELECT block_height FROM transactions ORDER BY block_height DESC LIMIT 1'))
                    db_block_height = c.fetchone()[0]

                    app_log.info("Outbound: Sending block height to compare: {}".format(db_block_height))
                    # append zeroes to get static length
                    connections.send(s, db_block_height, 10)

                    received_block_height = connections.receive(s, 10)  # receive node's block height
                    app_log.info("Outbound: Node {} is at block height: {}".format(peer_ip, received_block_height))

                    if int(received_block_height) < db_block_height:
                        app_log.warning("Outbound: We have a higher block ({}) than {} ({}), sending".format(db_block_height, peer_ip, received_block_height))

                        data = connections.receive(s, 10)  # receive client's last block_hash

                        # send all our followup hashes
                        app_log.info("Outbound: Will seek the following block: {}".format(data))

                        # consensus pool 2 (active connection)
                        consensus_blockheight = int(received_block_height)  # str int to remove leading zeros
                        peers.consensus_add(peer_ip, consensus_blockheight, s, last_block)
                        # consensus pool 2 (active connection)

                        try:
                            execute_param(h3, ("SELECT block_height FROM transactions WHERE block_hash = ?;"), (data,))
                            client_block = h3.fetchone()[0]

                            app_log.info("Outbound: Node is at block {}".format(client_block))  # now check if we have any newer

                            execute(h3, ('SELECT block_hash FROM transactions ORDER BY block_height DESC LIMIT 1'))
                            db_block_hash = h3.fetchone()[0]  # get latest block_hash

                            if db_block_hash == data:
                                app_log.info("Outbound: Node {} has the latest block".format(peer_ip))
                                connections.send(s, "nonewblk", 10)

                            else:
                                blocks_fetched = []
                                while len(str(blocks_fetched)) < 500000:  # limited size based on txs in blocks
                                    # execute_param(h3, ("SELECT block_height, timestamp,address,recipient,amount,signature,public_key,keep,openfield FROM transactions WHERE block_height > ? AND block_height <= ?;"),(str(int(client_block)),) + (str(int(client_block + 1)),))
                                    execute_param(h3, ("SELECT timestamp,address,recipient,amount,signature,public_key,cast(operation as TEXT),openfield FROM transactions WHERE block_height > ? AND block_height <= ?;"), (str(int(client_block)),str(int(client_block + 1)),))
                                    result = h3.fetchall()
                                    if not result:
                                        break
                                    blocks_fetched.extend([result])
                                    client_block = int(client_block) + 1

                                #blocks_send = [[l[1:] for l in group] for _, group in groupby(blocks_fetched, key=itemgetter(0))]  # remove block number

                                app_log.info("Outbound: Selected {}".format(blocks_fetched))

                                connections.send(s, "blocksfnd", 10)

                                confirmation = connections.receive(s, 10)

                                if confirmation == "blockscf":
                                    app_log.info("Outbound: Client confirmed they want to sync from us")
                                    connections.send(s, blocks_fetched, 10)

                                elif confirmation == "blocksrj":
                                    app_log.info("Outbound: Client rejected to sync from us because we're dont have the latest block")
                                    pass

                        except Exception as e:
                            app_log.warning("Outbound: Block {} of {} not found".format(data[:8], peer_ip))
                            connections.send(s, "blocknf", 10)
                            connections.send(s, data, 10)

                    elif int(received_block_height) >= db_block_height:
                        if int(received_block_height) == db_block_height:
                            app_log.info("Outbound: We have the same block as {} ({}), hash will be verified".format(peer_ip, received_block_height))
                        else:
                            app_log.warning("Outbound: We have a lower block ({}) than {} ({}), hash will be verified".format(db_block_height, peer_ip, received_block_height))

                        execute(c, ('SELECT block_hash FROM transactions ORDER BY block_height DESC LIMIT 1'))
                        db_block_hash = c.fetchone()[0]  # get latest block_hash

                        app_log.info("Outbound: block_hash to send: {}".format(db_block_hash))
                        connections.send(s, db_block_hash, 10)

                        # consensus pool 2 (active connection)
                        consensus_blockheight = int(received_block_height)  # str int to remove leading zeros
                        peers.consensus_add(peer_ip, consensus_blockheight, s, last_block)
                        # consensus pool 2 (active connection)

                except Exception as e:
                    app_log.info("Outbound: Sync failed {}".format(e))
                finally:
                    syncing.remove(peer_ip)

            elif data == "blocknf":  # one of the possible outcomes
                block_hash_delete = connections.receive(s, 10)
                # print peer_ip
                # if max(consensus_blockheight_list) == int(received_block_height):
                if int(received_block_height) == peers.consensus_max:
                    blocknf(block_hash_delete, peer_ip, conn, c, hdd, h, hdd2, h2)
                    if peers.warning(s, peer_ip, "Rollback", 2):
                        raise ValueError("{} is banned".format(peer_ip))

                sendsync(s, peer_ip, "Block not found", False)

            elif data == "blocksfnd":
                app_log.info("Outbound: Node {} has the block(s)".format(peer_ip))  # node should start sending txs in this step

                # app_log.info("Inbound: Combined segments: " + segments)
                # print peer_ip
                if db_lock.locked():
                    app_log.warning("Skipping sync from {}, syncing already in progress".format(peer_ip))

                else:
                    execute(c, "SELECT timestamp FROM transactions WHERE reward != 0 ORDER BY block_height DESC LIMIT 1;")  # or it takes the first
                    last_block_ago = Decimal(c.fetchone()[0])

                    if int(last_block_ago) < (time.time() - 600):
                        block_req = peers.consensus_most_common
                        app_log.warning("Most common block rule triggered")

                    else:
                        block_req = peers.consensus_max
                        app_log.warning("Longest chain rule triggered")

                    if int(received_block_height) >= block_req:
                        try:  # they claim to have the longest chain, things must go smooth or ban
                            app_log.warning("Confirming to sync from {}".format(peer_ip))

                            connections.send(s, "blockscf", 10)
                            segments = connections.receive(s, 10)

                        except:
                            if peers.warning(s, peer_ip, "Failed to deliver the longest chain", 10):
                                raise ValueError("{} is banned".format(peer_ip))

                        else:
                            digest_block(segments, s, peer_ip, conn, c, hdd, h, hdd2, h2, h3)

                            # receive theirs
                    else:
                        connections.send(s, "blocksrj", 10)
                        app_log.warning("Inbound: Distant peer {} is at {}, should be at least {}".format(peer_ip, received_block_height, block_req))

                sendsync(s, peer_ip, "Block found", True)

                # block_hash validation end

            elif data == "nonewblk":
                # send and receive mempool
                if mp.MEMPOOL.sendable (peer_ip):
                    mempool_txs = mp.MEMPOOL.tx_to_send (peer_ip)
                    # app_log.info("Outbound: Extracted from the mempool: " + str(mempool_txs))  # improve: sync based on signatures only
                    # if len(mempool_txs) > 0: #wont sync mempool until we send something, which is bad
                    # send own
                    connections.send (s, "mempool", 10)
                    connections.send (s, mempool_txs, 10)
                    # send own
                    # receive theirs
                    segments = connections.receive (s, 10)
                    app_log.info (mp.MEMPOOL.merge (segments, peer_ip, c, True))
                    # receive theirs
                    # Tell the mempool we just send our pool to a peer
                    mp.MEMPOOL.sent (peer_ip)
                sendsync (s, peer_ip, "No new block", True)

            else:
                if data == '*':
                    raise ValueError ("Broken pipe")
                raise ValueError ("Unexpected error, received: {}".format (str (data)[:32]))

        except Exception as e:
            # remove from active pool
            if this_client in peers.connection_pool:
                app_log.info("Will remove {} from active pool {}".format(this_client, peers.connection_pool))
                app_log.warning("Outbound: Disconnected from {}: {}".format(this_client, e))
                # temp

                # temp
                peers.remove_client(this_client)

            # remove from active pool

            # remove from consensus 2
            try:
                peers.consensus_remove(peer_ip)
            except:
                pass
            # remove from consensus 2

            app_log.info("Connection to {} terminated due to {}".format(this_client, e))
            app_log.info("---thread {} ended---".format(threading.currentThread()))

            # properly end the connection
            if s:
                s.close()
            # properly end the connection
            if debug_conf == 1:
                raise  # major debug client
            else:
                app_log.info("Ending thread, because {}".format(e))
                return

        finally:
            try:
                conn.close()
            except:
                pass


class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    pass


if __name__ == "__main__":
    app_log = log.log("node.log", debug_level_conf, terminal_output)
    app_log.warning("Configuration settings loaded")

    if os.path.exists("fresh_sync"):
        app_log.warning("Status: Fresh sync required, bootstrapping from the website")
        os.remove("fresh_sync")
        bootstrap()

    if "testnet" in version:  # overwrite for testnet
        port = 2829
        full_ledger = 0
        hyper_path_conf = "static/test.db"
        ledger_path_conf = "static/test.db" #for tokens
        ledger_ram_file = "file:ledger_testnet?mode=memory&cache=shared"
        hyper_recompress_conf = 0
        peerlist = "peers_test.txt"

        redownload_test = input("Status: Welcome to the testnet. Redownload test ledger? y/n")
        if redownload_test == "y" or not os.path.exists("static/test.db"):
            types = ['static/test.db-wal', 'static/test.db-shm']
            for type in types:
                for file in glob.glob(type):
                    os.remove(file)
                    print(file, "deleted")
            download_file("https://bismuth.cz/test.db", "static/test.db")
        else:
            print("Not redownloading test db")

    # TODO : move this to peers also.
    else:
        peerlist = "peers.txt"
        ledger_ram_file = "file:ledger?mode=memory&cache=shared"

    # UPDATE DB

    if "testnet" not in version:
        upgrade = sqlite3.connect (ledger_path_conf)
        u = upgrade.cursor()
        try:
            u.execute("PRAGMA table_info(transactions);")
            result = u.fetchall()[10][2]
            if result != "TEXT":
                raise ValueError("Database column type outdated for Command field")

            upgrade.close()

        except Exception as e:
            print (e)
            upgrade.close()
            print("Database needs upgrading, bootstrapping...")
            bootstrap()
    # UPDATE DB

    # This one too?
    global syncing
    syncing = []

    essentials.keys_check(app_log)
    essentials.db_check(app_log)

    # import keys
    # key = RSA.importKey(open('privkey.der').read())
    # private_key_readable = str(key.exportKey())
    _, public_key_readable, _, _, _, public_key_hashed, address = essentials.keys_load ("privkey.der", "pubkey.der")

    app_log.warning("Status: Local address: {}".format(address))

    check_integrity(hyper_path_conf)
    coherence_check()

    if "testnet" in version:
        index_db = "static/index_test.db"
    else:
        index_db = "static/index.db"

    app_log.warning("Status: Indexing tokens")
    tokens.tokens_update(index_db, ledger_path_conf, "normal", app_log)
    app_log.warning("Status: Indexing aliases")
    aliases.aliases_update(index_db, ledger_path_conf, "normal", app_log)

    ledger_compress(ledger_path_conf, hyper_path_conf)

    try:
        source_db = sqlite3.connect(hyper_path_conf, timeout=1)
        source_db.text_factory = str
        sc = source_db.cursor()

        sc.execute("SELECT block_height FROM transactions ORDER BY block_height DESC LIMIT 1")
        hdd_block = sc.fetchone()[0]

        if ram_conf == 1:
            app_log.warning("Status: Moving database to RAM")
            to_ram = sqlite3.connect(ledger_ram_file, uri=True, timeout=1, isolation_level=None)
            to_ram.text_factory = str
            tr = to_ram.cursor()

            query = "".join(line for line in source_db.iterdump())
            to_ram.executescript(query)
            # do not close
            app_log.warning("Status: Moved database to RAM")

    except Exception as e:
        app_log.error(e)
        raise

    # mempool, m = db_m_define()
    conn, c = db_c_define()
    hdd2, h2 = db_h2_define()
    if full_ledger == 1:
        hdd, h = db_h_define()
        h3 = h
    else:
        hdd, h = None, None
        h3 = h2

    # init

    ### LOCAL CHECKS FINISHED ###
    app_log.warning("Status: Starting...")
    global startup_time
    startup_time = time.time()

    try:
        if "testnet" in version:
            is_testnet = True
        else:
            is_testnet = False
        app_log.warning ("Testnet: {}".format (is_testnet))

        peers = peershandler.Peers(app_log, config)
        apihandler = apihandler.ApiHandler(app_log, config)
        mp.MEMPOOL = mp.Mempool(app_log, config, db_lock, is_testnet)

        if rebuild_db_conf == 1:
            db_maintenance(conn)
        # connectivity to self node

        if verify_conf == 1:
            verify(h3)

        if tor_conf == 0:
            # Port 0 means to select an arbitrary unused port
            HOST, PORT = "0.0.0.0", int(port)

            ThreadedTCPServer.allow_reuse_address = True
            ThreadedTCPServer.daemon_threads = True
            ThreadedTCPServer.timeout = 60
            ThreadedTCPServer.request_queue_size = 100

            server = ThreadedTCPServer((HOST, PORT), ThreadedTCPRequestHandler)
            ip, port = server.server_address

            # Start a thread with the server -- that thread will then start one
            # more thread for each request

            server_thread = threading.Thread(target=server.serve_forever)

            # Exit the server thread when the main thread terminates

            server_thread.daemon = True
            server_thread.start()
            app_log.warning("Status: Server loop running.")
        else:
            app_log.warning("Status: Not starting a local server to conceal identity on Tor network")

        # start connection manager
        t_manager = threading.Thread(target=manager(c))
        app_log.warning("Status: Starting connection manager")
        t_manager.daemon = True
        t_manager.start()
        # start connection manager

        # server.serve_forever() #added
        server.shutdown()
        server.server_close()
        mp.MEMPOOL.close()


    except Exception as e:
        app_log.info("Status: Node already running?")
        app_log.info(e)
        raise
sys.exit()