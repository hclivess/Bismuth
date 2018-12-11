# c = hyperblock in ram OR hyperblock file when running only hyperblocks
# h = ledger file
# h2 = hyperblock file
# h3 = ledger file OR hyperblock file when running only hyperblocks

# never remove the str() conversion in data evaluation or database inserts or you will debug for 14 days as signed types mismatch
# if you raise in the server thread, the server will die and node will stop
# never use codecs, they are bugged and do not provide proper serialization
# must unify node and client now that connections parameters are function parameters
# if you have a block of data and want to insert it into sqlite, you must use a single "commit" for the whole batch, it's 100x faster
# do not isolation_level=None/WAL hdd levels, it makes saving slow


VERSION = "4.2.8.1"  # 3. regnet support

# Bis specific modules
import log, options, connections, peershandler, apihandler

import shutil, socketserver, base64, hashlib, os, re, sqlite3, sys, threading, time, socks, random, keys, math, \
    requests, tarfile, essentials, glob
from hashlib import blake2b
import platform
import tokensv2 as tokens
import aliases
from quantizer import *
from ann import ann_get, ann_ver_get
from essentials import fee_calculate

from Cryptodome.Hash import SHA
from Cryptodome.PublicKey import RSA
from Cryptodome.Signature import PKCS1_v1_5

import mempool as mp
import plugins
import staking
import hyperlane
import mining
import mining_heavy3
import regnet
import classes

# load config

POW_FORK = 854660
FORK_AHEAD = 5
FORK_DIFF = 108.9

getcontext().rounding = ROUND_HALF_EVEN


db_lock = threading.Lock()
# mem_lock = threading.Lock()
# peersync_lock = threading.Lock()


from appdirs import *

appname = "Bismuth"
appauthor = "Bismuth Foundation"

# nodes_ban_reset=config.nodes_ban_reset

PEM_BEGIN = re.compile(r"\s*-----BEGIN (.*)-----\s+")
PEM_END = re.compile(r"-----END (.*)-----\s*$")


def limit_version():
    if 'mainnet0018' in node.version_allow:
        logger.app_log.warning(f"Beginning to reject mainnet0018 - block {node.last_block}")
        node.version_allow.remove('mainnet0018')


def tokens_rollback(height):
    """Rollback Token index

    :param height: height index of token in chain
    :param app_log: logger to use

    Simply deletes from the `tokens` table where the block_height is
    greater than or equal to the :param height: and logs the new height

    returns None
    """
    with sqlite3.connect(node.index_db) as tok:
        t = tok.cursor()
        execute_param(t, "DELETE FROM tokens WHERE block_height >= ?;", (height - 1,))
        commit(tok)
    logger.app_log.warning(f"Rolled back the token index to {(height - 1)}")


def staking_rollback(height):
    """Rollback staking index

    :param height: height index of token in chain
    :param app_log: logger to use

    Simply deletes from the `staking` table where the block_height is
    greater than or equal to the :param height: and logs the new height

    returns None
    """
    with sqlite3.connect(node.index_db) as sta:
        s = sta.cursor()
        execute_param(s, "DELETE FROM staking WHERE block_height >= ?;", (height - 1,))
        commit(sta)
    logger.app_log.warning(f"Rolled back the staking index to {(height - 1)}")


def aliases_rollback(height):
    """Rollback Alias index

    :param height: height index of token in chain
    :param app_log: logger to use

    Simply deletes from the `aliases` table where the block_height is
    greater than or equal to the :param height: and logs the new height

    returns None
    """
    with sqlite3.connect(node.index_db) as ali:
        a = ali.cursor()
        execute_param(a, "DELETE FROM aliases WHERE block_height >= ?;", (height - 1,))
        commit(ali)
    logger.app_log.warning(f"Rolled back the alias index to {(height - 1)}")


def sendsync(sdef, peer_ip, status, provider):
    """ Save peer_ip to peerlist and send `sendsync`

    :param sdef: socket object
    :param peer_ip: IP of peer synchronization has been completed with
    :param status: Status synchronization was completed in/as
    :param provider: <Documentation N/A>

    Log the synchronization status
    Save peer IP to peers list if applicable
    Wait for database to unlock
    Send `sendsync` command via socket `sdef`

    returns None
    """

    logger.app_log.info(f"Outbound: Synchronization with {peer_ip} finished after: {status}, sending new sync request")

    if provider:
        logger.app_log.info(f"Outbound: Saving peer {peer_ip}")
        node.peers.peers_save(peer_ip)

    time.sleep(Decimal(node.pause_conf))
    while db_lock.locked():
        if node.IS_STOPPING:
            return
        time.sleep(Decimal(node.pause_conf))

    connections.send(sdef, "sendsync")


def validate_pem(public_key):
    """ Validate PEM data against :param public key:

    :param public_key: public key to validate PEM against

    The PEM data is constructed by base64 decoding the public key
    Then, the data is tested against the PEM_BEGIN and PEM_END
    to ensure the `pem_data` is valid, thus validating the public key.

    returns None
    """
    # verify pem as cryptodome does
    pem_data = base64.b64decode(public_key).decode("utf-8")
    match = PEM_BEGIN.match(pem_data)
    if not match:
        raise ValueError("Not a valid PEM pre boundary")

    marker = match.group(1)

    match = PEM_END.search(pem_data)
    if not match or match.group(1) != marker:
        raise ValueError("Not a valid PEM post boundary")
        # verify pem as cryptodome does


def download_file(url, filename):
    """Download a file from URL to filename

    :param url: URL to download file from
    :param filename: Filename to save downloaded data as

    returns `filename`
    """
    try:
        r = requests.get(url, stream=True)
        total_size = int(r.headers.get('content-length')) / 1024

        with open(filename, 'wb') as filename:
            chunkno = 0
            for chunk in r.iter_content(chunk_size=1024):
                if chunk:
                    chunkno = chunkno + 1
                    if chunkno % 10000 == 0:  # every x chunks
                        print(f"Downloaded {int(100 * ((chunkno) / total_size))} %")

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
        for t in types:
            for f in glob.glob(t):
                os.remove(f)
                print(f, "deleted")

        archive_path = node.ledger_path_conf + ".tar.gz"
        download_file("https://bismuth.cz/ledger.tar.gz", archive_path)

        with tarfile.open(archive_path) as tar:
            tar.extractall("static/")  # NOT COMPATIBLE WITH CUSTOM PATH CONFS

    except:
        logger.app_log.warning("Something went wrong during bootstrapping, aborted")
        raise


def check_integrity(database):
    # check ledger integrity
    with sqlite3.connect(database) as ledger_check:
        ledger_check.text_factory = str
        l = ledger_check.cursor()

        try:
            l.execute("PRAGMA table_info('transactions')")
            redownload = False
        except:
            redownload = True

        if len(l.fetchall()) != 12:
            logger.app_log.warning(
                f"Status: Integrity check on database {database} failed, bootstrapping from the website")
            redownload = True

    if redownload and node.is_mainnet:
        bootstrap()


def percentage(percent, whole):
    return Decimal(percent) * Decimal(whole) / 100


def db_to_drive(hdd, h, hdd2, h2):

    logger.app_log.warning("Block: Moving new data to HDD")
    try:
        if node.ram_conf:  # select RAM as source database
            source_db = sqlite3.connect(node.ledger_ram_file, uri=True, timeout=1)
        else:  # select hyper.db as source database
            source_db = sqlite3.connect(node.hyper_path_conf, timeout=1)

        source_db.text_factory = str
        sc = source_db.cursor()

        execute_param(sc, (
            "SELECT * FROM transactions WHERE block_height > ? OR block_height < ? ORDER BY block_height ASC"),
                      (node.hdd_block, -node.hdd_block))
        result1 = sc.fetchall()

        if node.full_ledger:  # we want to save to ledger.db
            for x in result1:
                h.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                          (x[0], x[1], x[2], x[3], x[4], x[5], x[6], x[7], x[8], x[9], x[10], x[11]))
            commit(hdd)

        if node.ram_conf:  # we want to save to hyper.db from RAM/hyper.db depending on ram conf
            for x in result1:
                h2.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                           (x[0], x[1], x[2], x[3], x[4], x[5], x[6], x[7], x[8], x[9], x[10], x[11]))
            commit(hdd2)

        execute_param(sc, ("SELECT * FROM misc WHERE block_height > ? ORDER BY block_height ASC"), (node.hdd_block,))
        result2 = sc.fetchall()

        if node.full_ledger:  # we want to save to ledger.db from RAM/hyper.db depending on ram conf
            for x in result2:
                h.execute("INSERT INTO misc VALUES (?,?)", (x[0], x[1]))
            commit(hdd)

        if node.ram_conf:  # we want to save to hyper.db from RAM
            for x in result2:
                h2.execute("INSERT INTO misc VALUES (?,?)", (x[0], x[1]))
            commit(hdd2)

        h2.execute("SELECT max(block_height) FROM transactions")
        node.hdd_block = h2.fetchone()[0]
        logger.app_log.warning(f"Block: {len(result1)} txs moved to HDD")
    except Exception as e:
        logger.app_log.warning(f"Block: Exception Moving new data to HDD: {e}")
        # app_log.warning("Ledger digestion ended")  # dup with more informative digest_block notice.


def db_define(object):
    object.index = sqlite3.connect(node.index_db, timeout=1, check_same_thread=False)
    object.index.text_factory = str
    object.index.execute("PRAGMA page_size = 4096;")
    object.index_cursor = object.index.cursor()

    object.hdd = sqlite3.connect(node.ledger_path_conf, timeout=1, check_same_thread=False)
    object.hdd.text_factory = str
    object.hdd.execute("PRAGMA page_size = 4096;")
    object.h = object.hdd.cursor()

    object.hdd2 = sqlite3.connect(node.hyper_path_conf, timeout=1, check_same_thread=False)
    object.hdd2.text_factory = str
    object.hdd2.execute("PRAGMA page_size = 4096;")
    object.h2 = object.hdd2.cursor()

    if node.full_ledger:
        object.h3 = object.h
    else:
        object.h3 = object.h2


    try:
        if node.ram_conf:
            object.conn = sqlite3.connect(node.ledger_ram_file, uri=True, isolation_level=None,
                                            check_same_thread=False)
        else:
            object.conn = sqlite3.connect(node.hyper_path_conf, uri=True, isolation_level=None,
                                            check_same_thread=False)

        object.conn.execute('PRAGMA journal_mode = WAL;')
        object.conn.execute("PRAGMA page_size = 4096;")
        object.conn.text_factory = str
        object.c = object.conn.cursor()

    except Exception as e:
        logger.app_log.info(e)



def ledger_compress():
    local_ledger_path_conf = node.ledger_path_conf
    local_hyper_path_conf = node.hyper_path_conf

    """conversion of normal blocks into hyperblocks from ledger.db or hyper.db to hyper.db"""
    try:

        # if os.path.exists(node.hyper_path_conf+".temp"):
        #    os.remove(node.hyper_path_conf+".temp")
        #    logger.app_log.warning("Status: Removed old temporary hyperblock file")
        #    time.sleep(100)

        if os.path.exists(node.hyper_path_conf):

            if node.full_ledger:
                # cross-integrity check
                local_hdd = sqlite3.connect(node.ledger_path_conf, timeout=1)
                local_hdd.text_factory = str
                local_h = local_hdd.cursor()
                local_h.execute("SELECT max(block_height) FROM transactions")
                local_hdd_block_last = local_h.fetchone()[0]
                local_hdd.close()

                local_hdd2 = sqlite3.connect(node.hyper_path_conf, timeout=1)
                local_hdd2.text_factory = str
                local_h2 = local_hdd2.cursor()
                local_h2.execute("SELECT max(block_height) FROM transactions")
                local_hdd2_block_last = local_h2.fetchone()[0]
                local_hdd2.close()
                # cross-integrity check

                if local_hdd_block_last == local_hdd2_block_last and node.hyper_recompress_conf:  # cross-integrity check
                    local_ledger_path_conf = local_hyper_path_conf  # only valid within the function, this temporarily sets hyper.db as source
                    logger.app_log.warning("Status: Recompressing hyperblocks (keeping full ledger)")
                    recompress = True
                elif local_hdd_block_last == local_hdd2_block_last and not node.hyper_recompress_conf:
                    logger.app_log.warning("Status: Hyperblock recompression skipped")
                    recompress = False
                else:
                    logger.app_log.warning(
                        f"Status: Cross-integrity check failed {local_hdd_block_last} not equal to {local_hdd2_block_last}, hyperblocks will be rebuilt from full ledger")
                    recompress = True
            else:
                if node.hyper_recompress_conf:
                    logger.app_log.warning("Status: Recompressing hyperblocks (without full ledger)")
                    recompress = True
                else:
                    logger.app_log.warning("Status: Hyperblock recompression skipped")
                    recompress = False

        else:
            logger.app_log.warning("Status: Compressing ledger to Hyperblocks")
            recompress = True

        if recompress:
            depth = 15000  # REWORK TO REFLECT TIME INSTEAD OF BLOCKS

            if node.full_ledger:
                shutil.copy(local_ledger_path_conf, local_ledger_path_conf + '.temp')
                hyper = sqlite3.connect(local_ledger_path_conf + '.temp')
            else:
                shutil.copy(local_hyper_path_conf, local_ledger_path_conf + '.temp')
                hyper = sqlite3.connect(local_ledger_path_conf + '.temp')

            hyper.text_factory = str
            hyp = hyper.cursor()

            addresses = []

            hyp.execute("UPDATE transactions SET address = 'Hypoblock' WHERE address = 'Hyperblock'")

            hyp.execute("SELECT max(block_height) FROM transactions")
            db_block_height = int(hyp.fetchone()[0])
            depth_specific = db_block_height - depth

            hyp.execute(
                "SELECT distinct(recipient) FROM transactions WHERE (block_height < ? AND block_height > ?) ORDER BY block_height;",
                (depth_specific, -depth_specific,))  # new addresses will be ignored until depth passed
            unique_addressess = hyp.fetchall()

            for x in set(unique_addressess):
                credit = Decimal("0")
                for entry in hyp.execute(
                        "SELECT amount,reward FROM transactions WHERE recipient = ? AND (block_height < ? AND block_height > ?);",
                        (x[0],) + (depth_specific, -depth_specific,)):
                    try:
                        credit = quantize_eight(credit) + quantize_eight(entry[0]) + quantize_eight(entry[1])
                        credit = 0 if credit is None else credit
                    except Exception as e:
                        credit = 0

                debit = Decimal("0")
                for entry in hyp.execute(
                        "SELECT amount,fee FROM transactions WHERE address = ? AND (block_height < ? AND block_height > ?);",
                        (x[0],) + (depth_specific, -depth_specific,)):
                    try:
                        debit = quantize_eight(debit) + quantize_eight(entry[0]) + quantize_eight(entry[1])
                        debit = 0 if debit is None else debit
                    except Exception as e:
                        debit = 0

                end_balance = quantize_eight(credit - debit)

                # logger.app_log.info("Address: "+ str(x))
                # logger.app_log.info("Credit: " + str(credit))
                # logger.app_log.info("Debit: " + str(debit))
                # logger.app_log.info("Fees: " + str(fees))
                # logger.app_log.info("Rewards: " + str(rewards))
                # logger.app_log.info("Balance: " + str(end_balance))

                # print(x[0],end_balance)

                if end_balance > 0:
                    timestamp = str(time.time())
                    hyp.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
                        depth_specific - 1, timestamp, "Hyperblock", x[0], str(end_balance), "0", "0", "0", "0",
                        "0", "0", "0"))
            hyper.commit()

            hyp.execute(
                "DELETE FROM transactions WHERE address != 'Hyperblock' AND (block_height < ? AND block_height > ?);",
                (depth_specific, -depth_specific,))
            hyper.commit()

            hyp.execute("DELETE FROM misc WHERE (block_height < ? AND block_height > ?);",
                        (depth_specific, -depth_specific,))  # remove diff calc
            hyper.commit()

            hyp.execute("VACUUM")
            hyper.close()

            if os.path.exists(node.hyper_path_conf):
                os.remove(node.hyper_path_conf)  # remove the old hyperblocks

            os.rename(local_ledger_path_conf + '.temp', local_hyper_path_conf)

        if node.full_ledger == 0 and os.path.exists(local_ledger_path_conf) and node.is_mainnet:
            os.remove(local_ledger_path_conf)
            logger.app_log.warning("Removed full ledger and only kept hyperblocks")

    except Exception as e:
        raise ValueError(f"There was an issue converting to Hyperblocks: {e}")


def most_common(lst):
    return max(set(lst), key=lst.count)


def bin_convert(string):
    return ''.join(format(ord(x), '8b').replace(' ', '0') for x in string)


def commit(cursor):
    """Secure commit for slow nodes"""
    while not node.IS_STOPPING:
        try:
            cursor.commit()
            break
        except Exception as e:
            logger.app_log.warning(f"Database cursor: {cursor}")
            logger.app_log.warning(f"Database retry reason: {e}")
            time.sleep(0.1)

def execute(cursor, query):
    """Secure execute for slow nodes"""
    while not node.IS_STOPPING:
        try:
            cursor.execute(query)
            break
        except sqlite3.InterfaceError as e:
            logger.app_log.warning(f"Database query to abort: {cursor} {query}")
            logger.app_log.warning(f"Database abortion reason: {e}")
            break
        except sqlite3.IntegrityError as e:
            logger.app_log.warning(f"Database query to abort: {cursor} {query}")
            logger.app_log.warning(f"Database abortion reason: {e}")
            break
        except Exception as e:
            logger.app_log.warning(f"Database query: {cursor} {query}")
            logger.app_log.warning(f"Database retry reason: {e}")
            time.sleep(1)
    return cursor


def execute_param(cursor, query, param):
    """Secure execute w/ param for slow nodes"""
    while not node.IS_STOPPING:
        try:
            cursor.execute(query, param)
            break
        except sqlite3.InterfaceError as e:
            logger.app_log.warning(f"Database query to abort: {cursor} {query} {param}")
            logger.app_log.warning(f"Database abortion reason: {e}")
            break
        except sqlite3.IntegrityError as e:
            logger.app_log.warning(f"Database query to abort: {cursor} {query}")
            logger.app_log.warning(f"Database abortion reason: {e}")
            break
        except Exception as e:
            logger.app_log.warning(f"Database query: {cursor} {query} {param}")
            logger.app_log.warning(f"Database retry reason: {e}")
            time.sleep(1)
    return cursor


def difficulty(c):
    execute(c, "SELECT * FROM transactions WHERE reward != 0 ORDER BY block_height DESC LIMIT 2")
    result = c.fetchone()

    timestamp_last = Decimal(result[1])
    block_height = int(result[0])

    previous = c.fetchone()

    # Failsafe for regtest starting at block 1}
    timestamp_before_last = timestamp_last if previous is None else Decimal(previous[1])

    execute_param(c, (
        "SELECT timestamp FROM transactions WHERE CAST(block_height AS INTEGER) > ? AND reward != 0 ORDER BY timestamp ASC LIMIT 2"),
                  (block_height - 1441,))
    timestamp_1441 = Decimal(c.fetchone()[0])
    block_time_prev = (timestamp_before_last - timestamp_1441) / 1440
    temp = c.fetchone()
    timestamp_1440 = timestamp_1441 if temp is None else Decimal(temp[0])
    block_time = Decimal(timestamp_last - timestamp_1440) / 1440
    execute(c, ("SELECT difficulty FROM misc ORDER BY block_height DESC LIMIT 1"))
    diff_block_previous = Decimal(c.fetchone()[0])

    time_to_generate = timestamp_last - timestamp_before_last

    if node.is_regnet:
        return (float('%.10f' % regnet.REGNET_DIFF), float('%.10f' % (regnet.REGNET_DIFF - 8)), float(time_to_generate),
                float(regnet.REGNET_DIFF), float(block_time), float(0), float(0), block_height)

    hashrate = pow(2, diff_block_previous / Decimal(2.0)) / (
            block_time * math.ceil(28 - diff_block_previous / Decimal(16.0)))
    # Calculate new difficulty for desired blocktime of 60 seconds
    target = Decimal(60.00)
    ##D0 = diff_block_previous
    difficulty_new = Decimal(
        (2 / math.log(2)) * math.log(hashrate * target * math.ceil(28 - diff_block_previous / Decimal(16.0))))
    # Feedback controller
    Kd = 10
    difficulty_new = difficulty_new - Kd * (block_time - block_time_prev)
    diff_adjustment = (difficulty_new - diff_block_previous) / 720  # reduce by factor of 720

    if diff_adjustment > Decimal(1.0):
        diff_adjustment = Decimal(1.0)

    difficulty_new_adjusted = quantize_ten(diff_block_previous + diff_adjustment)
    difficulty = difficulty_new_adjusted

    if node.is_mainnet:
        if block_height == POW_FORK - FORK_AHEAD:
            limit_version()
        if block_height == POW_FORK - 1:
            difficulty = FORK_DIFF
        if block_height == POW_FORK:
            difficulty = FORK_DIFF
            # Remove mainnet0018 from allowed
            limit_version()
            # disconnect our outgoing connections

    diff_drop_time = Decimal(180)

    if Decimal(time.time()) > Decimal(timestamp_last) + Decimal(2 * diff_drop_time):
        # Emergency diff drop
        time_difference = quantize_two(time.time()) - quantize_two(timestamp_last)
        diff_dropped = quantize_ten(difficulty) - quantize_ten(1) \
                       - quantize_ten(10 * (time_difference - 2 * diff_drop_time) / diff_drop_time)
    elif Decimal(time.time()) > Decimal(timestamp_last) + Decimal(diff_drop_time):
        time_difference = quantize_two(time.time()) - quantize_two(timestamp_last)
        diff_dropped = quantize_ten(difficulty) + quantize_ten(1) - quantize_ten(time_difference / diff_drop_time)
    else:
        diff_dropped = difficulty

    if difficulty < 50:
        difficulty = 50
    if diff_dropped < 50:
        diff_dropped = 50

    return (
        float('%.10f' % difficulty), float('%.10f' % diff_dropped), float(time_to_generate), float(diff_block_previous),
        float(block_time), float(hashrate), float(diff_adjustment),
        block_height)  # need to keep float here for database inserts support


def balanceget(balance_address, c):
    # verify balance

    # logger.app_log.info("Mempool: Verifying balance")
    # logger.app_log.info("Mempool: Received address: " + str(balance_address))

    base_mempool = mp.MEMPOOL.fetchall("SELECT amount, openfield, operation FROM transactions WHERE address = ?;",
                                       (balance_address,))

    # include mempool fees

    debit_mempool = 0
    if base_mempool:
        for x in base_mempool:
            debit_tx = Decimal(x[0])
            fee = fee_calculate(x[1], x[2], node.last_block)
            debit_mempool = quantize_eight(debit_mempool + debit_tx + fee)
    else:
        debit_mempool = 0
    # include mempool fees

    credit_ledger = Decimal("0")
    for entry in execute_param(c, ("SELECT amount FROM transactions WHERE recipient = ?;"), (balance_address,)):
        try:
            credit_ledger = quantize_eight(credit_ledger) + quantize_eight(entry[0])
            credit_ledger = 0 if credit_ledger is None else credit_ledger
        except:
            credit_ledger = 0

    fees = Decimal("0")
    debit_ledger = Decimal("0")

    for entry in execute_param(c, ("SELECT fee, amount FROM transactions WHERE address = ?;"), (balance_address,)):
        try:
            fees = quantize_eight(fees) + quantize_eight(entry[0])
            fees = 0 if fees is None else fees
        except:
            fees = 0

        try:
            debit_ledger = debit_ledger + Decimal(entry[1])
            debit_ledger = 0 if debit_ledger is None else debit_ledger
        except:
            debit_ledger = 0

    debit = quantize_eight(debit_ledger + debit_mempool)

    rewards = Decimal("0")
    for entry in execute_param(c, ("SELECT reward FROM transactions WHERE recipient = ?;"), (balance_address,)):
        try:
            rewards = quantize_eight(rewards) + quantize_eight(entry[0])
            rewards = 0 if rewards is None else rewards
        except:
            rewards = 0

    balance = quantize_eight(credit_ledger - debit - fees + rewards)
    balance_no_mempool = float(credit_ledger) - float(debit_ledger) - float(fees) + float(rewards)
    # logger.app_log.info("Mempool: Projected transction address balance: " + str(balance))
    return str(balance), str(credit_ledger), str(debit), str(fees), str(rewards), str(balance_no_mempool)

def blocknf(block_hash_delete, peer_ip, c, conn, h, hdd, h2, hdd2):
    my_time = time.time()

    if not db_lock.locked():
        db_lock.acquire()
        backup_data = None  # used in "finally" section
        skip = False
        reason = ""

        try:
            execute(c, 'SELECT * FROM transactions ORDER BY block_height DESC LIMIT 1')
            results = c.fetchone()
            db_block_height = results[0]
            db_block_hash = results[7]

            ip = {'ip': peer_ip}
            node.plugin_manager.execute_filter_hook('filter_rollback_ip', ip)
            if ip['ip'] == 'no':
                reason = "Filter blocked this rollback"
                skip = True

            elif db_block_height < 2:
                reason = "Will not roll back this block"
                skip = True

            elif db_block_hash != block_hash_delete:
                # print db_block_hash
                # print block_hash_delete
                reason = "We moved away from the block to rollback, skipping"
                skip = True

            else:
                # backup

                execute_param(c, "SELECT * FROM transactions WHERE block_height >= ?;", (db_block_height,))
                backup_data = c.fetchall()
                # this code continues at the bottom because of ledger presence check

                # delete followups
                execute_param(c, "DELETE FROM transactions WHERE block_height >= ? OR block_height <= ?",
                              (db_block_height, -db_block_height))
                commit(conn)

                execute_param(c, "DELETE FROM misc WHERE block_height >= ?;", (str(db_block_height),))
                commit(conn)

                # execute_param(c, ('DELETE FROM transactions WHERE address = "Development Reward" AND block_height <= ?'), (-db_block_height,))
                # commit(conn)

                logger.app_log.warning(f"Node {peer_ip} didn't find block {db_block_height}({db_block_hash})")

                # roll back hdd too
                if node.full_ledger:  # rollback ledger.db
                    execute_param(h, "DELETE FROM transactions WHERE block_height >= ? OR block_height <= ?",
                                  (db_block_height, -db_block_height))
                    commit(hdd)
                    execute_param(h, "DELETE FROM misc WHERE block_height >= ?;", (str(db_block_height),))
                    commit(hdd)

                if node.ram_conf:  # rollback hyper.db
                    execute_param(h2, "DELETE FROM transactions WHERE block_height >= ? OR block_height <= ?",
                                  (db_block_height, -db_block_height))
                    commit(hdd2)
                    execute_param(h2, "DELETE FROM misc WHERE block_height >= ?;", (str(db_block_height),))
                    commit(hdd2)

                node.hdd_block = int(db_block_height) - 1
                # /roll back hdd too

                # rollback indices
                tokens_rollback(db_block_height)
                aliases_rollback(db_block_height)
                staking_rollback(db_block_height)
                # /rollback indices

        except Exception as e:
            logger.app_log.info(e)

        finally:
            db_lock.release()
            if skip:
                rollback = {"timestamp": my_time, "height": db_block_height, "ip": peer_ip,
                            "hash": db_block_hash, "skipped": True, "reason": reason}
                node.plugin_manager.execute_action_hook('rollback', rollback)
                logger.app_log.info(f"Skipping rollback: {reason}")
            else:
                try:
                    nb_tx = 0
                    for tx in backup_data:
                        tx_short = f"{tx[1]} - {tx[2]} to {tx[3]}: {tx[4]} ({tx[11]})"
                        if tx[9] == 0:
                            try:
                                nb_tx += 1
                                logger.app_log.info(
                                    mp.MEMPOOL.merge((tx[1], tx[2], tx[3], tx[4], tx[5], tx[6], tx[10], tx[11]),
                                                     peer_ip, c, False,
                                                     revert=True))  # will get stuck if you change it to respect db_lock
                                logger.app_log.warning(f"Moved tx back to mempool: {tx_short}")
                            except Exception as e:
                                logger.app_log.warning(f"Error during moving tx back to mempool: {e}")
                        else:
                            # It's the coinbase tx, so we get the miner address
                            miner = tx[3]
                            height = tx[0]
                    rollback = {"timestamp": my_time, "height": height, "ip": peer_ip, "miner": miner,
                                "hash": db_block_hash, "tx_count": nb_tx, "skipped": False, "reason": ""}
                    node.plugin_manager.execute_action_hook('rollback', rollback)

                except Exception as e:
                    logger.app_log.warning(f"Error during moving txs back to mempool: {e}")

    else:
        reason = "Skipping rollback, other ledger operation in progress"
        rollback = {"timestamp": my_time, "ip": peer_ip, "skipped": True, "reason": reason}
        node.plugin_manager.execute_action_hook('rollback', rollback)
        logger.app_log.info(reason)


def manager(c):
    # moved to peershandler
    # reset_time = node.startup_time
    # peers_test("peers.txt")
    # peers_test("suggested_peers.txt")

    until_purge = 0

    while not node.IS_STOPPING:
        # dict_keys = peer_dict.keys()
        # random.shuffle(peer_dict.items())
        if until_purge == 0:
            # will purge once at start, then about every hour (120 * 30 sec)
            mp.MEMPOOL.purge()
            until_purge = 120

        until_purge -= 1

        # peer management
        if not node.is_regnet:
            # regnet never tries to connect
            node.peers.manager_loop(target=worker)

        logger.app_log.warning(f"Status: Threads at {threading.active_count()} / {node.thread_limit_conf}")
        logger.app_log.info(f"Status: Syncing nodes: {node.syncing}")
        logger.app_log.info(f"Status: Syncing nodes: {len(node.syncing)}/3")

        # Status display for Peers related info
        node.peers.status_log()
        mp.MEMPOOL.status()

        # last block
        execute(c,
                "SELECT block_height, timestamp FROM transactions WHERE reward != 0 ORDER BY block_height DESC LIMIT 1;")  # or it takes the first
        result = c.fetchall()[0]
        node.last_block = result[0]
        node.last_block_ago = int(time.time() - result[1])
        logger.app_log.warning(f"Status: Last block {node.last_block} was generated {'%.2f' % (node.last_block_ago / 60)} minutes ago")
        # last block
        # status Hook
        uptime = int(time.time() - node.startup_time)
        tempdiff = difficulty(c)  # Can we avoid recalc that ?
        status = {"protocolversion": node.version, "walletversion": VERSION, "testnet": node.is_testnet,
                  # config data
                  "blocks": node.last_block, "timeoffset": 0, "connections": node.peers.consensus_size,
                  "difficulty": tempdiff[0],  # live status, bitcoind format
                  "threads": threading.active_count(), "uptime": uptime, "consensus": node.peers.consensus,
                  "consensus_percent": node.peers.consensus_percentage,
                  "node.last_block_ago": node.last_block_ago}  # extra data
        if node.is_regnet:
            status['regnet'] = True
        node.plugin_manager.execute_action_hook('status', status)
        # end status hook

        if node.peerlist:  # if it is not empty
            try:
                node.peers.peers_dump(node.peerlist, node.peers.peer_dict)
            except Exception as e:
                logger.app_log.warning(f"There was an issue saving peers ({e}), skipped")
                pass

        # logger.app_log.info(threading.enumerate() all threads)
        for i in range(30):
            # faster stop
            if not node.IS_STOPPING:
                time.sleep(1)

def ledger_balance3(address, cache, c):
    # Many heavy blocks are pool payouts, same address.
    # Cache pre_balance instead of recalc for every tx
    if address in cache:
        return cache[address]
    credit_ledger = Decimal(0)
    for entry in execute_param(c, "SELECT amount, reward FROM transactions WHERE recipient = ?;", (address,)):
        credit_ledger += quantize_eight(entry[0]) + quantize_eight(entry[1])

    debit_ledger = Decimal(0)
    for entry in execute_param(c, "SELECT amount, fee FROM transactions WHERE address = ?;", (address,)):
        debit_ledger += quantize_eight(entry[0]) + quantize_eight(entry[1])

    cache[address] = quantize_eight(credit_ledger - debit_ledger)
    return cache[address]


def digest_block(data, sdef, peer_ip, conn, c, hdd, h, hdd2, h2, h3, index, index_cursor):
    block_height_new = node.last_block + 1  # for logging purposes.
    block_hash = 'N/A'
    failed_cause = ''
    block_count = 0
    tx_count = 0

    if node.peers.is_banned(peer_ip):
        # no need to loose any time with banned peers
        raise ValueError("Cannot accept blocks from a banned peer")
        # since we raise, it will also drop the connection, it's fine since he's banned.

    if not db_lock.locked():
        db_lock.acquire()

        while mp.MEMPOOL.lock.locked():
            time.sleep(0.1)
            logger.app_log.info(f"Block: Waiting for mempool to unlock {peer_ip}")

        logger.app_log.warning(f"Block: Digesting started from {peer_ip}")
        # variables that have been quantized are prefixed by q_ So we can avoid any unnecessary quantize again later. Takes time.
        # Variables that are only used as quantized decimal are quantized once and for all.

        block_size = Decimal(sys.getsizeof(str(data))) / Decimal(1000000)
        logger.app_log.warning(f"Block: size: {block_size} MB")

        try:

            block_list = data

            # reject block with duplicate transactions
            signature_list = []
            block_transactions = []

            for transaction_list in block_list:
                block_count += 1

                # Reworked process: we exit as soon as we find an error, no need to process further tests.
                # Then the exception handler takes place.

                # TODO EGG: benchmark this loop vs a single "WHERE IN" SQL
                # move down, so bad format tx do not require sql query
                for entry in transaction_list:  # sig 4
                    tx_count += 1
                    entry_signature = entry[4]
                    if entry_signature:  # prevent empty signature database retry hack
                        signature_list.append(entry_signature)
                        # reject block with transactions which are already in the ledger ram

                        execute_param(h3, "SELECT block_height FROM transactions WHERE signature = ?;",
                                      (entry_signature,))
                        tx_presence_check = h3.fetchone()
                        if tx_presence_check:
                            # print(node.last_block)
                            raise ValueError(f"That transaction {entry_signature[:10]} is already in our ram ledger, block_height {tx_presence_check[0]}")

                        execute_param(c, "SELECT block_height FROM transactions WHERE signature = ?;",
                                      (entry_signature,))
                        tx_presence_check = c.fetchone()
                        if tx_presence_check:
                            # print(node.last_block)
                            raise ValueError(f"That transaction {entry_signature[:10]} is already in our ledger, block_height {tx_presence_check[0]}")
                    else:
                        raise ValueError(f"Empty signature from {peer_ip}")

                tx_count = len(signature_list)
                if tx_count != len(set(signature_list)):
                    raise ValueError("There are duplicate transactions in this block, rejected")

                del signature_list[:]

                # previous block info
                execute(c,
                        "SELECT block_hash, block_height, timestamp FROM transactions WHERE reward != 0 ORDER BY block_height DESC LIMIT 1;")
                result = c.fetchall()
                db_block_hash = result[0][0]
                db_block_height = result[0][1]
                q_db_timestamp_last = quantize_two(result[0][2])
                block_height_new = db_block_height + 1
                # previous block info

                transaction_list_converted = []  # makes sure all the data are properly converted
                for tx_index, transaction in enumerate(transaction_list):
                    q_time_now = quantize_two(time.time())
                    # verify signatures
                    q_received_timestamp = quantize_two(transaction[0])  # we use this several times
                    received_timestamp = '%.2f' % q_received_timestamp
                    received_address = str(transaction[1])[:56]
                    received_recipient = str(transaction[2])[:56]
                    received_amount = '%.8f' % (quantize_eight(transaction[3]))
                    received_signature_enc = str(transaction[4])[:684]
                    received_public_key_hashed = str(transaction[5])[:1068]
                    received_operation = str(transaction[6])[:30]
                    received_openfield = str(transaction[7])[:100000]

                    # if transaction == transaction_list[-1]:
                    if tx_index == tx_count - 1:  # faster than comparing the whole tx
                        # recognize the last transaction as the mining reward transaction
                        q_block_timestamp = q_received_timestamp
                        nonce = received_openfield[:128]
                        miner_address = received_address

                    transaction_list_converted.append((received_timestamp, received_address, received_recipient,
                                                       received_amount, received_signature_enc,
                                                       received_public_key_hashed, received_operation,
                                                       received_openfield))

                    # if (q_time_now < q_received_timestamp + 432000) or not quicksync:

                    # convert readable key to instance
                    received_public_key = RSA.importKey(base64.b64decode(received_public_key_hashed))

                    received_signature_dec = base64.b64decode(received_signature_enc)
                    verifier = PKCS1_v1_5.new(received_public_key)

                    validate_pem(received_public_key_hashed)

                    hash = SHA.new(str((received_timestamp, received_address, received_recipient, received_amount,
                                        received_operation, received_openfield)).encode("utf-8"))
                    if not verifier.verify(hash, received_signature_dec):
                        raise ValueError(f"Invalid signature from {received_address}")
                    else:
                        logger.app_log.info(f"Valid signature from {received_address} to {received_recipient} amount {received_amount}")
                    if float(received_amount) < 0:
                        raise ValueError("Negative balance spend attempt")

                    if received_address != hashlib.sha224(base64.b64decode(received_public_key_hashed)).hexdigest():
                        raise ValueError("Attempt to spend from a wrong address")

                    if not essentials.address_validate(received_address):
                        raise ValueError("Not a valid sender address")

                    if not essentials.address_validate(received_recipient):
                        raise ValueError("Not a valid recipient address")

                    if q_time_now < q_received_timestamp:
                        raise ValueError(f"Future transaction not allowed, timestamp {quantize_two((q_received_timestamp - q_time_now) / 60)} minutes in the future")
                    if q_db_timestamp_last - 86400 > q_received_timestamp:
                        raise ValueError("Transaction older than 24h not allowed.")
                        # verify signatures
                        # else:
                        # print("hyp1")

                # reject blocks older than latest block
                if q_block_timestamp <= q_db_timestamp_last:
                    raise ValueError("Block is older than the previous one, will be rejected")

                # calculate current difficulty
                diff = difficulty(c)

                logger.app_log.warning(f"Time to generate block {db_block_height + 1}: {'%.2f' % diff[2]}")
                logger.app_log.warning(f"Current difficulty: {diff[3]}")
                logger.app_log.warning(f"Current blocktime: {diff[4]}")
                logger.app_log.warning(f"Current hashrate: {diff[5]}")
                logger.app_log.warning(f"Difficulty adjustment: {diff[6]}")
                logger.app_log.warning(f"Difficulty: {diff[0]} {diff[1]}")

                # logger.app_log.info("Transaction list: {}".format(transaction_list_converted))
                block_hash = hashlib.sha224(
                    (str(transaction_list_converted) + db_block_hash).encode("utf-8")).hexdigest()
                # logger.app_log.info("Last block hash: {}".format(db_block_hash))
                logger.app_log.info(f"Calculated block hash: {block_hash}")
                # logger.app_log.info("Nonce: {}".format(nonce))

                # check if we already have the hash
                execute_param(h3, "SELECT block_height FROM transactions WHERE block_hash = ?", (block_hash,))
                dummy = c.fetchone()
                if dummy:
                    raise ValueError(
                        "Skipping digestion of block {} from {}, because we already have it on block_height {}".
                        format(block_hash[:10], peer_ip, dummy[0]))

                if node.is_mainnet:
                    if block_height_new < POW_FORK:
                        diff_save = mining.check_block(block_height_new, miner_address, nonce, db_block_hash, diff[0],
                                                       received_timestamp, q_received_timestamp, q_db_timestamp_last,
                                                       peer_ip=peer_ip, app_log=logger.app_log)
                    else:
                        diff_save = mining_heavy3.check_block(block_height_new, miner_address, nonce, db_block_hash,
                                                              diff[0],
                                                              received_timestamp, q_received_timestamp,
                                                              q_db_timestamp_last,
                                                              peer_ip=peer_ip, app_log=logger.app_log)
                elif node.is_testnet:
                    diff_save = mining_heavy3.check_block(block_height_new, miner_address, nonce, db_block_hash,
                                                          diff[0],
                                                          received_timestamp, q_received_timestamp, q_db_timestamp_last,
                                                          peer_ip=peer_ip, app_log=logger.app_log)
                else:
                    # it's regnet then, will use a specific fake method here.
                    diff_save = mining_heavy3.check_block(block_height_new, miner_address, nonce, db_block_hash,
                                                          regnet.REGNET_DIFF,
                                                          received_timestamp, q_received_timestamp, q_db_timestamp_last,
                                                          peer_ip=peer_ip, app_log=logger.app_log)

                fees_block = []
                mining_reward = 0  # avoid warning

                # Cache for multiple tx from same address
                balances = {}
                for tx_index, transaction in enumerate(transaction_list):
                    db_timestamp = '%.2f' % quantize_two(transaction[0])
                    db_address = str(transaction[1])[:56]
                    db_recipient = str(transaction[2])[:56]
                    db_amount = '%.8f' % quantize_eight(transaction[3])
                    db_signature = str(transaction[4])[:684]
                    db_public_key_hashed = str(transaction[5])[:1068]
                    db_operation = str(transaction[6])[:30]
                    db_openfield = str(transaction[7])[:100000]

                    block_debit_address = 0
                    block_fees_address = 0

                    # this also is redundant on many tx per address block
                    for x in transaction_list:
                        if x[1] == db_address:  # make calculation relevant to a particular address in the block
                            block_debit_address = quantize_eight(Decimal(block_debit_address) + Decimal(x[3]))

                            if x != transaction_list[-1]:
                                block_fees_address = quantize_eight(Decimal(block_fees_address) + Decimal(
                                    fee_calculate(db_openfield, db_operation,
                                                  node.last_block)))  # exclude the mining tx from fees

                    # print("block_fees_address", block_fees_address, "for", db_address)
                    # logger.app_log.info("Digest: Inbound block credit: " + str(block_credit))
                    # logger.app_log.info("Digest: Inbound block debit: " + str(block_debit))
                    # include the new block

                    # if (q_time_now < q_received_timestamp + 432000) and not quicksync:
                    # balance_pre = quantize_eight(credit_ledger - debit_ledger - fees + rewards)  # without projection
                    balance_pre = ledger_balance3(db_address, balances,
                                                  c)  # keep this as c (ram hyperblock access)

                    # balance = quantize_eight(credit - debit - fees + rewards)
                    balance = quantize_eight(balance_pre - block_debit_address)
                    # logger.app_log.info("Digest: Projected transaction address balance: " + str(balance))
                    # else:
                    #    print("hyp2")

                    fee = fee_calculate(db_openfield, db_operation, node.last_block)

                    fees_block.append(quantize_eight(fee))
                    # logger.app_log.info("Fee: " + str(fee))

                    # decide reward
                    if tx_index == tx_count - 1:
                        db_amount = 0  # prevent spending from another address, because mining txs allow delegation
                        if db_block_height <= 10000000:
                            mining_reward = 15 - (
                                        quantize_eight(block_height_new) / quantize_eight(1000000 / 2)) - Decimal("0.8")
                            if mining_reward < 0:
                                mining_reward = 0
                        else:
                            mining_reward = 0

                        reward = quantize_eight(mining_reward + sum(fees_block[:-1]))
                        # don't request a fee for mined block so new accounts can mine
                        fee = 0
                    else:
                        reward = 0

                    if quantize_eight(balance_pre) < quantize_eight(db_amount):
                        raise ValueError(f"{db_address} sending more than owned: {db_amount}/{balance_pre}")

                    if quantize_eight(balance) - quantize_eight(block_fees_address) < 0:
                        # exclude fee check for the mining/header tx
                        raise ValueError(f"{db_address} Cannot afford to pay fees")

                    # append, but do not insert to ledger before whole block is validated, note that it takes already validated values (decimals, length)
                    logger.app_log.info(f"Block: Appending transaction back to block with {len(block_transactions)} transactions in it")
                    block_transactions.append((block_height_new, db_timestamp, db_address, db_recipient, db_amount,
                                               db_signature, db_public_key_hashed, block_hash, fee, reward,
                                               db_operation, db_openfield))

                    try:
                        mp.MEMPOOL.delete_transaction(db_signature)
                        logger.app_log.info(
                            f"Block: Removed processed transaction {db_signature[:56]} from the mempool while digesting")
                    except:
                        # tx was not or is no more in the local mempool
                        pass
                # end for transaction_list

                # save current diff (before the new block)
                execute_param(c, "INSERT INTO misc VALUES (?, ?)", (block_height_new, diff_save))
                commit(conn)

                # quantized vars have to be converted, since Decimal is not json serializable...
                node.plugin_manager.execute_action_hook('block',
                                                        {'height': block_height_new, 'diff': diff_save,
                                                         'hash': block_hash, 'timestamp': float(q_block_timestamp),
                                                         'miner': miner_address, 'ip': peer_ip})

                node.plugin_manager.execute_action_hook('fullblock',
                                                        {'height': block_height_new, 'diff': diff_save,
                                                         'hash': block_hash, 'timestamp': float(q_block_timestamp),
                                                         'miner': miner_address, 'ip': peer_ip,
                                                         'transactions': block_transactions})

                # do not use "transaction" as it masks upper level variable.
                for transaction2 in block_transactions:
                    execute_param(c, "INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
                        str(transaction2[0]), str(transaction2[1]),
                        str(transaction2[2]), str(transaction2[3]),
                        str(transaction2[4]), str(transaction2[5]),
                        str(transaction2[6]), str(transaction2[7]),
                        str(transaction2[8]), str(transaction2[9]),
                        str(transaction2[10]), str(transaction2[11])))
                    # secure commit for slow nodes
                    commit(conn)

                # savings
                if node.is_testnet or block_height_new >= 843000:
                    # no savings for regnet
                    if int(block_height_new) % 10000 == 0:  # every x blocks
                        staking.staking_update(conn, c, index, index_cursor,
                                               "normal", block_height_new, logger.app_log)
                        staking.staking_payout(conn, c, index, index_cursor,
                                               block_height_new, float(q_block_timestamp), logger.app_log)
                        staking.staking_revalidate(conn, c, index, index_cursor,
                                                   block_height_new, logger.app_log)

                # new hash
                c.execute(
                    "SELECT * FROM transactions WHERE block_height = (SELECT max(block_height) FROM transactions)")
                # Was trying to simplify, but it's the latest mirror hash. not the latest block, nor the mirror of the latest block.
                # c.execute("SELECT * FROM transactions WHERE block_height = ?", (block_height_new -1,))
                tx_list_to_hash = c.fetchall()
                mirror_hash = blake2b(str(tx_list_to_hash).encode(), digest_size=20).hexdigest()
                # /new hash

                # dev reward
                if int(block_height_new) % 10 == 0:  # every 10 blocks
                    execute_param(c, "INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                  (-block_height_new, str(q_time_now), "Development Reward", str(node.genesis_conf),
                                   str(mining_reward), "0", "0", mirror_hash, "0", "0", "0", "0"))
                    commit(conn)

                    execute_param(c, "INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                  (-block_height_new, str(q_time_now), "Hypernode Payouts",
                                   "3e08b5538a4509d9daa99e01ca5912cda3e98a7f79ca01248c2bde16",
                                   "8", "0", "0", mirror_hash, "0", "0", "0", "0"))
                    commit(conn)
                # /dev reward

                # logger.app_log.warning("Block: {}: {} valid and saved from {}".format(block_height_new, block_hash[:10], peer_ip))
                logger.app_log.warning(
                    f"Valid block: {block_height_new}: {block_hash[:10]} with {len(transaction_list)} txs, digestion from {peer_ip} completed in {str(time.time() - float(q_time_now))[:5]}s.")

                del block_transactions[:]
                node.peers.unban(peer_ip)

                # This new block may change the int(diff). Trigger the hook whether it changed or not.
                diff = difficulty(c)
                node.plugin_manager.execute_action_hook('diff', diff[0])
                # We could recalc diff after inserting block, and then only trigger the block hook, but I fear this would delay the new block event.

                # /whole block validation
                # NEW: returns new block hash

            return block_hash

        except Exception as e:
            logger.app_log.warning(f"Block: processing failed: {e}")

            logger.app_log.info(f"Received data dump: {data}")

            failed_cause = str(e)
            # Temp

            exc_type, exc_obj, exc_tb = sys.exc_info()
            fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
            print(exc_type, fname, exc_tb.tb_lineno)

            if node.peers.warning(sdef, peer_ip, "Rejected block", 2):
                raise ValueError(f"{peer_ip} banned")
            raise ValueError("Block: digestion aborted")

        finally:
            if node.full_ledger or node.ram_conf:
                # first case move stuff from hyper.db to ledger.db; second case move stuff from ram to both
                db_to_drive(hdd,h,hdd2,h2)
            db_lock.release()
            delta_t = time.time() - float(q_time_now)
            # logger.app_log.warning("Block: {}: {} digestion completed in {}s.".format(block_height_new,  block_hash[:10], delta_t))
            node.plugin_manager.execute_action_hook('digestblock',
                                                    {'failed': failed_cause, 'ip': peer_ip, 'deltat': delta_t,
                                                     "blocks": block_count, "txs": tx_count})

    else:
        logger.app_log.warning(f"Block: Skipping processing from {peer_ip}, someone delivered data faster")
        node.plugin_manager.execute_action_hook('digestblock', {'failed': "skipped", 'ip': peer_ip})


def coherence_check():
    try:
        with open("coherence_last", 'r') as filename:
            coherence_last = int(filename.read())

    except:
        logger.app_log.warning("Coherence anchor not found, going through the whole chain")
        coherence_last = 0

    logger.app_log.warning(f"Status: Testing chain coherence, starting with block {coherence_last}")

    if node.full_ledger:
        chains_to_check = [node.ledger_path_conf, node.hyper_path_conf]
    else:
        chains_to_check = [node.hyper_path_conf]

    for chain in chains_to_check:
        conn = sqlite3.connect(chain)
        c = conn.cursor()

        # perform test on transaction table
        y = None
        # Egg: not sure block_height != (0 OR 1)  gives the proper result, 0 or 1  = 1. not in (0, 1) could be better.
        for row in c.execute(
                "SELECT block_height FROM transactions WHERE reward != 0 AND block_height != (0 OR 1) AND block_height >= ? ORDER BY block_height ASC",
                (coherence_last,)):
            y_init = row[0]

            if y is None:
                y = y_init

            if row[0] != y:

                for chain2 in chains_to_check:
                    conn2 = sqlite3.connect(chain2)
                    c2 = conn2.cursor()
                    logger.app_log.warning(
                        f"Status: Chain {chain} transaction coherence error at: {row[0] - 1}. {row[0]} instead of {y}")
                    c2.execute("DELETE FROM transactions WHERE block_height >= ? OR block_height <= ?",
                               (row[0] - 1, -(row[0] + 1)))
                    conn2.commit()
                    c2.execute("DELETE FROM misc WHERE block_height >= ?", (row[0] - 1,))
                    conn2.commit()

                    # execute_param(conn2, ('DELETE FROM transactions WHERE address = "Development Reward" AND block_height <= ?'), (-(row[0]+1),))
                    # commit(conn2)
                    # conn2.close()

                    # rollback indices
                    tokens_rollback(y)
                    aliases_rollback(y)
                    staking_rollback(y)

                    # rollback indices

                    logger.app_log.warning(f"Status: Due to a coherence issue at block {y}, {chain} has been rolled back and will be resynchronized")
                break

            y = y + 1

        # perform test on misc table
        y = None

        for row in c.execute("SELECT block_height FROM misc WHERE block_height > ? ORDER BY block_height ASC",
                             (300000,)):
            y_init = row[0]

            if y is None:
                y = y_init
                # print("assigned")
                # print(row[0], y)

            if row[0] != y:
                # print(row[0], y)
                for chain2 in chains_to_check:
                    conn2 = sqlite3.connect(chain2)
                    c2 = conn2.cursor()
                    logger.app_log.warning(
                        f"Status: Chain {chain} difficulty coherence error at: {row[0] - 1} {row[0]} instead of {y}")
                    c2.execute("DELETE FROM transactions WHERE block_height >= ?", (row[0] - 1,))
                    conn2.commit()
                    c2.execute("DELETE FROM misc WHERE block_height >= ?", (row[0] - 1,))
                    conn2.commit()

                    execute_param(conn2, (
                        'DELETE FROM transactions WHERE address = "Development Reward" AND block_height <= ?'),
                                  (-(row[0] + 1),))
                    commit(conn2)
                    conn2.close()

                    # rollback indices
                    tokens_rollback(y)
                    aliases_rollback(y)
                    staking_rollback(y)
                    # rollback indices

                    logger.app_log.warning(f"Status: Due to a coherence issue at block {y}, {chain} has been rolled back and will be resynchronized")
                break

            y = y + 1

        logger.app_log.warning(f"Status: Chain coherence test complete for {chain}")
        conn.close()

        with open("coherence_last", 'w') as filename:
            filename.write(str(y - 1000))  # room for rollbacks


# init
def db_maintenance():
    # db maintenance
    logger.app_log.warning("Status: Database maintenance started")
    execute(database.conn, "VACUUM")
    # mp.MEMPOOL.vacuum()
    logger.app_log.warning("Status: Database maintenance finished")


class ThreadedTCPRequestHandler(socketserver.BaseRequestHandler):
    def handle(self):
        if node.IS_STOPPING:
            return

        try:
            peer_ip = self.request.getpeername()[0]
        except:
            logger.app_log.warning("Inbound: Transport endpoint was not connected")
            return
        # if threading.active_count() < node.thread_limit_conf or peer_ip == "127.0.0.1":
        # Always keep a slot for whitelisted (wallet could be there)
        if threading.active_count() < node.thread_limit_conf / 3 * 2 or node.peers.is_whitelisted(peer_ip):  # inbound
            capacity = True
        else:
            capacity = False
            try:
                self.request.close()
                logger.app_log.info(f"Free capacity for {peer_ip} unavailable, disconnected")
                # if you raise here, you kill the whole server
            except:
                pass
            finally:
                return

        banned = False
        dict_ip = {'ip': peer_ip}
        node.plugin_manager.execute_filter_hook('peer_ip', dict_ip)
        if node.peers.is_banned(peer_ip) or dict_ip['ip'] == 'banned':
            banned = True
            try:
                self.request.close()
                logger.app_log.info(f"IP {peer_ip} banned, disconnected")
            except:
                pass
            finally:
                return

        timeout_operation = 120  # timeout
        timer_operation = time.time()  # start counting

        while not banned and capacity and node.peers.version_allowed(peer_ip,
                                                                     node.version_allow) and not node.IS_STOPPING:
            try:


                # Failsafe
                if self.request == -1:
                    raise ValueError(f"Inbound: Closed socket from {peer_ip}")

                if not time.time() <= timer_operation + timeout_operation:  # return on timeout
                    if node.peers.warning(self.request, peer_ip, "Operation timeout", 2):
                        logger.app_log.info(f"{peer_ip} banned")
                        break

                    raise ValueError(f"Inbound: Operation timeout from {peer_ip}")

                data = connections.receive(self.request)

                logger.app_log.info(
                    f"Inbound: Received: {data} from {peer_ip}")  # will add custom ports later

                if data.startswith('regtest_'):
                    if not node.is_regnet:
                        connections.send(self.request, "notok")
                        return
                    else:
                        execute(database.c, (
                            "SELECT block_hash FROM transactions WHERE block_height= (select max(block_height) from transactions)"))
                        block_hash = database.c.fetchone()[0]
                        # feed regnet with current thread db handle. refactor needed.
                        regnet.conn, regnet.c, regnet.hdd, regnet.h, regnet.hdd2, regnet.h2, regnet.h3 = database.conn, database.c, database.hdd, database.h, database.hdd2, database.h2, database.h3
                        regnet.command(self.request, data, block_hash)

                if data == 'version':
                    data = connections.receive(self.request)
                    if data not in node.version_allow:
                        logger.app_log.warning(
                            f"Protocol version mismatch: {data}, should be {node.version_allow}")
                        connections.send(self.request, "notok")
                        return
                    else:
                        logger.app_log.warning(f"Inbound: Protocol version matched: {data}")
                        connections.send(self.request, "ok")
                        node.peers.store_mainnet(peer_ip, data)

                elif data == 'getversion':
                    connections.send(self.request, node.version)

                elif data == 'mempool':

                    # receive theirs
                    segments = connections.receive(self.request)
                    logger.app_log.info(mp.MEMPOOL.merge(segments, peer_ip, database.c, False))

                    # receive theirs

                    # execute_param(m, ('SELECT timestamp,address,recipient,amount,signature,public_key,operation,openfield FROM transactions WHERE timeout < ? ORDER BY amount DESC;'), (int(time.time() - 5),))
                    if mp.MEMPOOL.sendable(peer_ip):
                        # Only send the diff
                        mempool_txs = mp.MEMPOOL.tx_to_send(peer_ip, segments)
                        # and note the time
                        mp.MEMPOOL.sent(peer_ip)
                    else:
                        # We already sent not long ago, send empy
                        mempool_txs = []

                    # send own
                    # logger.app_log.info("Inbound: Extracted from the mempool: " + str(mempool_txs))  # improve: sync based on signatures only

                    # if len(mempool_txs) > 0: same as the other
                    connections.send(self.request, mempool_txs)

                    # send own

                elif data == "hello":
                    if node.is_regnet:
                        logger.app_log.info("Inbound: Got hello but I'm in regtest mode, closing.")
                        return

                    connections.send(self.request, "peers")
                    connections.send(self.request, node.peers.peer_list_old_format())  # INCOMPATIBLE WITH THE OLD WAY

                    while db_lock.locked():
                        time.sleep(quantize_two(node.pause_conf))
                    logger.app_log.info("Inbound: Sending sync request")

                    connections.send(self.request, "sync")

                elif data == "sendsync":
                    while db_lock.locked():
                        time.sleep(quantize_two(node.pause_conf))

                    while len(node.syncing) >= 3:
                        if node.IS_STOPPING:
                            return
                        time.sleep(int(node.pause_conf))

                    connections.send(self.request, "sync")

                elif data == "blocksfnd":
                    logger.app_log.info(f"Inbound: Client {peer_ip} has the block(s)")  # node should start sending txs in this step

                    # logger.app_log.info("Inbound: Combined segments: " + segments)
                    # print peer_ip
                    if db_lock.locked():
                        logger.app_log.info(f"Skipping sync from {peer_ip}, syncing already in progress")

                    else:
                        execute(database.c,
                                "SELECT timestamp FROM transactions WHERE reward != 0 ORDER BY block_height DESC LIMIT 1;")  # or it takes the first
                        node.last_block_ago = quantize_two(database.c.fetchone()[0])

                        if node.last_block_ago < time.time() - 600:
                            # block_req = most_common(consensus_blockheight_list)
                            block_req = node.peers.consensus_most_common
                            logger.app_log.warning("Most common block rule triggered")

                        else:
                            # block_req = max(consensus_blockheight_list)
                            block_req = node.peers.consensus_max
                            logger.app_log.warning("Longest chain rule triggered")

                        if int(received_block_height) >= block_req:

                            try:  # they claim to have the longest chain, things must go smooth or ban
                                logger.app_log.warning(f"Confirming to sync from {peer_ip}")
                                node.plugin_manager.execute_action_hook('sync', {'what': 'syncing_from', 'ip': peer_ip})
                                connections.send(self.request, "blockscf")

                                segments = connections.receive(self.request)

                            except:
                                if node.peers.warning(self.request, peer_ip, "Failed to deliver the longest chain"):
                                    logger.app_log.info(f"{peer_ip} banned")
                                    break
                            else:
                                digest_block(segments, self.request, peer_ip, database.conn, database.c,database.hdd,database.h,database.hdd2,database.h2, database.h3, database.index, database.index_cursor)


                                # receive theirs
                        else:
                            logger.app_log.warning(f"Rejecting to sync from {peer_ip}")
                            connections.send(self.request, "blocksrj")
                            logger.app_log.info(
                                f"Inbound: Distant peer {peer_ip} is at {received_block_height}, should be at least {block_req}")

                    connections.send(self.request, "sync")

                elif data == "blockheight":
                    try:
                        received_block_height = connections.receive(self.request)  # receive client's last block height
                        logger.app_log.info(
                            f"Inbound: Received block height {received_block_height} from {peer_ip} ")

                        # consensus pool 1 (connection from them)
                        consensus_blockheight = int(received_block_height)  # str int to remove leading zeros
                        # consensus_add(peer_ip, consensus_blockheight, self.request)
                        node.peers.consensus_add(peer_ip, consensus_blockheight, self.request, node.last_block)
                        # consensus pool 1 (connection from them)

                        execute(database.c, ('SELECT max(block_height) FROM transactions'))
                        db_block_height = database.c.fetchone()[0]

                        # append zeroes to get static length
                        connections.send(self.request, db_block_height)
                        # send own block height

                        if int(received_block_height) > db_block_height:
                            logger.app_log.warning("Inbound: Client has higher block")

                            execute(database.c,
                                    ('SELECT block_hash FROM transactions ORDER BY block_height DESC LIMIT 1'))
                            db_block_hash = database.c.fetchone()[0]  # get latest block_hash

                            logger.app_log.info(f"Inbound: block_hash to send: {db_block_hash}")
                            connections.send(self.request, db_block_hash)

                            # receive their latest hash
                            # confirm you know that hash or continue receiving

                        elif int(received_block_height) <= db_block_height:
                            if int(received_block_height) == db_block_height:
                                logger.app_log.info(
                                    f"Inbound: We have the same height as {peer_ip} ({received_block_height}), hash will be verified")
                            else:
                                logger.app_log.warning(
                                    f"Inbound: We have higher ({db_block_height}) block height than {peer_ip} ({received_block_height}), hash will be verified")

                            data = connections.receive(self.request)  # receive client's last block_hash
                            # send all our followup hashes

                            logger.app_log.info(f"Inbound: Will seek the following block: {data}")

                            try:
                                execute_param(database.h3,
                                              ("SELECT block_height FROM transactions WHERE block_hash = ?;"), (data,))
                                client_block = database.h3.fetchone()[0]

                                logger.app_log.info(f"Inbound: Client is at block {client_block}") # now check if we have any newer

                                execute(database.h3,
                                        ('SELECT block_hash FROM transactions ORDER BY block_height DESC LIMIT 1'))
                                db_block_hash = database.h3.fetchone()[0]  # get latest block_hash
                                if db_block_hash == data or not node.egress:
                                    if not node.egress:
                                        logger.app_log.warning(f"Outbound: Egress disabled for {peer_ip}")
                                    else:
                                        logger.app_log.info(f"Inbound: Client {peer_ip} has the latest block")

                                    time.sleep(int(node.pause_conf))  # reduce CPU usage
                                    connections.send(self.request, "nonewblk")

                                else:

                                    blocks_fetched = []
                                    del blocks_fetched[:]
                                    while sys.getsizeof(
                                            str(blocks_fetched)) < 500000:  # limited size based on txs in blocks
                                        # execute_param(h3, ("SELECT block_height, timestamp,address,recipient,amount,signature,public_key,keep,openfield FROM transactions WHERE block_height > ? AND block_height <= ?;"),(str(int(client_block)),) + (str(int(client_block + 1)),))
                                        execute_param(database.h3, (
                                            "SELECT timestamp,address,recipient,amount,signature,public_key,cast(operation as TEXT),openfield FROM transactions WHERE block_height > ? AND block_height <= ?;"),
                                                      (str(int(client_block)), str(int(client_block + 1)),))
                                        result = database.h3.fetchall()
                                        if not result:
                                            break
                                        blocks_fetched.extend([result])
                                        client_block = int(client_block) + 1

                                    # blocks_send = [[l[1:] for l in group] for _, group in groupby(blocks_fetched, key=itemgetter(0))]  # remove block number

                                    # logger.app_log.info("Inbound: Selected " + str(blocks_fetched) + " to send")

                                    connections.send(self.request, "blocksfnd")

                                    confirmation = connections.receive(self.request)

                                    if confirmation == "blockscf":
                                        logger.app_log.info("Inbound: Client confirmed they want to sync from us")
                                        connections.send(self.request, blocks_fetched)

                                    elif confirmation == "blocksrj":
                                        logger.app_log.info(
                                            "Inbound: Client rejected to sync from us because we're don't have the latest block")
                                        pass

                                        # send own

                            except Exception as e:
                                logger.app_log.warning("Inbound: Block {data[:8]} of {peer_ip} not found")
                                connections.send(self.request, "blocknf")
                                connections.send(self.request, data)
                    except Exception as e:
                        logger.app_log.info(f"Inbound: Sync failed {e}")

                elif data == "nonewblk":
                    connections.send(self.request, "sync")

                elif data == "blocknf":
                    block_hash_delete = connections.receive(self.request)
                    # print peer_ip
                    if consensus_blockheight == node.peers.consensus_max:
                        blocknf(block_hash_delete, peer_ip, database.c, database.conn, database.h, database.hdd, database.h2, database.hdd2)
                        if node.peers.warning(self.request, peer_ip, "Rollback", 2):
                            logger.app_log.info(f"{peer_ip} banned")
                            break
                    logger.app_log.info("Outbound: Deletion complete, sending sync request")

                    while db_lock.locked():
                        if node.IS_STOPPING:
                            return
                        time.sleep(node.pause_conf)
                    connections.send(self.request, "sync")

                elif data == "block":
                    # if (peer_ip in allowed or "any" in allowed):  # from miner
                    if node.peers.is_allowed(peer_ip, data):  # from miner
                        # TODO: rights management could be done one level higher instead of repeating the same check everywhere

                        logger.app_log.info(f"Outbound: Received a block from miner {peer_ip}")
                        # receive block
                        segments = connections.receive(self.request)
                        # logger.app_log.info("Inbound: Combined mined segments: " + segments)

                        # check if we have the latest block

                        execute(database.c, ('SELECT max(block_height) FROM transactions'))
                        db_block_height = int(database.c.fetchone()[0])

                        # check if we have the latest block

                        mined = {"timestamp": time.time(), "last": db_block_height, "ip": peer_ip, "miner": "",
                                 "result": False, "reason": ''}
                        try:
                            mined['miner'] = segments[0][-1][2]
                        except:
                            pass
                        if node.is_mainnet:
                            if len(node.peers.connection_pool) < 5 and not node.peers.is_whitelisted(peer_ip):
                                reason = "Outbound: Mined block ignored, insufficient connections to the network"
                                mined['reason'] = reason
                                node.plugin_manager.execute_action_hook('mined', mined)
                                logger.app_log.info(reason)
                            elif db_lock.locked():
                                reason = "Outbound: Block from miner skipped because we are digesting already"
                                mined['reason'] = reason
                                node.plugin_manager.execute_action_hook('mined', mined)
                                logger.app_log.warning(reason)
                            elif db_block_height >= node.peers.consensus_max - 3:
                                mined['result'] = True
                                node.plugin_manager.execute_action_hook('mined', mined)
                                logger.app_log.info("Outbound: Processing block from miner")
                                digest_block(segments, self.request, peer_ip, database.conn, database.c, database.hdd,
                                             database.h, database.hdd2, database.h2, database.h3, database.index,
                                             database.index_cursor)
                            else:
                                reason = f"Outbound: Mined block was orphaned because node was not synced, we are at block {db_block_height}, should be at least {node.peers.consensus_max - 3}"
                                mined['reason'] = reason
                                node.plugin_manager.execute_action_hook('mined', mined)
                                logger.app_log.warning(reason)
                        else:
                            digest_block(segments, self.request, peer_ip, database.conn, database.c, database.hdd,
                                         database.h, database.hdd2, database.h2, database.h3, database.index,
                                         database.index_cursor)
                    else:
                        connections.receive(self.request)  # receive block, but do nothing about it
                        logger.app_log.info(f"{peer_ip} not whitelisted for block command")

                elif data == "blocklast":
                    # if (peer_ip in allowed or "any" in allowed):  # only sends the miner part of the block!
                    if node.peers.is_allowed(peer_ip, data):
                        execute(database.c,
                                ("SELECT * FROM transactions WHERE reward != 0 ORDER BY block_height DESC LIMIT 1;"))
                        block_last = database.c.fetchall()[0]

                        connections.send(self.request, block_last)
                    else:
                        logger.app_log.info(f"{peer_ip} not whitelisted for blocklast command")

                elif data == "blocklastjson":
                    # if (peer_ip in allowed or "any" in allowed):  # only sends the miner part of the block!
                    if node.peers.is_allowed(peer_ip, data):
                        execute(database.c,
                                ("SELECT * FROM transactions WHERE reward != 0 ORDER BY block_height DESC LIMIT 1;"))
                        block_last = database.c.fetchall()[0]

                        response = {"block_height": block_last[0],
                                    "timestamp": block_last[1],
                                    "address": block_last[2],
                                    "recipient": block_last[3],
                                    "amount": block_last[4],
                                    "signature": block_last[5],
                                    "public_key": block_last[6],
                                    "block_hash": block_last[7],
                                    "fee": block_last[8],
                                    "reward": block_last[9],
                                    "operation": block_last[10],
                                    "nonce": block_last[11]}

                        connections.send(self.request, response)
                    else:
                        logger.app_log.info(f"{peer_ip} not whitelisted for blocklastjson command")

                elif data == "blockget":
                    # if (peer_ip in allowed or "any" in allowed):
                    if node.peers.is_allowed(peer_ip, data):
                        block_desired = connections.receive(self.request)

                        execute_param(database.h3, ("SELECT * FROM transactions WHERE block_height = ?;"),
                                      (block_desired,))
                        block_desired_result = database.h3.fetchall()

                        connections.send(self.request, block_desired_result)
                    else:
                        logger.app_log.info(f"{peer_ip} not whitelisted for blockget command")

                elif data == "blockgetjson":
                    # if (peer_ip in allowed or "any" in allowed):
                    if node.peers.is_allowed(peer_ip, data):
                        block_desired = connections.receive(self.request)

                        execute_param(database.h3, ("SELECT * FROM transactions WHERE block_height = ?;"),
                                      (block_desired,))
                        block_desired_result = database.h3.fetchall()

                        response_list = []
                        for transaction in block_desired_result:
                            response = {"block_height": transaction[0],
                                        "timestamp": transaction[1],
                                        "address": transaction[2],
                                        "recipient": transaction[3],
                                        "amount": transaction[4],
                                        "signature": transaction[5],
                                        "public_key": transaction[6],
                                        "block_hash": transaction[7],
                                        "fee": transaction[8],
                                        "reward": transaction[9],
                                        "operation": transaction[10],
                                        "openfield": transaction[11]}

                            response_list.append(response)

                        connections.send(self.request, response_list)
                    else:
                        logger.app_log.info(f"{peer_ip} not whitelisted for blockget command")

                elif data == "mpinsert":
                    # if (peer_ip in allowed or "any" in allowed):
                    if node.peers.is_allowed(peer_ip, data):
                        mempool_insert = connections.receive(self.request)
                        logger.app_log.warning("mpinsert command")

                        mpinsert_result = mp.MEMPOOL.merge(mempool_insert, peer_ip, database.c, True, True)
                        logger.app_log.warning(f"mpinsert result: {mpinsert_result}")
                        connections.send(self.request, mpinsert_result)
                    else:
                        logger.app_log.info(f"{peer_ip} not whitelisted for mpinsert command")

                elif data == "balanceget":
                    # if (peer_ip in allowed or "any" in allowed):
                    if node.peers.is_allowed(peer_ip, data):
                        balance_address = connections.receive(self.request)  # for which address

                        balanceget_result = balanceget(balance_address, database.h3)

                        connections.send(self.request,
                                         balanceget_result)  # return balance of the address to the client, including mempool
                        # connections.send(self.request, balance_pre)  # return balance of the address to the client, no mempool
                    else:
                        logger.app_log.info("{peer_ip} not whitelisted for balanceget command")

                elif data == "balancegetjson":
                    # if (peer_ip in allowed or "any" in allowed):
                    if node.peers.is_allowed(peer_ip, data):
                        balance_address = connections.receive(self.request)  # for which address

                        balanceget_result = balanceget(balance_address, database.h3)
                        response = {"balance": balanceget_result[0],
                                    "credit": balanceget_result[1],
                                    "debit": balanceget_result[2],
                                    "fees": balanceget_result[3],
                                    "rewards": balanceget_result[4],
                                    "balance_no_mempool": balanceget_result[5]}

                        connections.send(self.request,
                                         response)  # return balance of the address to the client, including mempool
                        # connections.send(self.request, balance_pre)  # return balance of the address to the client, no mempool
                    else:
                        logger.app_log.info(f"{peer_ip} not whitelisted for balancegetjson command")

                elif data == "balancegethyper":
                    # if (peer_ip in allowed or "any" in allowed):
                    if node.peers.is_allowed(peer_ip, data):
                        balance_address = connections.receive(self.request)  # for which address

                        balanceget_result = balanceget(balance_address, database.c)[0]

                        connections.send(self.request,
                                         balanceget_result)  # return balance of the address to the client, including mempool
                        # connections.send(self.request, balance_pre)  # return balance of the address to the client, no mempool
                    else:
                        logger.app_log.info(f"{peer_ip} not whitelisted for balancegetjson command")

                elif data == "balancegethyperjson":
                    # if (peer_ip in allowed or "any" in allowed):
                    if node.peers.is_allowed(peer_ip, data):
                        balance_address = connections.receive(self.request)  # for which address

                        balanceget_result = balanceget(balance_address, database.c)
                        response = {"balance": balanceget_result[0]}

                        connections.send(self.request,
                                         response)  # return balance of the address to the client, including mempool
                        # connections.send(self.request, balance_pre)  # return balance of the address to the client, no mempool
                    else:
                        logger.app_log.info(f"{peer_ip} not whitelisted for balancegethyperjson command")

                elif data == "mpgetjson" and node.peers.is_allowed(peer_ip, data):
                    mempool_txs = mp.MEMPOOL.fetchall(mp.SQL_SELECT_TX_TO_SEND)

                    response_list = []
                    for transaction in mempool_txs:
                        response = {"timestamp": transaction[0],
                                    "address": transaction[1],
                                    "recipient": transaction[2],
                                    "amount": transaction[3],
                                    "signature": transaction[4],
                                    "public_key": transaction[5],
                                    "operation": transaction[6],
                                    "openfield": transaction[7]}

                        response_list.append(response)

                    # logger.app_log.info("Outbound: Extracted from the mempool: " + str(mempool_txs))  # improve: sync based on signatures only

                    # if len(mempool_txs) > 0: #wont sync mempool until we send something, which is bad
                    # send own
                    connections.send(self.request, response_list)

                elif data == "mpget" and node.peers.is_allowed(peer_ip, data):
                    mempool_txs = mp.MEMPOOL.fetchall(mp.SQL_SELECT_TX_TO_SEND)

                    # logger.app_log.info("Outbound: Extracted from the mempool: " + str(mempool_txs))  # improve: sync based on signatures only

                    # if len(mempool_txs) > 0: #wont sync mempool until we send something, which is bad
                    # send own
                    connections.send(self.request, mempool_txs)

                elif data == "mpclear" and peer_ip == "127.0.0.1":  # reserved for localhost
                    mp.MEMPOOL.clear()

                elif data == "keygen":
                    # if (peer_ip in allowed or "any" in allowed):
                    if node.peers.is_allowed(peer_ip, data):
                        (gen_private_key_readable, gen_public_key_readable, gen_address) = keys.generate()
                        connections.send(self.request, (gen_private_key_readable, gen_public_key_readable, gen_address))
                        (gen_private_key_readable, gen_public_key_readable, gen_address) = (None, None, None)
                    else:
                        logger.app_log.info(f"{peer_ip} not whitelisted for keygen command")

                elif data == "keygenjson":
                    # if (peer_ip in allowed or "any" in allowed):
                    if node.peers.is_allowed(peer_ip, data):
                        (gen_private_key_readable, gen_public_key_readable, gen_address) = keys.generate()
                        response = {"private_key": gen_private_key_readable,
                                    "public_key": gen_public_key_readable,
                                    "address": gen_address}

                        connections.send(self.request, response)
                        (gen_private_key_readable, gen_public_key_readable, gen_address) = (None, None, None)
                    else:
                        logger.app_log.info(f"{peer_ip} not whitelisted for keygen command")

                elif data == "addlist":
                    # if (peer_ip in allowed or "any" in allowed):
                    if node.peers.is_allowed(peer_ip, data):
                        address_tx_list = connections.receive(self.request)
                        execute_param(database.h3, (
                            "SELECT * FROM transactions WHERE (address = ? OR recipient = ?) ORDER BY block_height DESC"),
                                      (address_tx_list, address_tx_list,))
                        result = database.h3.fetchall()
                        connections.send(self.request, result)
                    else:
                        logger.app_log.info(f"{peer_ip} not whitelisted for addlist command")

                elif data == "listlimjson":
                    # if (peer_ip in allowed or "any" in allowed):
                    if node.peers.is_allowed(peer_ip, data):
                        list_limit = connections.receive(self.request)
                        # print(address_tx_list_limit)
                        execute_param(database.h3, ("SELECT * FROM transactions ORDER BY block_height DESC LIMIT ?"),
                                      (list_limit,))
                        result = database.h3.fetchall()

                        response_list = []
                        for transaction in result:
                            response = {"block_height": transaction[0],
                                        "timestamp": transaction[1],
                                        "address": transaction[2],
                                        "recipient": transaction[3],
                                        "amount": transaction[4],
                                        "signature": transaction[5],
                                        "public_key": transaction[6],
                                        "block_hash": transaction[7],
                                        "fee": transaction[8],
                                        "reward": transaction[9],
                                        "operation": transaction[10],
                                        "openfield": transaction[11]}

                            response_list.append(response)

                        connections.send(self.request, response_list)
                    else:
                        logger.app_log.info(f"{peer_ip} not whitelisted for listlimjson command")

                elif data == "listlim":
                    # if (peer_ip in allowed or "any" in allowed):
                    if node.peers.is_allowed(peer_ip, data):
                        list_limit = connections.receive(self.request)
                        # print(address_tx_list_limit)
                        execute_param(database.h3, ("SELECT * FROM transactions ORDER BY block_height DESC LIMIT ?"),
                                      (list_limit,))
                        result = database.h3.fetchall()
                        connections.send(self.request, result)
                    else:
                        logger.app_log.info(f"{peer_ip} not whitelisted for listlim command")

                elif data == "addlistlim":
                    # if (peer_ip in allowed or "any" in allowed):
                    if node.peers.is_allowed(peer_ip, data):
                        address_tx_list = connections.receive(self.request)
                        address_tx_list_limit = connections.receive(self.request)

                        # print(address_tx_list_limit)
                        execute_param(database.h3, (
                            "SELECT * FROM transactions WHERE (address = ? OR recipient = ?) ORDER BY block_height DESC LIMIT ?"),
                                      (address_tx_list, address_tx_list, address_tx_list_limit,))
                        result = database.h3.fetchall()
                        connections.send(self.request, result)
                    else:
                        logger.app_log.info(f"{peer_ip} not whitelisted for addlistlim command")

                elif data == "addlistlimjson":
                    # if (peer_ip in allowed or "any" in allowed):
                    if node.peers.is_allowed(peer_ip, data):
                        address_tx_list = connections.receive(self.request)
                        address_tx_list_limit = connections.receive(self.request)

                        # print(address_tx_list_limit)
                        execute_param(database.h3, (
                            "SELECT * FROM transactions WHERE (address = ? OR recipient = ?) ORDER BY block_height DESC LIMIT ?"),
                                      (address_tx_list, address_tx_list, address_tx_list_limit,))
                        result = database.h3.fetchall()

                        response_list = []
                        for transaction in result:
                            response = {"block_height": transaction[0],
                                        "timestamp": transaction[1],
                                        "address": transaction[2],
                                        "recipient": transaction[3],
                                        "amount": transaction[4],
                                        "signature": transaction[5],
                                        "public_key": transaction[6],
                                        "block_hash": transaction[7],
                                        "fee": transaction[8],
                                        "reward": transaction[9],
                                        "operation": transaction[10],
                                        "openfield": transaction[11]}

                            response_list.append(response)

                        connections.send(self.request, response_list)
                    else:
                        logger.app_log.info(f"{peer_ip} not whitelisted for addlistlimjson command")

                elif data == "addlistlimmir":
                    # if (peer_ip in allowed or "any" in allowed):
                    if node.peers.is_allowed(peer_ip, data):
                        address_tx_list = connections.receive(self.request)
                        address_tx_list_limit = connections.receive(self.request)

                        # print(address_tx_list_limit)
                        execute_param(database.h3, (
                            "SELECT * FROM transactions WHERE (address = ? OR recipient = ?) AND block_height < 1 ORDER BY block_height ASC LIMIT ?"),
                                      (address_tx_list, address_tx_list, address_tx_list_limit,))
                        result = database.h3.fetchall()
                        connections.send(self.request, result)
                    else:
                        logger.app_log.info(f"{peer_ip} not whitelisted for addlistlimmir command")

                elif data == "addlistlimmirjson":
                    # if (peer_ip in allowed or "any" in allowed):
                    if node.peers.is_allowed(peer_ip, data):
                        address_tx_list = connections.receive(self.request)
                        address_tx_list_limit = connections.receive(self.request)

                        # print(address_tx_list_limit)
                        execute_param(database.h3, (
                            "SELECT * FROM transactions WHERE (address = ? OR recipient = ?) AND block_height < 1 ORDER BY block_height ASC LIMIT ?"),
                                      (address_tx_list, address_tx_list, address_tx_list_limit,))
                        result = database.h3.fetchall()

                        response_list = []
                        for transaction in result:
                            response = {"block_height": transaction[0],
                                        "timestamp": transaction[1],
                                        "address": transaction[2],
                                        "recipient": transaction[3],
                                        "amount": transaction[4],
                                        "signature": transaction[5],
                                        "public_key": transaction[6],
                                        "block_hash": transaction[7],
                                        "fee": transaction[8],
                                        "reward": transaction[9],
                                        "operation": transaction[10],
                                        "openfield": transaction[11]}

                            response_list.append(response)

                        connections.send(self.request, response_list)

                        connections.send(self.request, result)
                    else:
                        logger.app_log.info(f"{peer_ip} not whitelisted for addlistlimmir command")


                elif data == "aliasget":  # all for a single address, no protection against overlapping
                    # if (peer_ip in allowed or "any" in allowed):
                    if node.peers.is_allowed(peer_ip, data):
                        aliases.aliases_update(node.index_db, node.ledger_path_conf, "normal", logger.app_log)

                        alias_address = connections.receive(self.request)

                        execute_param(database.index_cursor, ("SELECT alias FROM aliases WHERE address = ? "),
                                      (alias_address,))

                        result = database.index_cursor.fetchall()

                        if not result:
                            result = [[alias_address]]

                        connections.send(self.request, result)
                    else:
                        logger.app_log.info(f"{peer_ip} not whitelisted for aliasget command")

                elif data == "aliasesget":  # only gets the first one, for multiple addresses
                    # if (peer_ip in allowed or "any" in allowed):
                    if node.peers.is_allowed(peer_ip, data):
                        aliases.aliases_update(node.index_db, node.ledger_path_conf, "normal", logger.app_log)

                        aliases_request = connections.receive(self.request)

                        results = []
                        for alias_address in aliases_request:
                            execute_param(database.index_cursor, (
                                "SELECT alias FROM aliases WHERE address = ? ORDER BY block_height ASC LIMIT 1"),
                                          (alias_address,))
                            try:
                                result = database.index_cursor.fetchall()[0][0]
                            except:
                                result = alias_address
                            results.append(result)

                        connections.send(self.request, results)
                    else:
                        logger.app_log.info(f"{peer_ip} not whitelisted for aliasesget command")

                # Not mandatory, but may help to reindex with minimal sql queries
                elif data == "tokensupdate":
                    if node.peers.is_allowed(peer_ip, data):
                        tokens.tokens_update(node.index_db, node.ledger_path_conf, "normal", logger.app_log,
                                             node.plugin_manager)
                #
                elif data == "tokensget":
                    if node.peers.is_allowed(peer_ip, data):
                        tokens.tokens_update(node.index_db, node.ledger_path_conf, "normal", logger.app_log,
                                             node.plugin_manager)
                        tokens_address = connections.receive(self.request)

                        database.index_cursor.execute(
                            "SELECT DISTINCT token FROM tokens WHERE address OR recipient = ?", (tokens_address,))
                        tokens_user = database.index_cursor.fetchall()

                        tokens_list = []
                        for token in tokens_user:
                            token = token[0]
                            database.index_cursor.execute(
                                "SELECT sum(amount) FROM tokens WHERE recipient = ? AND token = ?;",
                                (tokens_address,) + (token,))
                            credit = database.index_cursor.fetchone()[0]
                            database.index_cursor.execute(
                                "SELECT sum(amount) FROM tokens WHERE address = ? AND token = ?;",
                                (tokens_address,) + (token,))
                            debit = database.index_cursor.fetchone()[0]

                            debit = 0 if debit is None else debit
                            credit = 0 if credit is None else credit

                            balance = str(Decimal(credit) - Decimal(debit))

                            tokens_list.append((token, balance))

                        connections.send(self.request, tokens_list)
                    else:
                        logger.app_log.info(f"{peer_ip} not whitelisted for tokensget command")

                elif data == "addfromalias":
                    if node.peers.is_allowed(peer_ip, data):

                        aliases.aliases_update(node.index_db, node.ledger_path_conf, "normal", logger.app_log)

                        alias_address = connections.receive(self.request)
                        database.index_cursor.execute(
                            "SELECT address FROM aliases WHERE alias = ? ORDER BY block_height ASC LIMIT 1;",
                            (alias_address,))  # asc for first entry
                        try:
                            address_fetch = database.index_cursor.fetchone()[0]
                        except:
                            address_fetch = "No alias"
                        logger.app_log.warning(f"Fetched the following alias address: {address_fetch}")

                        connections.send(self.request, address_fetch)

                    else:
                        logger.app_log.info(f"{peer_ip} not whitelisted for addfromalias command")

                elif data == "pubkeyget":
                    # if (peer_ip in allowed or "any" in allowed):
                    if node.peers.is_allowed(peer_ip, data):
                        pub_key_address = connections.receive(self.request)

                        database.c.execute(
                            "SELECT public_key FROM transactions WHERE address = ? and reward = 0 LIMIT 1",
                            (pub_key_address,))
                        target_public_key_hashed = database.c.fetchone()[0]
                        connections.send(self.request, target_public_key_hashed)

                    else:
                        logger.app_log.info(f"{peer_ip} not whitelisted for pubkeyget command")

                elif data == "aliascheck":
                    # if (peer_ip in allowed or "any" in allowed):
                    if node.peers.is_allowed(peer_ip, data):
                        reg_string = connections.receive(self.request)

                        registered_pending = mp.MEMPOOL.fetchone(
                            "SELECT timestamp FROM transactions WHERE openfield = ?;",
                            ("alias=" + reg_string,))

                        database.h3.execute("SELECT timestamp FROM transactions WHERE openfield = ?;",
                                            ("alias=" + reg_string,))
                        registered_already = database.h3.fetchone()

                        if registered_already is None and registered_pending is None:
                            connections.send(self.request, "Alias free")
                        else:
                            connections.send(self.request, "Alias registered")
                    else:
                        logger.app_log.info(f"{peer_ip} not whitelisted for aliascheck command")

                elif data == "txsend":
                    # if (peer_ip in allowed or "any" in allowed):
                    if node.peers.is_allowed(peer_ip, data):
                        tx_remote = connections.receive(self.request)

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
                        remote_tx = (str(remote_tx_timestamp), str(remote_tx_address), str(remote_tx_recipient),
                                     '%.8f' % quantize_eight(remote_tx_amount), str(remote_tx_operation),
                                     str(remote_tx_openfield))  # this is signed

                        remote_hash = SHA.new(str(remote_tx).encode("utf-8"))
                        remote_signer = PKCS1_v1_5.new(tx_remote_key)
                        remote_signature = remote_signer.sign(remote_hash)
                        remote_signature_enc = base64.b64encode(remote_signature).decode("utf-8")
                        # construct tx

                        # insert to mempool, where everything will be verified
                        mempool_data = ((str(remote_tx_timestamp), str(remote_tx_address), str(remote_tx_recipient),
                                         '%.8f' % quantize_eight(remote_tx_amount), str(remote_signature_enc),
                                         str(remote_tx_pubkey_hashed), str(remote_tx_operation),
                                         str(remote_tx_openfield)))

                        logger.app_log.info(mp.MEMPOOL.merge(mempool_data, peer_ip, database.c, True, True))

                        connections.send(self.request, str(remote_signature_enc))
                        # wipe variables
                        (tx_remote, remote_tx_privkey, tx_remote_key) = (None, None, None)
                    else:
                        logger.app_log.info(f"{peer_ip} not whitelisted for txsend command")

                # less important methods
                elif data == "addvalidate":
                    # if (peer_ip in allowed or "any" in allowed):
                    if node.peers.is_allowed(peer_ip, data):

                        address_to_validate = connections.receive(self.request)
                        if essentials.address_validate(address_to_validate):
                            result = "valid"
                        else:
                            result = "invalid"

                        connections.send(self.request, result)
                    else:
                        logger.app_log.info(f"{peer_ip} not whitelisted for addvalidate command")

                elif data == "annget":
                    # if (peer_ip in allowed or "any" in allowed):
                    if node.peers.is_allowed(peer_ip, data):

                        # with open(peerlist, "r") as peer_list:
                        #    peers_file = peer_list.read()
                        connections.send(self.request, ann_get(database.h3, node.genesis_conf))
                    else:
                        logger.app_log.info(f"{peer_ip} not whitelisted for annget command")

                elif data == "annverget":
                    # if (peer_ip in allowed or "any" in allowed):
                    if node.peers.is_allowed(peer_ip, data):

                        # with open(peerlist, "r") as peer_list:
                        #    peers_file = peer_list.read()
                        connections.send(self.request, ann_ver_get(database.h3, node.genesis_conf))

                    else:
                        logger.app_log.info(f"{peer_ip} not whitelisted for annget command")

                elif data == "peersget":
                    # if (peer_ip in allowed or "any" in allowed):
                    if node.peers.is_allowed(peer_ip, data):

                        # with open(peerlist, "r") as peer_list:
                        #    peers_file = peer_list.read()
                        connections.send(self.request, node.peers.peer_list_disk_format())

                    else:
                        logger.app_log.info(f"{peer_ip} not whitelisted for peersget command")

                elif data == "statusget":
                    # if (peer_ip in allowed or "any" in allowed):
                    if node.peers.is_allowed(peer_ip, data):

                        nodes_count = node.peers.consensus_size
                        nodes_list = node.peers.peer_ip_list
                        threads_count = threading.active_count()
                        uptime = int(time.time() - node.startup_time)
                        diff = difficulty(database.c)
                        server_timestamp = '%.2f' % time.time()

                        if node.reveal_address:
                            revealed_address = node_keys.address

                        else:
                            revealed_address = "private"

                        connections.send(self.request, (
                            revealed_address, nodes_count, nodes_list, threads_count, uptime, node.peers.consensus,
                            node.peers.consensus_percentage, VERSION, diff, server_timestamp))

                    else:
                        logger.app_log.info(f"{peer_ip} not whitelisted for statusget command")

                elif data == "statusjson":
                    if node.peers.is_allowed(peer_ip, data):
                        uptime = int(time.time() - node.startup_time)
                        tempdiff = difficulty(database.c)

                        if node.reveal_address:
                            revealed_address = node_keys.address
                        else:
                            revealed_address = "private"

                        status = {"protocolversion": node.version,
                                  "address": revealed_address,
                                  "walletversion": VERSION,
                                  "testnet": node.is_testnet,  # config data
                                  "blocks": node.last_block, "timeoffset": 0,
                                  "connections": node.peers.consensus_size,
                                  "connections_list": node.peers.peer_ip_list,
                                  "difficulty": tempdiff[0],  # live status, bitcoind format
                                  "threads": threading.active_count(),
                                  "uptime": uptime, "consensus": node.peers.consensus,
                                  "consensus_percent": node.peers.consensus_percentage,
                                  "server_timestamp": '%.2f' % time.time()}  # extra data
                        if node.is_regnet:
                            status['regnet'] = True
                        connections.send(self.request, status)
                    else:
                        logger.app_log.info(f"{peer_ip} not whitelisted for statusjson command")
                elif data[:4] == 'api_':
                    if node.peers.is_allowed(peer_ip, data):
                        try:
                            node.apihandler.dispatch(data, self.request, database.h3, node.peers)
                        except Exception as e:
                            print(e)

                elif data == "diffget":
                    # if (peer_ip in allowed or "any" in allowed):
                    if node.peers.is_allowed(peer_ip, data):
                        diff = difficulty(database.c)
                        connections.send(self.request, diff)
                    else:
                        logger.app_log.info(f"{peer_ip} not whitelisted for diffget command")

                elif data == "diffgetjson":
                    # if (peer_ip in allowed or "any" in allowed):
                    if node.peers.is_allowed(peer_ip, data):
                        diff = difficulty(database.c)
                        response = {"difficulty": diff[0],
                                    "diff_dropped": diff[0],
                                    "time_to_generate": diff[0],
                                    "diff_block_previous": diff[0],
                                    "block_time": diff[0],
                                    "hashrate": diff[0],
                                    "diff_adjustment": diff[0],
                                    "block_height": diff[0]}

                        connections.send(self.request, response)
                    else:
                        logger.app_log.info(f"{peer_ip} not whitelisted for diffgetjson command")

                elif data == "difflast":
                    # if (peer_ip in allowed or "any" in allowed):
                    if node.peers.is_allowed(peer_ip, data):

                        execute(database.h3,
                                ("SELECT block_height, difficulty FROM misc ORDER BY block_height DESC LIMIT 1"))
                        difflast = database.h3.fetchone()
                        connections.send(self.request, difflast)
                    else:
                        logger.app_log.info("f{peer_ip} not whitelisted for difflastget command")

                elif data == "difflastjson":
                    # if (peer_ip in allowed or "any" in allowed):
                    if node.peers.is_allowed(peer_ip, data):

                        execute(database.h3,
                                ("SELECT block_height, difficulty FROM misc ORDER BY block_height DESC LIMIT 1"))
                        difflast = database.h3.fetchone()
                        response = {"block": difflast[0],
                                    "difficulty": difflast[1]
                                    }
                        connections.send(self.request, response)
                    else:
                        logger.app_log.info(f"{peer_ip} not whitelisted for difflastjson command")

                elif data == "stop":
                    # if (peer_ip in allowed or "any" in allowed):
                    if node.peers.is_allowed(peer_ip, data):
                        logger.app_log.warning(f"Received stop from {peer_ip}")
                        node.IS_STOPPING = True


                elif data == "hyperlane":
                    pass

                else:
                    if data == '*':
                        raise ValueError("Broken pipe")
                    raise ValueError("Unexpected error, received: " + str(data)[:32] + ' ...')

                if not time.time() <= timer_operation + timeout_operation:
                    timer_operation = time.time()  # reset timer
                # time.sleep(float(node.pause_conf))  # prevent cpu overload
                logger.app_log.info(f"Server loop finished for {peer_ip}")

            except Exception as e:
                logger.app_log.info(f"Inbound: Lost connection to {peer_ip}")
                logger.app_log.info(f"Inbound: {e}")

                # remove from consensus (connection from them)
                node.peers.consensus_remove(peer_ip)
                # remove from consensus (connection from them)
                if self.request:
                    self.request.close()

                if node.debug_conf:
                    raise  # major debug client
                else:
                    return

        if not node.peers.version_allowed(peer_ip, node.version_allow):
            logger.app_log.warning(f"Inbound: Closing connection to old {peer_ip} node: {node.peers.ip_to_mainnet['peer_ip']}")


def ensure_good_peer_version(peer_ip):
    """
    cleanup after HF, kepts here for future use.
    """
    """
    # If we are post fork, but we don't know the version, then it was an old connection, close.
    if is_mainnet and (node.last_block >= POW_FORK) :
        if peer_ip not in node.peers.ip_to_mainnet:
            raise ValueError("Outbound: disconnecting old node {}".format(peer_ip));
        elif node.peers.ip_to_mainnet[peer_ip] not in node.version_allow:
            raise ValueError("Outbound: disconnecting old node {} - {}".format(peer_ip, node.peers.ip_to_mainnet[peer_ip]));
    """


# client thread
# if you "return" from the function, the exception code will node be executed and client thread will hang
def worker(HOST, PORT):
    if node.IS_STOPPING:
        return
    dict_ip = {'ip': HOST}
    node.plugin_manager.execute_filter_hook('peer_ip', dict_ip)
    if node.peers.is_banned(HOST) or dict_ip['ip'] == 'banned':
        logger.app_log.warning(f"IP {HOST} is banned, won't connect")
        return

    timeout_operation = 60  # timeout
    timer_operation = time.time()  # start counting


    this_worker = classes.Database()
    db_define(this_worker)

    try:
        this_client = (HOST + ":" + str(PORT))
        s = socks.socksocket()
        if node.tor_conf:
            s.setproxy(socks.PROXY_TYPE_SOCKS5, "127.0.0.1", 9050)
        # s.setblocking(0)
        s.connect((HOST, PORT))
        logger.app_log.info(f"Outbound: Connected to {this_client}")

        # communication starter

        connections.send(s, "version")
        connections.send(s, node.version)

        data = connections.receive(s)

        if data == "ok":
            logger.app_log.info(f"Outbound: Node protocol version of {this_client} matches our client")
        else:
            raise ValueError(f"Outbound: Node protocol version of {this_client} mismatch")

        # If we are post pow fork, then the peer has getversion command
        # if node.last_block >= POW_FORK - FORK_AHEAD:
        # Peers that are not up to date will disconnect since they don't know that command.
        # That is precisely what we need :D
        connections.send(s, "getversion")
        peer_version = connections.receive(s)
        if peer_version not in node.version_allow:
            raise ValueError(f"Outbound: Incompatible peer version {peer_version} from {this_client}")

        connections.send(s, "hello")

        # communication starter

    except Exception as e:
        logger.app_log.info(f"Could not connect to {this_client}: {e}")
        return  # can return here, because no lists are affected yet

    banned = False
    # if node.last_block >= POW_FORK - FORK_AHEAD:
    node.peers.store_mainnet(HOST, peer_version)
    try:
        peer_ip = s.getpeername()[0]
    except:
        # Should not happen, extra safety
        logger.app_log.warning("Outbound: Transport endpoint was not connected")
        return

    if this_client not in node.peers.connection_pool:
        node.peers.append_client(this_client)
        logger.app_log.info(f"Connected to {this_client}")
        logger.app_log.info(f"Current active pool: {node.peers.connection_pool}")




    while not banned and node.peers.version_allowed(HOST, node.version_allow) and not node.IS_STOPPING:
        try:
            ensure_good_peer_version(HOST)

            data = connections.receive(s)  # receive data, one and the only root point
            # print(data)

            if data == "peers":
                subdata = connections.receive(s)
                node.peers.peersync(subdata)

            elif data == "sync":
                if not time.time() <= timer_operation + timeout_operation:
                    timer_operation = time.time()  # reset timer

                try:
                    while len(node.syncing) >= 3:
                        if node.IS_STOPPING:
                            return
                        time.sleep(int(node.pause_conf))

                    node.syncing.append(peer_ip)
                    # sync start

                    # send block height, receive block height
                    connections.send(s, "blockheight")

                    execute(this_worker.c, ('SELECT max(block_height) FROM transactions'))
                    db_block_height = this_worker.c.fetchone()[0]

                    logger.app_log.info(f"Outbound: Sending block height to compare: {db_block_height}")
                    # append zeroes to get static length
                    connections.send(s, db_block_height)

                    received_block_height = connections.receive(s)  # receive node's block height
                    logger.app_log.info(
                        f"Outbound: Node {peer_ip} is at block height: {received_block_height}")

                    if int(received_block_height) < db_block_height:
                        logger.app_log.warning(
                            f"Outbound: We have a higher block ({db_block_height}) than {peer_ip} ({received_block_height}), sending")

                        data = connections.receive(s)  # receive client's last block_hash

                        # send all our followup hashes
                        logger.app_log.info(f"Outbound: Will seek the following block: {data}")

                        # consensus pool 2 (active connection)
                        consensus_blockheight = int(received_block_height)
                        node.peers.consensus_add(peer_ip, consensus_blockheight, s, node.last_block)
                        # consensus pool 2 (active connection)

                        try:
                            execute_param(this_worker.h3, ("SELECT block_height FROM transactions WHERE block_hash = ?;"),
                                          (data,))
                            client_block = this_worker.h3.fetchone()[0]

                            logger.app_log.info(
                                f"Outbound: Node is at block {client_block}")  # now check if we have any newer

                            execute(this_worker.h3,
                                    ('SELECT block_hash FROM transactions ORDER BY block_height DESC LIMIT 1'))
                            db_block_hash = this_worker.h3.fetchone()[0]  # get latest block_hash

                            if db_block_hash == data or not node.egress:
                                if not node.egress:
                                    logger.app_log.warning(f"Outbound: Egress disabled for {peer_ip}")
                                    time.sleep(int(node.pause_conf))  # reduce CPU usage
                                else:
                                    logger.app_log.info(f"Outbound: Node {peer_ip} has the latest block")
                                    # TODO: this is unlikely to happen due to conditions above, consider removing
                                connections.send(s, "nonewblk")

                            else:
                                blocks_fetched = []
                                while sys.getsizeof(
                                        str(blocks_fetched)) < 500000:  # limited size based on txs in blocks
                                    # execute_param(h3, ("SELECT block_height, timestamp,address,recipient,amount,signature,public_key,keep,openfield FROM transactions WHERE block_height > ? AND block_height <= ?;"),(str(int(client_block)),) + (str(int(client_block + 1)),))
                                    execute_param(this_worker.h3, (
                                        "SELECT timestamp,address,recipient,amount,signature,public_key,cast(operation as TEXT),openfield FROM transactions WHERE block_height > ? AND block_height <= ?;"),
                                                  (str(int(client_block)), str(int(client_block + 1)),))
                                    result = this_worker.h3.fetchall()
                                    if not result:
                                        break
                                    blocks_fetched.extend([result])
                                    client_block = int(client_block) + 1

                                # blocks_send = [[l[1:] for l in group] for _, group in groupby(blocks_fetched, key=itemgetter(0))]  # remove block number

                                logger.app_log.info(f"Outbound: Selected {blocks_fetched}")

                                connections.send(s, "blocksfnd")

                                confirmation = connections.receive(s)

                                if confirmation == "blockscf":
                                    logger.app_log.info("Outbound: Client confirmed they want to sync from us")
                                    connections.send(s, blocks_fetched)

                                elif confirmation == "blocksrj":
                                    logger.app_log.info(
                                        "Outbound: Client rejected to sync from us because we're dont have the latest block")
                                    pass

                        except Exception as e:
                            logger.app_log.warning(f"Outbound: Block {data[:8]} of {peer_ip} not found")
                            connections.send(s, "blocknf")
                            connections.send(s, data)

                    elif int(received_block_height) >= db_block_height:
                        if int(received_block_height) == db_block_height:
                            logger.app_log.info(f"Outbound: We have the same block as {peer_ip} ({received_block_height}), hash will be verified")
                        else:
                            logger.app_log.warning(f"Outbound: We have a lower block ({db_block_height}) than {peer_ip} ({received_block_height}), hash will be verified")

                        execute(this_worker.c, ('SELECT block_hash FROM transactions ORDER BY block_height DESC LIMIT 1'))
                        db_block_hash = this_worker.c.fetchone()[0]  # get latest block_hash

                        logger.app_log.info(f"Outbound: block_hash to send: {db_block_hash}")
                        connections.send(s, db_block_hash)

                        ensure_good_peer_version(HOST)

                        # consensus pool 2 (active connection)
                        consensus_blockheight = int(received_block_height)  # str int to remove leading zeros
                        node.peers.consensus_add(peer_ip, consensus_blockheight, s, node.last_block)
                        # consensus pool 2 (active connection)

                except Exception as e:
                    logger.app_log.info(f"Outbound: Sync failed {e}")
                finally:
                    node.syncing.remove(peer_ip)

            elif data == "blocknf":  # one of the possible outcomes
                block_hash_delete = connections.receive(s)
                # print peer_ip
                # if max(consensus_blockheight_list) == int(received_block_height):
                if int(received_block_height) == node.peers.consensus_max:
                    blocknf(block_hash_delete, peer_ip, this_worker.c, this_worker.conn, this_worker.h, this_worker.hdd,
                            this_worker.h2, this_worker.hdd2)

                    if node.peers.warning(s, peer_ip, "Rollback", 2):
                        raise ValueError(f"{peer_ip} is banned")

                sendsync(s, peer_ip, "Block not found", False)

            elif data == "blocksfnd":
                logger.app_log.info(f"Outbound: Node {peer_ip} has the block(s)")  # node should start sending txs in this step

                # logger.app_log.info("Inbound: Combined segments: " + segments)
                # print peer_ip
                if db_lock.locked():
                    logger.app_log.warning(f"Skipping sync from {peer_ip}, syncing already in progress")

                else:
                    execute(this_worker.c,
                            "SELECT timestamp FROM transactions WHERE reward != 0 ORDER BY block_height DESC LIMIT 1;")  # or it takes the first
                    node.last_block_ago = Decimal(this_worker.c.fetchone()[0])

                    if int(node.last_block_ago) < (time.time() - 600):
                        block_req = node.peers.consensus_most_common
                        logger.app_log.warning("Most common block rule triggered")

                    else:
                        block_req = node.peers.consensus_max
                        logger.app_log.warning("Longest chain rule triggered")

                    ensure_good_peer_version(HOST)

                    if int(received_block_height) >= block_req:
                        try:  # they claim to have the longest chain, things must go smooth or ban
                            logger.app_log.warning(f"Confirming to sync from {peer_ip}")

                            connections.send(s, "blockscf")
                            segments = connections.receive(s)
                            ensure_good_peer_version(HOST)

                        except:
                            if node.peers.warning(s, peer_ip, "Failed to deliver the longest chain", 2):
                                raise ValueError(f"{peer_ip} is banned")

                        else:
                            digest_block(segments, s, peer_ip, this_worker.conn, this_worker.c, this_worker.hdd,
                                         this_worker.h, this_worker.hdd2, this_worker.h2, this_worker.h3, this_worker.index,
                                         this_worker.index_cursor)

                            # receive theirs
                    else:
                        connections.send(s, "blocksrj")
                        logger.app_log.warning(f"Inbound: Distant peer {peer_ip} is at {received_block_height}, should be at least {block_req}")

                sendsync(s, peer_ip, "Block found", True)

                # block_hash validation end

            elif data == "nonewblk":
                # send and receive mempool
                if mp.MEMPOOL.sendable(peer_ip):
                    mempool_txs = mp.MEMPOOL.tx_to_send(peer_ip)
                    # logger.app_log.info("Outbound: Extracted from the mempool: " + str(mempool_txs))  # improve: sync based on signatures only
                    # if len(mempool_txs) > 0: #wont sync mempool until we send something, which is bad
                    # send own
                    connections.send(s, "mempool")
                    connections.send(s, mempool_txs)
                    # send own
                    # receive theirs
                    segments = connections.receive(s)
                    logger.app_log.info(mp.MEMPOOL.merge(segments, peer_ip, this_worker.c, True))
                    # receive theirs
                    # Tell the mempool we just send our pool to a peer
                    mp.MEMPOOL.sent(peer_ip)
                sendsync(s, peer_ip, "No new block", True)

            elif data == "hyperlane":
                pass

            else:
                if data == '*':
                    raise ValueError("Broken pipe")
                raise ValueError(f"Unexpected error, received: {str(data)[:32]}")

        except Exception as e:
            """
            exc_type, exc_obj, exc_tb = sys.exc_info()
            fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
            print(exc_type, fname, exc_tb.tb_lineno)
            """
            # remove from active pool
            if this_client in node.peers.connection_pool:
                logger.app_log.info(
                    f"Will remove {this_client} from active pool {node.peers.connection_pool}")
                logger.app_log.warning(f"Outbound: Disconnected from {this_client}: {e}")

                node.peers.remove_client(this_client)

            # remove from active pool

            # remove from consensus 2
            try:
                node.peers.consensus_remove(peer_ip)
            except:
                pass
            # remove from consensus 2

            logger.app_log.info(f"Connection to {this_client} terminated due to {e}")
            logger.app_log.info(f"---thread {threading.currentThread()} ended---")

            # properly end the connection
            if s:
                s.close()
            # properly end the connection
            if node.debug_conf:
                raise  # major debug client
            else:
                logger.app_log.info(f"Ending thread, because {e}")
                return

    if not node.peers.version_allowed(HOST, node.version_allow):
        logger.app_log.warning(f"Outbound: Ending thread, because {HOST} has too old a version: {node.peers.ip_to_mainnet[HOST]}")


class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    pass


def just_int_from(s):
    return int(''.join(i for i in s if i.isdigit()))


def setup_net_type():
    """
    Adjust globals depending on mainnet, testnet or regnet
    """
    # Defaults value, dup'd here for clarity sake.
    node.is_mainnet = True
    node.is_testnet = False
    node.is_regnet = False

    if "testnet" in node.version or config.testnet:
        node.is_testnet = True
        node.is_mainnet = False

    if "regnet" in node.version or config.regnet:
        node.is_regnet = True
        node.is_testnet = False
        node.is_mainnet = False

    logger.app_log.warning(f"Testnet: {node.is_testnet}")
    logger.app_log.warning(f"Regnet : {node.is_regnet}")

    # default mainnet config
    node.peerlist = "peers.txt"
    node.ledger_ram_file = "file:ledger?mode=memory&cache=shared"
    node.index_db = "static/index.db"

    if node.is_mainnet:
        # Allow 18 for transition period. Will be auto removed at fork block.
        if node.version != 'mainnet0020':
            node.version = 'mainnet0019'  # Force in code.
        if "mainnet0020" not in node.version_allow:
            node.version_allow = ['mainnet0019', 'mainnet0020', 'mainnet0021']
        # Do not allow bad configs.
        if not 'mainnet' in node.version:
            logger.app_log.error("Bad mainnet version, check config.txt")
            sys.exit()
        num_ver = just_int_from(node.version)
        if num_ver < 19:
            logger.app_log.error("Too low mainnet version, check config.txt")
            sys.exit()
        for allowed in node.version_allow:
            num_ver = just_int_from(allowed)
            if num_ver < 19:
                logger.app_log.error("Too low allowed version, check config.txt")
                sys.exit()

    if node.is_testnet:
        node.port = 2829
        node.full_ledger = False
        node.hyper_path_conf = "static/test.db"
        node.ledger_path_conf = "static/test.db"  # for tokens
        node.ledger_ram_file = "file:ledger_testnet?mode=memory&cache=shared"
        node.hyper_recompress_conf = False
        node.peerlist = "peers_test.txt"
        node.index_db = "static/index_test.db"
        if not 'testnet' in node.version:
            logger.app_log.error("Bad testnet version, check config.txt")
            sys.exit()

        redownload_test = input("Status: Welcome to the testnet. Redownload test ledger? y/n")
        if redownload_test == "y" or not os.path.exists("static/test.db"):
            types = ['static/test.db-wal', 'static/test.db-shm', 'static/index_test.db']
            for type in types:
                for file in glob.glob(type):
                    os.remove(file)
                    print(file, "deleted")
            download_file("https://bismuth.cz/test.db", "static/test.db")
            download_file("https://bismuth.cz/index_test.db", "static/index_test.db")
        else:
            print("Not redownloading test db")

    if node.is_regnet:
        node.port = regnet.REGNET_PORT
        node.full_ledger = False
        node.hyper_path_conf = regnet.REGNET_DB
        node.ledger_path_conf = regnet.REGNET_DB
        node.ledger_ram_file = "file:ledger_regnet?mode=memory&cache=shared"
        node.hyper_recompress_conf = False
        node.peerlist = regnet.REGNET_PEERS
        node.index_db = regnet.REGNET_INDEX
        if not 'regnet' in node.version:
            logger.app_log.error("Bad regnet version, check config.txt")
            sys.exit()
        logger.app_log.warning("Regnet init...")
        regnet.init(logger.app_log)
        regnet.DIGEST_BLOCK = digest_block
        mining_heavy3.is_regnet = True
        """
        logger.app_log.warning("Regnet still is WIP atm.")
        sys.exit()
        """


def initial_db_check():
    """
    Initial bootstrap check and chain validity control
    """
    # force bootstrap via adding an empty "fresh_sync" file in the dir.
    if os.path.exists("fresh_sync") and node.is_mainnet:
        logger.app_log.warning("Status: Fresh sync required, bootstrapping from the website")
        os.remove("fresh_sync")
        bootstrap()
    # UPDATE mainnet DB if required
    if node.is_mainnet:
        upgrade = sqlite3.connect(node.ledger_path_conf)
        u = upgrade.cursor()
        try:
            u.execute("PRAGMA table_info(transactions);")
            result = u.fetchall()[10][2]
            if result != "TEXT":
                raise ValueError("Database column type outdated for Command field")
            upgrade.close()
        except Exception as e:
            print(e)
            upgrade.close()
            print("Database needs upgrading, bootstrapping...")
            bootstrap()

    logger.app_log.warning(f"Status: Indexing tokens from ledger {node.ledger_path_conf}")
    tokens.tokens_update(node.index_db, node.ledger_path_conf, "normal", logger.app_log, node.plugin_manager)
    logger.app_log.warning("Status: Indexing aliases")
    aliases.aliases_update(node.index_db, node.ledger_path_conf, "normal", logger.app_log)

    try:
        source_db = sqlite3.connect(node.hyper_path_conf, timeout=1)
        source_db.text_factory = str
        sc = source_db.cursor()

        sc.execute("SELECT max(block_height) FROM transactions")
        node.hdd_block = sc.fetchone()[0]

        node.last_block = node.hdd_block

        if node.is_mainnet and (node.hdd_block >= POW_FORK - FORK_AHEAD):
            limit_version()

        if node.ram_conf:
            logger.app_log.warning("Status: Moving database to RAM")
            database.to_ram = sqlite3.connect(node.ledger_ram_file, uri=True, timeout=1, isolation_level=None)
            database.to_ram.text_factory = str
            database.tr = database.to_ram.cursor()

            query = "".join(line for line in source_db.iterdump())
            database.to_ram.executescript(query)
            # do not close
            logger.app_log.warning("Status: Moved database to RAM")

    except Exception as e:
        logger.app_log.error(e)
        sys.exit()


def load_keys():
    """Initial loading of crypto keys"""

    essentials.keys_check(logger.app_log, "wallet.der")

    node_keys.key, node_keys.public_key_readable, node_keys.private_key_readable, _, _, node_keys.public_key_hashed, node_keys.address, node_keys.keyfile = essentials.keys_load(
        "privkey.der", "pubkey.der")

    if node.is_regnet:
        regnet.PRIVATE_KEY_READABLE = node_keys.private_key_readable
        regnet.PUBLIC_KEY_HASHED = node_keys.public_key_hashed
        regnet.ADDRESS = node_keys.address
        regnet.KEY = node_keys.key

    logger.app_log.warning(f"Status: Local address: {node_keys.address}")

def verify(h3):
    try:
        logger.app_log.warning("Blockchain verification started...")
        # verify blockchain
        execute(h3, ("SELECT Count(*) FROM transactions"))
        db_rows = h3.fetchone()[0]
        logger.app_log.warning("Total steps: {}".format(db_rows))

        # verify genesis
        if node.full_ledger:
            execute(h3, ("SELECT block_height, recipient FROM transactions WHERE block_height = 1"))
            result = h3.fetchall()[0]
            block_height = result[0]
            genesis = result[1]
            logger.app_log.warning("Genesis: {}".format(genesis))
            if str(genesis) != node.genesis_conf and int(
                    block_height) == 0:  # change this line to your genesis address if you want to clone
                node.app_log.warning("Invalid genesis address")
                sys.exit(1)
        # verify genesis

        db_hashes = {
            '27258-1493755375.23': 'acd6044591c5baf121e581225724fc13400941c7',
            '27298-1493755830.58': '481ec856b50a5ae4f5b96de60a8eda75eccd2163',
            '30440-1493768123.08': 'ed11b24530dbcc866ce9be773bfad14967a0e3eb',
            '32127-1493775151.92': 'e594d04ad9e554bce63593b81f9444056dd1705d',
            '32128-1493775170.17': '07a8c49d00e703f1e9518c7d6fa11d918d5a9036',
            '37732-1493799037.60': '43c064309eff3b3f065414d7752f23e1de1e70cd',
            '37898-1493799317.40': '2e85b5c4513f5e8f3c83a480aea02d9787496b7a',
            '37898-1493799774.46': '4ea899b3bdd943a9f164265d51b9427f1316ce39',
            '38083-1493800650.67': '65e93aab149c7e77e383e0f9eb1e7f9a021732a0',
            '52233-1493876901.73': '29653fdefc6ca98aadeab37884383fedf9e031b3',
            '52239-1493876963.71': '4c0e262de64a5e792601937a333ca2bf6d6681f2',
            '52282-1493877169.29': '808f90534e7ba68ee60bb2ea4530f5ff7b9d8dea',
            '52308-1493877257.85': '8919548fdbc5093a6e9320818a0ca058449e29c2',
            '52393-1493877463.97': '0eba7623a44441d2535eafea4655e8ef524f3719',
            '62507-1493946372.50': '81c9ca175d09f47497a57efeb51d16ee78ddc232',
            '70094-1494032933.14': '2ca4403387e84b95ed558e7c9350c43efff8225c'
        }
        invalid = 0
        for row in execute(h3,
                           ('SELECT * FROM transactions WHERE block_height > 1 and reward = 0 ORDER BY block_height')):

            db_block_height = str(row[0])
            db_timestamp = '%.2f' % (quantize_two(row[1]))
            db_address = str(row[2])[:56]
            db_recipient = str(row[3])[:56]
            db_amount = '%.8f' % (quantize_eight(row[4]))
            db_signature_enc = str(row[5])[:684]
            db_public_key_hashed = str(row[6])[:1068]
            db_public_key = RSA.importKey(base64.b64decode(db_public_key_hashed))
            db_operation = str(row[10])[:30]
            db_openfield = str(row[11])  # no limit for backward compatibility

            db_transaction = (db_timestamp, db_address, db_recipient, db_amount, db_operation, db_openfield)

            db_signature_dec = base64.b64decode(db_signature_enc)
            verifier = PKCS1_v1_5.new(db_public_key)
            hash = SHA.new(str(db_transaction).encode("utf-8"))
            if verifier.verify(hash, db_signature_dec):
                pass
            else:
                try:
                    if hash.hexdigest() != db_hashes[db_block_height + "-" + db_timestamp]:
                        logger.app_log.warning("Signature validation problem: {} {}".format(db_block_height, db_transaction))
                        invalid = invalid + 1
                except:
                    logger.app_log.warning("Signature validation problem: {} {}".format(db_block_height, db_transaction))
                    invalid = invalid + 1

        if invalid == 0:
            logger.app_log.warning("All transacitons in the local ledger are valid")

    except Exception as e:
        logger.app_log.warning("Error: {}".format(e))
        raise

if __name__ == "__main__":

    # classes
    node = classes.Node()
    logger = classes.Logger()
    node_keys = classes.Keys()
    database = classes.Database()

    node.is_testnet = False
    # regnet takes over testnet
    node.is_regnet = False
    # if it's not testnet, nor regnet, it's mainnet
    node.is_mainnet = True

    config = options.Get()
    config.read()
    # classes

    node.version = config.version_conf
    node.debug_level = config.debug_level_conf
    node.port = config.port
    node.verify_conf = config.verify_conf
    node.thread_limit_conf = config.thread_limit_conf
    node.rebuild_db_conf = config.rebuild_db_conf
    node.debug_conf = config.debug_conf
    node.debug_level_conf = config.debug_level_conf
    node.pause_conf = config.pause_conf
    node.ledger_path_conf = config.ledger_path_conf
    node.hyper_path_conf = config.hyper_path_conf
    node.hyper_recompress_conf = config.hyper_recompress_conf
    node.tor_conf = config.tor_conf
    node.ram_conf = config.ram_conf
    node.version_allow = config.version_allow
    node.full_ledger = config.full_ledger_conf
    node.reveal_address = config.reveal_address
    node.terminal_output = config.terminal_output
    node.egress = config.egress
    node.genesis_conf = config.genesis_conf

    node.IS_STOPPING = False

    logger.app_log = log.log("node.log", node.debug_level_conf, node.terminal_output)
    logger.app_log.warning("Configuration settings loaded")

    # upgrade wallet location after nuitka-required "files" folder introduction
    if os.path.exists("../wallet.der") and not os.path.exists("wallet.der") and "Windows" in platform.system():
        print("Upgrading wallet location")
        os.rename("../wallet.der", "wallet.der")
    # upgrade wallet location after nuitka-required "files" folder introduction

    mining_heavy3.mining_open()
    try:
        # create a plugin manager, load all plugin modules and init
        node.plugin_manager = plugins.PluginManager(app_log=logger.app_log, init=True)

        setup_net_type()
        load_keys()

        ledger_compress()
        db_define(database)

        coherence_check()
        check_integrity(node.hyper_path_conf)

        initial_db_check()

        if node.rebuild_db_conf:
            db_maintenance()

        if node.verify_conf:
            verify(database.h3)

        logger.app_log.warning(f"Status: Starting node version {VERSION}")
        node.startup_time = time.time()
        try:

            node.peers = peershandler.Peers(logger.app_log, config)

            # print(peers.peer_list_old_format())
            # sys.exit()

            node.apihandler = apihandler.ApiHandler(logger.app_log, config)
            mp.MEMPOOL = mp.Mempool(logger.app_log, config, db_lock, node.is_testnet)

            if not node.tor_conf:
                # Port 0 means to select an arbitrary unused port
                HOST, PORT = "0.0.0.0", int(node.port)

                ThreadedTCPServer.allow_reuse_address = True
                ThreadedTCPServer.daemon_threads = True
                ThreadedTCPServer.timeout = 60
                ThreadedTCPServer.request_queue_size = 100

                server = ThreadedTCPServer((HOST, PORT), ThreadedTCPRequestHandler)
                ip, node.port = server.server_address

                # Start a thread with the server -- that thread will then start one
                # more thread for each request

                server_thread = threading.Thread(target=server.serve_forever)

                # Exit the server thread when the main thread terminates

                server_thread.daemon = True
                server_thread.start()
                logger.app_log.warning("Status: Server loop running.")
            else:
                logger.app_log.warning("Status: Not starting a local server to conceal identity on Tor network")

            # hyperlane_manager = hyperlane.HyperlaneManager(logger.app_log).hyperlane_manager()
            # hyperlane_manager.start()

            # start connection manager
            t_manager = threading.Thread(target=manager(database.c))
            logger.app_log.warning("Status: Starting connection manager")
            t_manager.daemon = True
            t_manager.start()
            # start connection manager

            if not node.is_regnet:
                # regnet mode does not need any specific attention.
                logger.app_log.warning("Closing in 10 sec...")
                time.sleep(10)
            # server.serve_forever() #added
            server.shutdown()
            server.server_close()
            mp.MEMPOOL.close()
            # TODO: VACUUM THE DBs?

        except Exception as e:
            logger.app_log.info(e)
            raise
    finally:
        mining_heavy3.mining_close()