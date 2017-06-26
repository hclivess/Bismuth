# it's recommended to only use code from the "release" state, running master blob may have issues
# never remove the str() conversion in data evaluation or database inserts or you will debug for 14 days as signed types mismatch
# never change the type of database columns from TEXT to anything else
# if you raise in the server thread, the server will die and node will stop
# must unify node and client now that connections parameters are function parameters

from itertools import groupby
from operator import itemgetter
import shutil, socketserver, ast, base64, gc, hashlib, os, re, sqlite3, sys, threading, time, socks, log, options, connections, random

from Crypto import Random
from Crypto.Hash import SHA
from Crypto.PublicKey import RSA
from Crypto.Signature import PKCS1_v1_5

# load config
global warning_list_limit_conf
global peersync
peersync = 0
global ram_done
global hdd_block
ram_done = 0

(port, genesis_conf, verify_conf, version_conf, thread_limit_conf, rebuild_db_conf, debug_conf, purge_conf, pause_conf, ledger_path_conf, hyperblocks_conf, warning_list_limit_conf, tor_conf, debug_level_conf, allowed, mining_ip_conf, sync_conf, mining_threads_conf, diff_recalc_conf, pool_conf, pool_address, ram_conf) = options.read()

# load config

def db_to_drive():
    global hdd_block

    app_log.warning("Moving new data to HDD")
    hdd = sqlite3.connect(ledger_path_conf)
    hdd.text_factory = str
    h = hdd.cursor()

    old_db = sqlite3.connect('file::memory:?cache=shared', uri=True)

    old_db.text_factory = str
    o = old_db.cursor()

    for row in o.execute("SELECT * FROM transactions where block_height > {} ORDER BY block_height ASC".format(hdd_block)):
        h.execute("INSERT INTO transactions VALUES {}".format(row))
        commit(hdd)

    h.execute("SELECT block_height FROM transactions ORDER BY block_height DESC LIMIT 1")
    hdd_block = h.fetchone()[0]
    hdd.close()

def db_c_define():
    global hdd_block

    if ram_conf == 1:
        try:
            conn = sqlite3.connect('file::memory:?cache=shared',uri=True)
            conn.text_factory = str
            c = conn.cursor()
        except Exception as e:
            app_log.info(e)

    else:
        try:
            conn = sqlite3.connect(ledger_path_conf)
            conn.text_factory = str
            c = conn.cursor()
        except Exception as e:
            app_log.info(e)

    return conn, c


def db_m_define():
    mempool = sqlite3.connect('mempool.db')
    mempool.text_factory = str
    m = mempool.cursor()
    return mempool, m

app_log = log.log("node.log", debug_level_conf)
version = version_conf

app_log.warning("Configuration settings loaded")

def unban(ip):
    global warning_list
    global banlist

    warning_list = [x for x in warning_list if x != ip]
    banlist = [x for x in banlist if x != ip]


def warning(sdef, ip):
    global warning_list
    global warning_list_limit_conf

    warning_list.append(ip)
    app_log.info("Added a warning to {} ({} / {})".format(ip, warning_list.count(ip), warning_list_limit_conf))

    if warning_list.count(ip) >= warning_list_limit_conf:
        banlist.append(ip)
        sdef.close()
        raise ValueError("{} banned".format(ip))  # rework this


def ledger_convert():
    try:
        app_log.warning("Converting ledger to Hyperblocks")
        depth = 10000

        shutil.copy(ledger_path_conf, ledger_path_conf + '.hyper')
        hyper = sqlite3.connect(ledger_path_conf + '.hyper')
        hyper.text_factory = str
        h = hyper.cursor()

        end_balance = 0
        addresses = []

        h.execute("UPDATE transactions SET address = 'Hypoblock' WHERE address = 'Hyperblock'")

        h.execute("SELECT block_height FROM transactions ORDER BY block_height DESC LIMIT 1;")
        db_block_height = h.fetchone()[0]

        for row in h.execute("SELECT * FROM transactions WHERE (block_height < ? AND keep = '0') ORDER BY block_height;",
                             (str(int(db_block_height) - depth),)):
            db_address = row[2]
            db_recipient = row[3]
            addresses.append(db_address.strip())
            addresses.append(db_recipient.strip())

        unique_addressess = set(addresses)

        for x in set(unique_addressess):
            h.execute("SELECT sum(amount) FROM transactions WHERE (recipient = ? AND block_height < ?  AND keep = '0');", (x,) + (str(int(db_block_height) - depth),))
            credit = h.fetchone()[0]
            if credit == None:
                credit = 0

            h.execute("SELECT sum(amount),sum(fee),sum(reward) FROM transactions WHERE (address = ? AND block_height < ? AND keep = '0');", (x,) + (str(int(db_block_height) - depth),))
            result = h.fetchall()
            debit = result[0][0]
            if debit == None:
                debit = 0

            fees = result[0][1]
            if fees == None:
                fees = 0

            rewards = result[0][2]
            if rewards == None:
                rewards = 0

            end_balance = credit - debit - fees + rewards
            #app_log.info("Address: "+ str(x))
            #app_log.info("Credit: " + str(credit))
            #app_log.info("Debit: " + str(debit))
            #app_log.info("Fees: " + str(fees))
            #app_log.info("Rewards: " + str(rewards))
            #app_log.info("Balance: " + str(end_balance))

            # test for keep positivity
            h.execute("SELECT block_height FROM transactions WHERE address OR recipient = ?", (x,))
            keep_is = 1
            try:
                h.fetchone()[0]
            except:
                keep_is = 0
            # test for keep positivity

            if end_balance > 0 or keep_is == 1:
                timestamp = str(time.time())
                h.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
                    db_block_height - depth - 1, timestamp, "Hyperblock", x, '%.8f' % float(end_balance), "0", "0", "0", "0", "0",
                    "0", "0"))
                hyper.commit()

        h.execute("DELETE FROM transactions WHERE block_height < ? AND address != 'Hyperblock' AND keep = '0';", (str(int(db_block_height) - depth),))
        hyper.commit()

        h.execute("VACUUM")
        hyper.close()

        os.remove(ledger_path_conf)
        os.rename(ledger_path_conf + '.hyper', ledger_path_conf)
    except Exception as e:
        raise ValueError("There was an issue converting to Hyperblocks: {}".format(e))


def most_common(lst):
    return max(set(lst), key=lst.count)


def bin_convert(string):
    return ''.join(format(ord(x), 'b') for x in string)


def commit(cursor):
    # secure commit for slow nodes
    passed = 0
    while passed == 0:
        try:
            cursor.commit()
            passed = 1
        except:
            app_log.info("Retrying database execute due to " + str(e))
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
            app_log.info("Retrying database execute due to {}".format(e))
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
            app_log.info("Retrying database execute due to " + str(e))
            time.sleep(0.1)
            pass
            # secure execute for slow nodes
    return cursor


gc.enable()

global active_pool
active_pool = []
global peer_ip_list
peer_ip_list = []
global consensus_blockheight_list
consensus_blockheight_list = []
global tried
tried = []
global consensus_percentage
consensus_percentage = ""
global warning_list
warning_list = []
global banlist
banlist = []
global busy
busy = 0
global busy_mempool
busy_mempool = 0
global consensus
consensus = ""
global syncing
syncing = []


# port = 2829 now defined by config

def mempool_merge(data, peer_ip, conn, c, mempool, m):
    global busy_mempool
    if busy_mempool == 0:
        busy_mempool = 1

        if data == "[]":
            app_log.info("Mempool from {} was empty".format(peer_ip))
            busy_mempool = 0
        else:
            # app_log.info("Mempool merging started")
            # merge mempool

            try:
                block_list = ast.literal_eval(data)

                for transaction in block_list:  # set means unique
                    mempool_timestamp = '%.2f' % float(transaction[0])
                    mempool_address = str(transaction[1][:56])
                    mempool_recipient = str(transaction[2][:56])
                    mempool_amount = '%.8f' % float(transaction[3])
                    mempool_signature_enc = str(transaction[4])
                    mempool_public_key_hashed = str(transaction[5])
                    mempool_keep = str(transaction[6])
                    mempool_openfield = str(transaction[7])
                    mempool_public_key = RSA.importKey(base64.b64decode(mempool_public_key_hashed))  # convert readable key to instance
                    mempool_signature_dec = base64.b64decode(mempool_signature_enc)

                    ledger_in = 0
                    mempool_in = 0

                    acceptable = 1
                    try:
                        execute_param(m, ("SELECT * FROM transactions WHERE signature = ?;"),
                                      (mempool_signature_enc,))  # condition 1)
                        dummy1 = m.fetchall()[0]
                        if dummy1 != None:
                            # app_log.info("That transaction is already in our mempool")
                            acceptable = 0
                            mempool_in = 1
                    except:
                        pass

                    try:
                        # reject transactions which are already in the ledger
                        execute_param(c, ("SELECT * FROM transactions WHERE signature = ?;"),
                                      (mempool_signature_enc,))  # condition 2
                        dummy2 = c.fetchall()[0]
                        if dummy2 != None:
                            # app_log.info("That transaction is already in our ledger")
                            # reject transactions which are already in the ledger
                            acceptable = 0
                            ledger_in = 1
                    except:
                        pass

                    if mempool_keep != "1" or mempool_keep != "0":
                        app_log.info = ("Wrong keep value")
                        acceptable = 0

                    if mempool_address != hashlib.sha224(base64.b64decode(mempool_public_key_hashed)).hexdigest():
                        app_log.info("Attempt to spend from a wrong address")
                        acceptable = 0

                    if float(mempool_amount) < 0:
                        acceptable = 0
                        app_log.info("Negative balance spend attempt")

                    if float(mempool_timestamp) > time.time() + 30:  # dont accept future txs
                        acceptable = 0

                    if float(mempool_timestamp) < time.time() - 86400:  # dont accept old txs
                        acceptable = 0

                    if (mempool_in == 1) and (
                                ledger_in == 1):  # remove from mempool if it's in both ledger and mempool already
                        try:
                            execute_param(m, ("DELETE FROM transactions WHERE signature = ?;"), (mempool_signature_enc,))
                            commit(mempool)
                            app_log.info("Transaction deleted from our mempool")
                        except:  # experimental try and except
                            app_log.info("Transaction was not present in the pool anymore")
                            pass  # continue to mempool finished message

                            # verify signatures and balances

                    # verify signature
                    verifier = PKCS1_v1_5.new(mempool_public_key)

                    h = SHA.new(str((mempool_timestamp, mempool_address, mempool_recipient, mempool_amount, mempool_keep, mempool_openfield)).encode("utf-8"))
                    if not verifier.verify(h, mempool_signature_dec):
                        acceptable = 0
                        app_log.info("Wrong signature in mempool insert attempt")
                    # verify signature

                    if acceptable == 1:

                        # verify balance


                        # app_log.info("Mempool: Verifying balance")
                        app_log.info("Mempool: Received address: {}".format(mempool_address))

                        # include the new block
                        execute_param(m, ("SELECT sum(amount) FROM transactions WHERE recipient = ?;"), (mempool_address,))
                        credit_mempool = m.fetchone()[0]
                        if credit_mempool == None:
                            credit_mempool = 0

                        execute_param(m, ("SELECT sum(amount) FROM transactions WHERE address = ?;"), (mempool_address,))
                        debit_mempool = m.fetchone()[0]
                        if debit_mempool == None:
                            debit_mempool = 0
                        # include the new block

                        execute_param(c, ("SELECT sum(amount) FROM transactions WHERE recipient = ?;"), (mempool_address,))
                        credit_ledger = c.fetchone()[0]
                        if credit_ledger == None:
                            credit_ledger = 0
                        credit = float(credit_ledger) + float(credit_mempool)

                        execute_param(c, ("SELECT sum(amount) FROM transactions WHERE address = ?;"), (mempool_address,))
                        debit_ledger = c.fetchone()[0]
                        if debit_ledger == None:
                            debit_ledger = 0
                        debit = float(debit_ledger) + float(debit_mempool)

                        execute_param(c, ("SELECT sum(fee),sum(reward) FROM transactions WHERE address = ?;"), (mempool_address,))
                        result = c.fetchall()[0]
                        fees = result[0]
                        if fees == None:
                            fees = 0

                        rewards = result[1]
                        if rewards == None:
                            rewards = 0

                        # app_log.info("Mempool: Total credit: " + str(credit))
                        # app_log.info("Mempool: Total debit: " + str(debit))
                        balance = float(credit) - float(debit) - float(fees) + float(rewards) - float(mempool_amount)
                        balance_pre = float(credit_ledger) - float(debit_ledger) - float(fees) + float(rewards)
                        # app_log.info("Mempool: Projected transction address balance: " + str(balance))

                        fee = '%.8f' % float(0.01 + (float(mempool_amount) * 0.001) + (float(len(mempool_openfield)) / 100000) + (float(mempool_keep) / 10))  # 0.1% + 0.01 dust

                        time_now = time.time()
                        if float(mempool_timestamp) > float(time_now) + 30:
                            app_log.info("Mempool: Future transaction not allowed, timestamp {} minutes in the future".format((float(mempool_timestamp) - float(time_now)) / 60))

                        elif float(time_now) - 86400 > float(mempool_timestamp):
                            app_log.info("Mempool: Transaction older than 24h not allowed.")

                        elif float(mempool_amount) > float(balance_pre):
                            app_log.info("Mempool: Sending more than owned")

                        elif (float(balance)) - (float(fee)) < 0:  # removed +float(db_amount) because it is a part of the incoming block
                            app_log.info("Mempool: Cannot afford to pay fees")
                        # verify signatures and balances
                        else:
                            execute_param(m, "INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?)", (
                                str(mempool_timestamp), str(mempool_address), str(mempool_recipient), str(mempool_amount),
                                str(mempool_signature_enc), str(mempool_public_key_hashed), str(mempool_keep), str(mempool_openfield)))
                            app_log.info("Mempool updated with a received transaction")
                            commit(mempool)  # Save (commit) the changes

                            # merge mempool

                            # receive mempool

                            # app_log.info("Mempool: Finished with {} received transactions from {}".format(len(block_list),peer_ip))
            except:
                app_log.info("Mempool: Error processing")

                if debug_conf == 1:
                    raise
                else:
                    return
            finally:
                busy_mempool = 0


def purge_old_peers():
    global peer_dict
    peer_dict = {}
    with open("peers.txt") as f:
        for line in f:
            line = re.sub("[\)\(\:\\n\'\s]", "", line)
            peer_dict[line.split(",")[0]] = line.split(",")[1]

        for key, value in peer_dict.items():
            HOST = key
            # app_log.info(HOST)
            PORT = int(value)
            # app_log.info(PORT)

            try:
                s = socks.socksocket()
                s.settimeout(0.3)
                if tor_conf == 1:
                    s.setproxy(socks.PROXY_TYPE_SOCKS5, "127.0.0.1", 9050)
                # s.setblocking(0)
                s.connect((HOST, PORT))
                s.close()
            except:
                if purge_conf == 1:
                    # remove from peerlist if not connectible
                    del peer_dict[key]

                    print("Removed formerly active peer {} {}".format(HOST, PORT))
                pass

    output = open("peers.txt", 'w')
    for key, value in peer_dict.items():
        output.write("('" + key + "', '" + value + "')\n")
    output.close()


def verify(c):
    try:
        # verify blockchain
        execute(c, ("SELECT Count(*) FROM transactions"))
        db_rows = c.fetchone()[0]
        app_log.warning("Total steps: {}".format(db_rows))

        # verify genesis
        execute(c, ("SELECT recipient FROM transactions ORDER BY block_height ASC LIMIT 1"))
        genesis = c.fetchone()[0]
        app_log.warning("Genesis: {}".format(genesis))
        if str(genesis) != genesis_conf:  # change this line to your genesis address if you want to clone
            app_log.info("Invalid genesis address")
            sys.exit(1)
        # verify genesis

        invalid = 0
        for row in execute(c, ('SELECT * FROM transactions WHERE block_height > 0 and ORDER BY block_height')):

            db_block_height = row[0]
            db_timestamp = '%.2f' % float(row[1])
            db_address = row[2]
            db_recipient = row[3]
            db_amount = row[4]
            db_signature_enc = row[5]
            db_public_key_hashed = row[6]
            db_public_key = RSA.importKey(base64.b64decode(db_public_key_hashed))
            db_keep = str(row[10])
            db_openfield = row[11]

            db_transaction = (str(db_timestamp), str(db_address), str(db_recipient), '%.8f' % float(db_amount), str(db_keep), str(db_openfield))

            db_signature_dec = base64.b64decode(db_signature_enc)
            verifier = PKCS1_v1_5.new(db_public_key)
            h = SHA.new(str(db_transaction).encode("utf-8"))
            if verifier.verify(h, db_signature_dec):
                pass
            else:
                app_log.warning("The following transaction is invalid: {}".format(row))
                invalid = invalid + 1
                if db_block_height == str(1):
                    app_log.warning("Your genesis signature is invalid, someone meddled with the database")
                    sys.exit(1)

        if invalid == 0:
            app_log.warning("All transacitons in the local ledger are valid")

    except sqlite3.Error as e:
        app_log.info("Error %s:" % e.args[0])
        sys.exit(1)


def blocknf(block_hash_delete, peer_ip, conn, c):
    global busy, hdd_block
    if busy == 0:
        busy = 1
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
                # delete followups
                execute_param(c, ("DELETE FROM transactions WHERE block_height >= ?;"), (str(db_block_height),))
                commit(conn)

                app_log.warning("Node {} didn't find block {}({}), rolled back".format(peer_ip, db_block_height, db_block_hash))

                if ram_conf == 1:
                    #roll back hdd too
                    hdd = sqlite3.connect(ledger_path_conf)
                    hdd.text_factory = str
                    h = hdd.cursor()
                    execute_param(h, ("DELETE FROM transactions WHERE block_height >= ?;"), (str(db_block_height),))
                    commit(hdd)
                    hdd.close()
                    hdd_block = int(db_block_height)-1
                    # roll back hdd too


        except:
            pass
        finally:
            busy = 0

            # delete followups


def consensus_add(peer_ip, consensus_blockheight):
    try:
        global peer_ip_list
        global consensus_blockheight_list
        global consensus_percentage
        global consensus

        if peer_ip not in peer_ip_list:
            app_log.info("Adding {} to consensus peer list".format(peer_ip))
            peer_ip_list.append(peer_ip)
            app_log.info("Assigning {} to peer block height list".format(consensus_blockheight))
            consensus_blockheight_list.append(str(int(consensus_blockheight)))

        if peer_ip in peer_ip_list:
            consensus_index = peer_ip_list.index(peer_ip)  # get where in this list it is

            if consensus_blockheight_list[consensus_index] == (consensus_blockheight):
                app_log.info("Opinion of {} hasn't changed".format(peer_ip))

            else:
                del peer_ip_list[consensus_index]  # remove ip
                del consensus_blockheight_list[consensus_index]  # remove ip's opinion

                app_log.info("Updating {} in consensus".format(peer_ip))
                peer_ip_list.append(peer_ip)
                consensus_blockheight_list.append(int(consensus_blockheight))

        consensus = most_common(consensus_blockheight_list)

        consensus_percentage = (float(
            consensus_blockheight_list.count(consensus) / float(len(consensus_blockheight_list)))) * 100

        # app_log.info("Current outgoing connections: {}".format(len(active_pool)))
        # app_log.info("Current block consensus: {} = {}%".format(consensus,consensus_percentage))

        return
    except Exception as e:
        app_log.info(e)
        raise


def consensus_remove(peer_ip):
    global peer_ip_list
    global consensus_blockheight_list
    try:
        app_log.info("Consensus opinion list: {}".format(consensus_blockheight_list))
        app_log.info("Will remove {} from consensus pool {}".format(peer_ip, peer_ip_list))
        consensus_index = peer_ip_list.index(peer_ip)
        peer_ip_list.remove(peer_ip)
        del consensus_blockheight_list[consensus_index]  # remove ip's opinion
    except:
        app_log.info("IP of {} not present in the consensus pool".format(peer_ip))
        pass


def manager():
    global banlist
    global peer_dict
    while True:

        keys = peer_dict.keys()
        #random.shuffle(keys)

        for key, value in peer_dict.items():
            HOST = key
            # app_log.info(HOST)
            PORT = int(value)
            if threading.active_count() < thread_limit_conf and str(HOST + ":" + str(PORT)) not in tried and str(
                                    HOST + ":" + str(PORT)) not in active_pool and str(HOST) not in banlist:
                app_log.info("Will attempt to connect to {}:{}".format(HOST, PORT))
                tried.append(HOST + ":" + str(PORT))
                t = threading.Thread(target=worker, args=(HOST, PORT))  # threaded connectivity to nodes here
                app_log.info("---Starting a client thread " + str(threading.currentThread()) + "---")
                t.daemon = True
                t.start()

                # client thread handling
        if len(active_pool) < thread_limit_conf / 3:
            app_log.info("Only {} connections active, resetting the try list".format(len(active_pool)))
            del tried[:]

        app_log.info("Connection manager: Banlist: {}".format(banlist))
        app_log.info("Connection manager: Syncing nodes: {}".format(syncing))
        app_log.info("Connection manager: Syncing nodes: {}/3".format(len(syncing)))
        app_log.info("Connection manager: Database access: {}/1".format(busy))
        app_log.info("Connection manager: Threads at {} / {}".format(threading.active_count(), thread_limit_conf))
        app_log.info("Connection manager: Tried: {}".format(tried))
        app_log.info("Connection manager: Current active pool: {}".format(active_pool))
        app_log.warning("Connection manager: Current connections: {}".format(len(active_pool)))
        if consensus:  # once the consensus is filled
            app_log.warning("Connection manager: Consensus: {} = {}%".format(consensus, consensus_percentage))
            app_log.info("Connection manager: Consensus IP list: {}".format(peer_ip_list))
            app_log.info("Connection manager: Consensus opinion list: {}".format(consensus_blockheight_list))

        # app_log.info(threading.enumerate() all threads)
        time.sleep(int(pause_conf) * 10)


def digest_block(data, sdef, peer_ip, conn, c, mempool, m):
    global busy, banlist, hdd_block

    if busy == 0:
        block_valid = 1  # init

        app_log.info("Digesting started from {}".format(peer_ip))
        try:
            busy = 1

            block_list = ast.literal_eval(data)
            if not any(isinstance(el, list) for el in block_list):  # if it's not a list of lists
                new_list = []
                new_list.append(block_list)
                block_list = new_list  # make it a list of lists
            # print block_list

            # reject block with duplicate transactions
            signature_list = []
            block_transactions = []

            for transaction_list in block_list:

                for r in transaction_list:  # sig 4
                    signature_list.append(r[4])

                    # reject block with transactions which are already in the ledger
                    execute_param(c, ("SELECT block_height FROM transactions WHERE signature = ?;"), (r[4],))
                    try:
                        result = c.fetchall()[0]
                        error_msg = "That transaction is already in our ledger, row {}".format(result[0])
                        block_valid = 0

                    except:
                        pass
                        # reject block with transactions which are already in the ledger

                if len(signature_list) != len(set(signature_list)):
                    error_msg = "There are duplicate transactions in this block, rejected"
                    block_valid = 0  # dont really need this one
                del signature_list[:]

                # reject block with duplicate transactions

                # previous block info
                execute(c, ("SELECT block_hash, block_height,timestamp FROM transactions WHERE reward != 0 ORDER BY block_height DESC LIMIT 1;"))
                result = c.fetchall()
                db_block_hash = result[0][0]
                db_block_height = result[0][1]
                db_timestamp_last = float(result[0][2])
                block_height_new = db_block_height + 1
                # previous block info

                transaction_list_converted = []  # makes sure all the data are properly converted as in the previous lines
                for transaction in transaction_list:
                    # print transaction
                    # verify signatures
                    received_timestamp = '%.2f' % float(transaction[0])
                    received_address = str(transaction[1][:56])
                    received_recipient = str(transaction[2][:56])
                    received_amount = '%.8f' % float(transaction[3])
                    received_signature_enc = str(transaction[4])
                    received_public_key_hashed = str(transaction[5])
                    received_keep = str(transaction[6])
                    received_openfield = str(transaction[7])

                    transaction_list_converted.append((received_timestamp, received_address, received_recipient, received_amount, received_signature_enc, received_public_key_hashed, received_keep, received_openfield))

                    received_public_key = RSA.importKey(
                        base64.b64decode(received_public_key_hashed))  # convert readable key to instance
                    received_signature_dec = base64.b64decode(received_signature_enc)
                    verifier = PKCS1_v1_5.new(received_public_key)

                    h = SHA.new(str((received_timestamp, received_address, received_recipient, received_amount, received_keep, received_openfield)).encode("utf-8"))
                    if not verifier.verify(h, received_signature_dec):
                        error_msg = "Invalid signature"
                        block_valid = 0
                    else:
                        app_log.info("Valid signature")

                    if received_keep != "1" or received_keep != "0":
                        block_valid = 0
                        error_msg = "Wrong keep value"

                    if float(received_amount) < 0:
                        block_valid = 0
                        error_msg = "Negative balance spend attempt"

                    if transaction != transaction_list[-1]:  # non-mining txs
                        if received_address != hashlib.sha224(base64.b64decode(received_public_key_hashed)).hexdigest():
                            error_msg = "Attempt to spend from a wrong address"
                            block_valid = 0

                    if transaction == transaction_list[-1]:  # recognize the last transaction as the mining reward transaction
                        block_timestamp = received_timestamp
                        nonce = received_openfield
                        miner_address = received_address

                    time_now = time.time()
                    if float(time_now) + 30 < float(received_timestamp):
                        error_msg = "Future transaction not allowed, timestamp {} minutes in the future".format((float(received_timestamp) - float(time_now)) / 60)
                        block_valid = 0
                    if float(db_timestamp_last) - 86400 > float(received_timestamp):
                        error_msg = "Transaction older than 24h not allowed."
                        block_valid = 0
                        # verify signatures

                # reject blocks older than latest block
                if float(block_timestamp) <= float(db_timestamp_last):
                    block_valid = 0
                    error_msg = "Block is older than the previous one, will be rejected"
                # reject blocks older than latest block

                # calculate difficulty
                execute_param(c, ("SELECT block_height FROM transactions WHERE CAST(timestamp AS INTEGER) > ? AND reward != 0"), (db_timestamp_last - 1800,))  # 1800=30 min
                blocks_per_30 = len(c.fetchall())

                diff = blocks_per_30 * 2

                # drop diff per minute if over target
                time_drop = time.time()

                drop_factor = 120  # drop 0,5 diff per minute #hardfork

                if time_drop > db_timestamp_last + 120:  # start dropping after 2 minutes
                    diff = diff - (time_drop - db_timestamp_last) / drop_factor  # drop 0,5 diff per minute (1 per 2 minutes)
                    # drop diff per minute if over target

                if time_drop > db_timestamp_last + 300 or diff < 37:  # 5 m lim
                    diff = 37  # 5 m lim
                # calculate difficulty

                app_log.info("Calculated difficulty: {}".format(diff))
                diff = int(diff)
                # calculate difficulty

                # match difficulty

                #app_log.info("Transaction list: {}".format(transaction_list_converted))
                block_hash = hashlib.sha224((str(transaction_list_converted) + db_block_hash).encode("utf-8")).hexdigest()
                #app_log.info("Last block hash: {}".format(db_block_hash))
                app_log.info("Calculated block hash: {}".format(block_hash))
                #app_log.info("Nonce: {}".format(nonce))

                mining_hash = bin_convert(hashlib.sha224((miner_address + nonce + db_block_hash).encode("utf-8")).hexdigest())

                mining_condition = bin_convert(db_block_hash)[0:diff]

                if mining_condition in mining_hash:  # simplified comparison, no backwards mining
                    app_log.info("Difficulty requirement satisfied for block {} from {}".format(block_height_new, peer_ip))
                else:
                    # app_log.info("Digest: Difficulty requirement not satisfied: " + bin_convert(miner_address) + " " + bin_convert(block_hash))
                    error_msg = "Difficulty too low for block {} from {}, should be at least {}".format(block_height_new, peer_ip, diff)
                    block_valid = 0

                    # print data
                    # print transaction_list
                # match difficulty

                fees_block = []

                if peer_ip in banlist:
                    block_valid = 0
                    error_msg = "Cannot accept blocks form a banned peer"

                if block_valid == 0:
                    app_log.warning("Check 1: A part of the block is invalid, rejected: {}".format(error_msg))
                    error_msg = ""
                    app_log.info("Check 1: Complete rejected data: {}".format(data))
                    warning(sdef, peer_ip)

                if block_valid == 1:
                    for transaction in transaction_list:
                        db_timestamp = '%.2f' % float(transaction[0])
                        db_address = str(transaction[1][:56])
                        db_recipient = str(transaction[2][:56])
                        db_amount = '%.8f' % float(transaction[3])
                        db_signature = str(transaction[4])
                        db_public_key_hashed = str(transaction[5])
                        db_keep = str(transaction[6])
                        db_openfield = str(transaction[7])

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

                        block_credit = 0
                        block_debit = 0

                        for x in transaction_list:  # quite nasty, care not to overlap variables
                            if x[2] == db_address:
                                block_credit = float(block_credit) + float(x[3])
                            if x[1] == db_address:
                                block_debit = float(block_debit) + float(x[3])

                        # app_log.info("Digest: Incoming block credit: " + str(block_credit))
                        # app_log.info("Digest: Incoming block debit: " + str(block_debit))
                        # include the new block

                        execute_param(c, ("SELECT sum(amount) FROM transactions WHERE recipient = ?;"), (db_address,))
                        credit_ledger = c.fetchone()[0]
                        if credit_ledger == None:
                            credit_ledger = 0
                        credit = float(credit_ledger) + float(block_credit)

                        execute_param(c, ("SELECT sum(amount) FROM transactions WHERE address = ?;"), (db_address,))
                        debit_ledger = c.fetchone()[0]
                        if debit_ledger == None:
                            debit_ledger = 0
                        debit = float(debit_ledger) + float(block_debit)

                        execute_param(c, ("SELECT sum(fee),sum(reward) FROM transactions WHERE address = ?;"),
                                      (db_address,))
                        result = c.fetchall()[0]
                        fees = result[0]
                        rewards = result[1]

                        if fees == None:
                            fees = 0
                        if rewards == None:
                            rewards = 0

                        # app_log.info("Digest: Total credit: " + str(credit))
                        # app_log.info("Digest: Total debit: " + str(debit))
                        balance_pre = float(credit_ledger) - float(debit_ledger) - float(fees) + float(rewards)  # without projection
                        balance = float(credit) - float(debit) - float(fees) + float(rewards)
                        # app_log.info("Digest: Projected transction address balance: " + str(balance))



                        fee = '%.8f' % float(0.01 + (float(db_amount) * 0.001) + (float(len(db_openfield)) / 100000) + (float(db_keep) / 10))  # 0.1% + 0.01 dust

                        fees_block.append(float(fee))
                        # app_log.info("Fee: " + str(fee))


                        # decide reward

                        if transaction == transaction_list[-1]:

                            db_amount = 0  # prevent spending from another address, because mining txs allow delegation
                            if db_block_height <= 10000000:
                                mining_reward = 15 - (float(block_height_new) / float(1000000))  # one zero less
                            else:
                                mining_reward = 0

                            reward = '%.8f' % float(mining_reward + sum(fees_block[:-1]))
                            fee = 0
                        else:
                            reward = 0

                            # dont request a fee for mined block so new accounts can mine

                        if float(balance_pre) < float(db_amount):
                            error_msg = "Sending more than owned"
                            block_valid = 0

                        elif (float(balance)) - (float(fee)) < 0:  # removed +float(db_amount) because it is a part of the incoming block
                            error_msg = "Cannot afford to pay fees"
                            block_valid = 0

                        else:
                            # append, but do not insert to ledger before whole block is validated
                            app_log.info("Digest: Appending transaction back to block with {} transactions in it".format(len(block_transactions)))
                            block_transactions.append((block_height_new, db_timestamp, db_address, db_recipient,
                                                       db_amount, db_signature, db_public_key_hashed,
                                                       block_hash, fee, reward, db_keep, db_openfield))

                        try:
                            execute_param(m, ("DELETE FROM transactions WHERE signature = ?;"),
                                          (db_signature,))  # delete tx from mempool now that it is in the ledger
                            commit(mempool)
                            app_log.info("Digest: Removed processed transaction from the mempool")
                        except:
                            # tx was not in the local mempool
                            pass

                    # whole block validation
                    if block_valid == 0:
                        app_log.info("Check 2: A part of the block is invalid, rejected: {}".format(error_msg))
                        error_msg = ""
                        app_log.info("Check 2: Complete rejected block: {}".format(data))
                        warning(sdef, peer_ip)

                    if block_valid == 1:
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
                                try:
                                    execute_param(c, ("SELECT timestamp FROM transactions WHERE openfield = ?;"), (str(block_height_new),))
                                    test_dev_reward = c.fetchone()[0]
                                except:
                                    if transaction == block_transactions[-1]:  # put at the end
                                        execute_param(c, "INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
                                            "0", str(time_now), "Development Reward", str(genesis_conf),
                                            str(reward), "0", "0", "0", "0", "0", "0", str(block_height_new)))
                                        commit(conn)
                                        # dev reward

                        app_log.warning("Block {} valid and saved from {}".format(block_height_new, peer_ip))

                        del block_transactions[:]
                        unban(peer_ip)

                        # whole block validation
        except Exception as e:
            app_log.info(e)

            if debug_conf == 1:
                raise  # major debug client
            else:
                pass

        finally:
            app_log.info("Digesting complete")
            global ram_conf
            if ram_conf == 1 and block_valid == 1:
                db_to_drive()
            busy = 0


# key maintenance
if os.path.isfile("privkey.der") is True:
    app_log.warning("privkey.der found")
elif os.path.isfile("privkey_encrypted.der") is True:
    app_log.warning("privkey_encrypted.der found")
else:
    # generate key pair and an address
    random_generator = Random.new().read
    key = RSA.generate(1024, random_generator)
    public_key = key.publickey()

    private_key_readable = key.exportKey().decode("utf-8")
    public_key_readable = key.exportKey().decode("utf-8")
    address = hashlib.sha224(public_key_readable.encode("utf-8")).hexdigest()  # hashed public key
    # generate key pair and an address

    app_log.info("Your address: {}".format(address))
    app_log.info("Your public key: {}".format(public_key_readable))

    pem_file = open("privkey.der", 'a')
    pem_file.write(str(private_key_readable))
    pem_file.close()

    pem_file = open("pubkey.der", 'a')
    pem_file.write(str(public_key_readable))
    pem_file.close()

    address_file = open("address.txt", 'a')
    address_file.write(str(address) + "\n")
    address_file.close()

# import keys
# key = RSA.importKey(open('privkey.der').read())
# private_key_readable = str(key.exportKey())
public_key_readable = open('pubkey.der'.encode('utf-8')).read()
public_key_hashed = base64.b64encode(public_key_readable.encode('utf-8'))
address = hashlib.sha224(public_key_readable.encode('utf-8')).hexdigest()

app_log.warning("Local address: {}".format(address))

if not os.path.exists('mempool.db'):
    # create empty mempool
    mempool = sqlite3.connect('mempool.db')
    mempool.text_factory = str
    m = mempool.cursor()
    execute(m, ("CREATE TABLE IF NOT EXISTS transactions (timestamp, address, recipient, amount, signature, public_key, keep, openfield)"))
    commit(mempool)
    app_log.info("Created mempool file")
    # create empty mempool
else:
    app_log.warning("Mempool exists")

if hyperblocks_conf == 1:
    ledger_convert()

if ram_conf == 1:
    try:
        app_log.warning("Moving database to RAM")
        conn = sqlite3.connect('file::memory:?cache=shared', uri=True)
        conn.text_factory = str
        c = conn.cursor()

        old_db = sqlite3.connect(ledger_path_conf)
        query = "".join(line for line in old_db.iterdump())

        conn.executescript(query)

        c.execute("SELECT block_height FROM transactions ORDER BY block_height DESC LIMIT 1")
        hdd_block = c.fetchone()[0]

        app_log.warning("Moved database to RAM")
    except Exception as e:
        app_log.info(e)

mempool, m = db_m_define()
conn, c = db_c_define()


# init
def db_maintenance():
    # db maintenance
    execute(conn, "VACUUM")
    execute(mempool, "VACUUM")
    app_log.warning("Database maintenance finished")


if rebuild_db_conf == 1:
    db_maintenance()
# connectivity to self node

if verify_conf == 1:
    verify(c)

# init

### LOCAL CHECKS FINISHED ###
app_log.warning("Starting up...")


class ThreadedTCPRequestHandler(socketserver.BaseRequestHandler):
    def handle(self):  # server defined here

        mempool, m = db_m_define()
        conn, c = db_c_define()

        global busy
        global banlist
        global warning_list_limit_conf

        peer_ip = self.request.getpeername()[0]

        if threading.active_count() < thread_limit_conf or peer_ip == "127.0.0.1":
            capacity = 1
        else:
            capacity = 0
            self.request.close()
            app_log.info("Free capacity for {} unavailable, disconnected".format(peer_ip))
            # if you raise here, you kill the whole server

        if peer_ip not in banlist:
            banned = 0
        else:
            banned = 1
            self.request.close()
            app_log.warning("IP {} banned, disconnected".format(peer_ip))
            # if you raise here, you kill the whole server

        timeout_operation = 60  # timeout
        timer_operation = time.time()  # start counting

        while banned == 0 and capacity == 1:
            try:
                if not time.time() <= timer_operation + timeout_operation:  # return on timeout
                    warning_list.append(peer_ip)  # add warning

                    raise ValueError("Incoming: Operation timeout from {}".format(peer_ip))

                data = connections.receive(self.request, 10)

                app_log.info(
                    "Incoming: Received: {} from {}".format(data, peer_ip))  # will add custom ports later

                if data == 'version':
                    data = connections.receive(self.request, 10)
                    if version != data:
                        app_log.info("Protocol version mismatch: {}, should be {}".format(data, version))
                        connections.send(self.request, "notok", 10)
                        return
                    else:
                        app_log.info("Incoming: Protocol version matched: {}".format(data))
                        connections.send(self.request, "ok", 10)

                elif data == 'mempool':

                    # receive theirs
                    segments = connections.receive(self.request, 10)
                    mempool_merge(segments, peer_ip, conn, c, mempool, m)
                    # receive theirs

                    execute(m, ('SELECT * FROM transactions'))
                    mempool_txs = m.fetchall()

                    # send own
                    # app_log.info("Incoming: Extracted from the mempool: " + str(mempool_txs))  # improve: sync based on signatures only

                    # if len(mempool_txs) > 0: same as the other
                    connections.send(self.request, mempool_txs, 10)
                    # send own

                elif data == 'hello':
                    global peersync
                    if peersync == 0:
                        peersync = 1
                        with open("peers.txt", "r") as peer_list:
                            peers = peer_list.read()

                            connections.send(self.request, "peers", 10)
                            connections.send(self.request, peers, 10)

                        peer_list.close()

                        # save peer if connectible
                        peer_file = open("peers.txt", 'r')
                        peer_tuples = []
                        for line in peer_file:
                            extension = re.findall("'([\d\.]+)', '([\d]+)'", line)
                            peer_tuples.extend(extension)
                        peer_file.close()
                        peer_tuple = ("('" + peer_ip + "', '" + str(port) + "')")

                        try:
                            app_log.info("Testing connectivity to: {}".format(peer_ip))
                            peer_test = socks.socksocket()
                            if tor_conf == 1:
                                peer_test.setproxy(socks.PROXY_TYPE_SOCKS5, "127.0.0.1", 9050)
                            # peer_test.setblocking(0)
                            peer_test.connect((str(peer_ip), int(str(port))))  # double parentheses mean tuple
                            app_log.info("Incoming: Distant peer connectible")

                            # properly end the connection
                            peer_test.close()
                            # properly end the connection
                            if peer_tuple not in str(peer_tuples):  # stringing tuple is a nasty way
                                peer_list_file = open("peers.txt", 'a')
                                peer_list_file.write((peer_tuple) + "\n")
                                app_log.info("Incoming: Distant peer saved to peer list")
                                peer_list_file.close()
                            else:
                                app_log.info("Distant peer already in peer list")
                        except:
                            app_log.info("Incoming: Distant peer not connectible")
                            pass
                    peersync = 0

                            # raise #test only

                        # save peer if connectible

                    while busy == 1:
                        time.sleep(float(pause_conf))
                    app_log.info("Incoming: Sending sync request")
                    connections.send(self.request, "sync", 10)

                elif data == "sendsync":
                    while busy == 1:
                        time.sleep(float(pause_conf))

                    global syncing
                    while len(syncing) >= 3:
                        time.sleep(int(pause_conf))

                    connections.send(self.request, "sync", 10)


                elif data == "blocksfnd":
                    app_log.info("Incoming: Client has the block")  # node should start sending txs in this step

                    # app_log.info("Incoming: Combined segments: " + segments)
                    # print peer_ip
                    if busy == 1:
                        app_log.info("Skipping sync from {}, syncing already in progress".format(peer_ip))

                    elif max(consensus_blockheight_list) == int(consensus_blockheight):
                        connections.send(self.request, "blockscf", 10)

                        segments = connections.receive(self.request, 10)
                        digest_block(segments, self.request, peer_ip, conn, c, mempool, m)
                        # receive theirs
                    else:
                        connections.send(self.request, "blocksrj", 10)
                        app_log.info("Incoming: Distant peer {} is at {}, should be {}".format(peer_ip, consensus_blockheight_list, max(consensus_blockheight_list)))

                    connections.send(self.request, "sync", 10)

                elif data == "blockheight":
                    try:
                        received_block_height = connections.receive(self.request, 10)  # receive client's last block height
                        app_log.info("Incoming: Received block height {} from {} ".format(received_block_height, peer_ip))

                        # consensus pool 1 (connection from them)
                        consensus_blockheight = int(received_block_height)  # str int to remove leading zeros
                        consensus_add(peer_ip, consensus_blockheight)
                        # consensus pool 1 (connection from them)


                        execute(c, ('SELECT block_height FROM transactions ORDER BY block_height DESC LIMIT 1'))
                        db_block_height = c.fetchone()[0]

                        # append zeroes to get static length
                        connections.send(self.request, db_block_height, 10)
                        # send own block height

                        if int(received_block_height) > db_block_height:
                            app_log.info("Incoming: Client has higher block")

                            execute(c, ('SELECT block_hash FROM transactions ORDER BY block_height DESC LIMIT 1'))
                            db_block_hash = c.fetchone()[0]  # get latest block_hash

                            app_log.info("Incoming: block_hash to send: " + str(db_block_hash))
                            connections.send(self.request, db_block_hash, 10)

                            # receive their latest hash
                            # confirm you know that hash or continue receiving

                        if int(received_block_height) <= db_block_height:
                            app_log.info("Incoming: We have the same or higher block height, hash will be verified")

                            data = connections.receive(self.request, 10)  # receive client's last block_hash
                            # send all our followup hashes

                            app_log.info("Incoming: Will seek the following block: {}".format(data))

                            try:
                                execute_param(c, ("SELECT block_height FROM transactions WHERE block_hash = ?;"), (data,))
                                client_block = c.fetchone()[0]

                                app_log.info("Incoming: Client is at block {}".format(client_block))  # now check if we have any newer

                                execute(c, ('SELECT block_hash FROM transactions ORDER BY block_height DESC LIMIT 1'))
                                db_block_hash = c.fetchone()[0]  # get latest block_hash
                                if db_block_hash == data:
                                    app_log.info("Incoming: Client has the latest block")
                                    connections.send(self.request, "nonewblk", 10)

                                else:
                                    execute_param(c, ("SELECT block_height, timestamp,address,recipient,amount,signature,public_key,keep,openfield FROM transactions WHERE block_height > ? AND block_height < ?;"),
                                                  (str(int(client_block)),) + (str(int(client_block + 100)),))  # select incoming transaction + 1
                                    blocks_fetched = c.fetchall()
                                    blocks_send = [[l[1:] for l in group] for _, group in groupby(blocks_fetched, key=itemgetter(0))] #remove block number

                                    # app_log.info("Incoming: Selected " + str(blocks_send) + " to send")


                                    connections.send(self.request, "blocksfnd", 10)

                                    confirmation = connections.receive(self.request, 10)

                                    if confirmation == "blockscf":
                                        app_log.info("Incoming: Client confirmed they want to sync from us")
                                        connections.send(self.request, blocks_send, 10)

                                    elif confirmation == "blocksrj":
                                        app_log.info("Incoming: Client rejected to sync from us because we're dont have the latest block")
                                        pass

                                        # send own

                            except:
                                app_log.info("Incoming: Block not found")
                                connections.send(self.request, "blocknf", 10)
                                connections.send(self.request, data, 10)
                    except Exception as e:
                        app_log.info("Incoming: Sync failed {}".format(e))


                elif data == "nonewblk":
                    # digest_block() #temporary #otherwise passive node will not be able to digest
                    connections.send(self.request, "sync", 10)

                elif data == "blocknf":
                    block_hash_delete = connections.receive(self.request, 10)
                    # print peer_ip
                    if max(consensus_blockheight_list) == consensus_blockheight:
                        blocknf(block_hash_delete, peer_ip, conn, c)
                        warning_list.append(peer_ip)
                    app_log.info("Outgoing: Deletion complete, sending sync request")

                    while busy == 1:
                        time.sleep(float(pause_conf))
                    connections.send(self.request, "sync", 10)

                elif data == "block" and (peer_ip in allowed or "any" in allowed):  # from miner

                    app_log.warning("Outgoing: Received a block from miner {}".format(peer_ip))
                    # receive block
                    segments = connections.receive(self.request, 10)
                    # app_log.info("Incoming: Combined mined segments: " + segments)

                    # check if we have the latest block

                    execute(c, ('SELECT block_height FROM transactions ORDER BY block_height DESC LIMIT 1'))
                    db_block_height = c.fetchone()[0]

                    # check if we have the latest block

                    if len(active_pool) < 5:
                        app_log.warning("Outgoing: Mined block ignored, insufficient connections to the network")
                    elif int(db_block_height) >= int(max(consensus_blockheight_list)) - 3 and busy == 0:
                        app_log.warning("Outgoing: Processing block from miner")
                        digest_block(segments, self.request, peer_ip, conn, c, mempool, m)
                    elif busy == 1:
                        app_log.warning("Outgoing: Block from miner skipped because we are digesting already")

                    # receive theirs
                    else:
                        app_log.warning("Outgoing: Mined block was orphaned because node was not synced, we are at block {}, should be at least {}".format(db_block_height, int(max(consensus_blockheight_list)) - 3))

                elif data == "blocklast" and (peer_ip in allowed or "any" in allowed):  # only sends the miner part of the block!

                    execute(c, ("SELECT * FROM transactions WHERE reward != 0 ORDER BY block_height DESC LIMIT 1;"))
                    block_last = c.fetchall()[0]

                    connections.send(self.request, block_last, 10)

                elif data == "blockget" and (peer_ip in allowed or "any" in allowed):
                    block_desired = connections.receive(self.request, 10)

                    execute_param(c, ("SELECT * FROM transactions WHERE block_height = ?;"), (block_desired,))
                    block_desired_result = c.fetchall()

                    connections.send(self.request, block_desired_result, 10)

                elif data == "mpinsert" and (peer_ip in allowed or "any" in allowed):
                    mempool_insert = connections.receive(self.request, 10)
                    mempool_merge(mempool_insert, peer_ip, conn, c, mempool, m)
                    connections.send(self.request, "Mempool insert finished", 10)

                elif data == "balanceget" and (peer_ip in allowed or "any" in allowed):
                    balance_address = connections.receive(self.request, 10)  # for which address

                    # verify balance

                    # app_log.info("Mempool: Verifying balance")
                    app_log.info("Mempool: Received address: " + str(balance_address))

                    execute_param(m, ("SELECT sum(amount) FROM transactions WHERE recipient = ?;"), (balance_address,))
                    credit_mempool = m.fetchone()[0]
                    if credit_mempool == None:
                        credit_mempool = 0

                    execute_param(m, ("SELECT sum(amount) FROM transactions WHERE address = ?;"), (balance_address,))
                    debit_mempool = m.fetchone()[0]
                    if debit_mempool == None:
                        debit_mempool = 0

                    execute_param(c, ("SELECT sum(amount) FROM transactions WHERE recipient = ?;"), (balance_address,))
                    credit_ledger = c.fetchone()[0]
                    if credit_ledger == None:
                        credit_ledger = 0
                    credit = float(credit_ledger) + float(credit_mempool)

                    execute_param(c, ("SELECT sum(fee),sum(reward),sum(amount) FROM transactions WHERE address = ?;"), (balance_address,))
                    result = c.fetchall()[0]

                    fees = result[0]
                    if fees == None:
                        fees = 0

                    rewards = result[1]
                    if rewards == None:
                        rewards = 0

                    debit_ledger = result[2]
                    if debit_ledger == None:
                        debit_ledger = 0

                    debit = float(debit_ledger) + float(debit_mempool)

                    balance = float(credit) - float(debit) - float(fees) + float(rewards)
                    # balance_pre = float(credit_ledger) - float(debit_ledger) - float(fees) + float(rewards)
                    # app_log.info("Mempool: Projected transction address balance: " + str(balance))

                    connections.send(self.request, (balance, credit, debit, fees, rewards), 10)  # return balance of the address to the client, including mempool
                    # connections.send(self.request, balance_pre, 10)  # return balance of the address to the client, no mempool

                elif data == "mpget" and (peer_ip in allowed or "any" in allowed):
                    execute(m, ('SELECT * FROM transactions'))
                    mempool_txs = m.fetchall()

                    # app_log.info("Outgoing: Extracted from the mempool: " + str(mempool_txs))  # improve: sync based on signatures only

                    # if len(mempool_txs) > 0: #wont sync mempool until we send something, which is bad
                    # send own
                    connections.send(self.request, mempool_txs, 10)



                elif data == "diffget" and (peer_ip in allowed or "any" in allowed):

                    execute(c, ("SELECT timestamp,block_height FROM transactions WHERE reward != 0 ORDER BY block_height DESC LIMIT 1;"))
                    result = c.fetchall()
                    db_timestamp_last = float(result[0][0])

                    # calculate difficulty
                    execute_param(c, ("SELECT block_height FROM transactions WHERE CAST(timestamp AS INTEGER) > ? AND reward != 0"), (db_timestamp_last - 1800,))  # 1800=30 min
                    blocks_per_30 = len(c.fetchall())

                    diff = blocks_per_30 * 2

                    # drop diff per minute if over target
                    time_drop = time.time()

                    drop_factor = 120  # drop 0,5 diff per minute #hardfork

                    if time_drop > db_timestamp_last + 120:  # start dropping after 2 minutes
                        diff = diff - (time_drop - db_timestamp_last) / drop_factor  # drop 0,5 diff per minute (1 per 2 minutes)

                    if time_drop > db_timestamp_last + 300 or diff < 37:  # 5 m lim
                        diff = 37  # 5 m lim

                    connections.send(self.request, diff, 10)


                else:
                    raise ValueError("Unexpected error, received: " + str(data))

                if not time.time() <= timer_operation + timeout_operation:
                    timer_operation = time.time()  # reset timer
                time.sleep(0.1)  # prevent cpu overload
                # app_log.info("Server resting")

            except Exception as e:
                app_log.info("Incoming: Lost connection to {}".format(peer_ip))
                app_log.info("Incoming: {}".format(e))

                # remove from consensus (connection from them)
                consensus_remove(peer_ip)
                # remove from consensus (connection from them)
                if self.request:
                    self.request.close()
                if debug_conf == 1:
                    raise  # major debug client
                else:
                    return
        mempool.close()
        conn.close()


# client thread
def worker(HOST, PORT):
    global busy
    try:
        this_client = (HOST + ":" + str(PORT))
        s = socks.socksocket()
        if tor_conf == 1:
            s.setproxy(socks.PROXY_TYPE_SOCKS5, "127.0.0.1", 9050)
        # s.setblocking(0)
        s.connect((HOST, PORT))
        app_log.info("Outgoing: Connected to {}".format(this_client))

        if this_client not in active_pool:
            active_pool.append(this_client)
            app_log.info("Current active pool: {}".format(active_pool))

    except:
        app_log.info("Could not connect to {}".format(this_client))
        return

    first_run = 1

    timeout_operation = 60  # timeout
    timer_operation = time.time()  # start counting

    while True:
        peer_ip = s.getpeername()[0]

        try:
            if not time.time() <= timer_operation + timeout_operation:  # return on timeout
                warning_list.append(peer_ip)  # add warning

                raise ValueError("Outgoing: Operation timeout from {}".format(peer_ip))

            mempool, m = db_m_define()
            conn, c = db_c_define()

            # communication starter
            if first_run == 1:
                first_run = 0

                connections.send(s, "version", 10)
                connections.send(s, version, 10)

                data = connections.receive(s, 10)

                if (data == "ok"):
                    app_log.info("Outgoing: Node protocol version of {} matches our client".format(peer_ip))
                else:
                    app_log.info("Outgoing: Node protocol version of {} mismatch".format(peer_ip))
                    return

                connections.send(s, "hello", 10)

            # communication starter

            data = connections.receive(s, 10)  # receive data, one and the only root point

            if data == "peers":
                subdata = connections.receive(s, 10)

                # get remote peers into tuples (actually list)
                server_peer_tuples = re.findall("'([\d\.]+)', '([\d]+)'", subdata)
                app_log.info("Received following {} peers: {}".format(len((server_peer_tuples)), server_peer_tuples))
                # get remote peers into tuples (actually list)

                # get local peers into tuples
                peer_file = open("peers.txt", 'r')
                peer_tuples = []
                for line in peer_file:
                    extension = re.findall("'([\d\.]+)', '([\d]+)'", line)
                    peer_tuples.extend(extension)
                peer_file.close()
                # get local peers into tuples

                for x in server_peer_tuples:
                    if x not in peer_tuples:
                        app_log.info("Outgoing: {} is a new peer, saving if connectible".format(x))
                        try:
                            s_purge = socks.socksocket()
                            if tor_conf == 1:
                                s_purge.setproxy(socks.PROXY_TYPE_SOCKS5, "127.0.0.1", 9050)
                                # s_purge = s.setblocking(0)

                            s_purge.connect((x[0], int(x[1])))  # save a new peer file with only active nodes
                            s_purge.close()

                            peer_list_file = open("peers.txt", 'a')
                            peer_list_file.write("('"+x[0]+"', '"+x[1]+"')\n")
                            peer_list_file.close()
                        except:
                            pass
                            app_log.info("Not connectible")

                    else:
                        app_log.info("Outgoing: {} is not a new peer".format(x))

            elif data == "sync":
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

                    app_log.info("Outgoing: Sending block height to compare: {}".format(db_block_height))
                    # append zeroes to get static length
                    connections.send(s, db_block_height, 10)

                    received_block_height = connections.receive(s, 10)  # receive node's block height
                    app_log.info("Outgoing: Node is at block height: {}".format(received_block_height))

                    if int(received_block_height) < db_block_height:
                        app_log.info("Outgoing: We have a higher, sending")
                        data = connections.receive(s, 10)  # receive client's last block_hash

                        # send all our followup hashes
                        app_log.info("Outgoing: Will seek the following block: {}".format(data))

                        # consensus pool 2 (active connection)
                        consensus_blockheight = int(received_block_height)  # str int to remove leading zeros
                        consensus_add(peer_ip, consensus_blockheight)
                        # consensus pool 2 (active connection)



                        try:
                            execute_param(c, ("SELECT block_height FROM transactions WHERE block_hash = ?;"), (data,))
                            client_block = c.fetchone()[0]

                            app_log.info("Outgoing: Node is at block {}".format(client_block))  # now check if we have any newer

                            execute(c, ('SELECT block_hash FROM transactions ORDER BY block_height DESC LIMIT 1'))
                            db_block_hash = c.fetchone()[0]  # get latest block_hash
                            if db_block_hash == data:
                                app_log.info("Outgoing: Node has the latest block")
                                connections.send(s, "nonewblk", 10)

                            else:
                                execute_param(c, ("SELECT block_height, timestamp,address,recipient,amount,signature,public_key,keep,openfield FROM transactions WHERE block_height > ? AND block_height < ?;"),
                                              (str(int(client_block)),) + (str(int(client_block + 100)),))  # select incoming transaction + 1, only columns that need not be verified
                                blocks_fetched = c.fetchall()
                                blocks_send = [[l[1:] for l in group] for _, group in groupby(blocks_fetched, key=itemgetter(0))] #remove block number

                                # app_log.info("Outgoing: Selected " + str(blocks_send) + " to send")

                                connections.send(s, "blocksfnd", 10)

                                confirmation = connections.receive(s, 10)

                                if confirmation == "blockscf":
                                    app_log.info("Outgoing: Client confirmed they want to sync from us")
                                    connections.send(s, blocks_send, 10)

                                elif confirmation == "blocksrj":
                                    app_log.info("Outgoing: Client rejected to sync from us because we're dont have the latest block")
                                    pass

                        except:
                            app_log.info("Outgoing: Block not found")
                            connections.send(s, "blocknf", 10)
                            connections.send(s, data, 10)

                    if int(received_block_height) >= db_block_height:
                        app_log.info("Outgoing: We have the same or lower block height, hash will be verified")

                        execute(c, ('SELECT block_hash FROM transactions ORDER BY block_height DESC LIMIT 1'))
                        db_block_hash = c.fetchone()[0]  # get latest block_hash

                        app_log.info("Outgoing: block_hash to send: {}".format(db_block_hash))
                        connections.send(s, db_block_hash, 10)

                        # consensus pool 2 (active connection)
                        consensus_blockheight = int(received_block_height)  # str int to remove leading zeros
                        consensus_add(peer_ip, consensus_blockheight)
                        # consensus pool 2 (active connection)

                        # receive their latest hash
                        # confirm you know that hash or continue receiving

                except Exception as e:
                    app_log.info("Outgoing: Sync failed {}".format(e))
                finally:
                    syncing.remove(peer_ip)

            elif data == "blocknf":
                block_hash_delete = connections.receive(s, 10)
                # print peer_ip
                if max(consensus_blockheight_list) == consensus_blockheight:
                    blocknf(block_hash_delete, peer_ip, conn, c)

                while busy == 1:
                    time.sleep(float(pause_conf))
                connections.send(s, "sendsync", 10)

            elif data == "blocksfnd":
                app_log.info("Outgoing: Node has the block")  # node should start sending txs in this step

                # app_log.info("Incoming: Combined segments: " + segments)
                # print peer_ip
                if busy == 1:
                    app_log.info("Skipping sync from {}, syncing already in progress".format(peer_ip))

                elif max(consensus_blockheight_list) == int(consensus_blockheight):
                    connections.send(s, "blockscf", 10)

                    segments = connections.receive(s, 10)
                    digest_block(segments, s, peer_ip, conn, c, mempool, m)
                    # receive theirs
                else:
                    connections.send(s, "blocksrj", 10)
                    app_log.info("Incoming: Distant peer {} is at {}, should be {}".format(peer_ip, consensus_blockheight, max(consensus_blockheight_list)))

                connections.send(s, "sendsync", 10)

                # block_hash validation end

            elif data == "nonewblk":
                # digest_block() #temporary #otherwise passive node will not be able to digest

                # sand and receive mempool
                execute(m, ('SELECT * FROM transactions'))
                mempool_txs = m.fetchall()

                # app_log.info("Outgoing: Extracted from the mempool: " + str(mempool_txs))  # improve: sync based on signatures only

                # if len(mempool_txs) > 0: #wont sync mempool until we send something, which is bad
                # send own
                connections.send(s, "mempool", 10)
                connections.send(s, mempool_txs, 10)
                # send own

                # receive theirs
                segments = connections.receive(s, 10)
                mempool_merge(segments, peer_ip, conn, c, mempool, m)
                # receive theirs

                # receive mempool

                app_log.info("Outgoing: We seem to be at the latest block. Paused before recheck")

                time.sleep(int(pause_conf))
                while busy == 1:
                    time.sleep(float(pause_conf))
                connections.send(s, "sendsync", 10)

            else:
                raise ValueError("Unexpected error, received: {}".format(data))

            if not time.time() <= timer_operation + timeout_operation:
                timer_operation = time.time()  # reset timer



        except Exception as e:
            # remove from active pool
            if this_client in active_pool:
                app_log.info("Will remove {} from active pool {}".format(this_client, active_pool))
                active_pool.remove(this_client)
            # remove from active pool

            # remove from consensus 2
            consensus_remove(peer_ip)
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
                return

        finally:
            mempool.close()
            conn.close()


class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    pass


if __name__ == "__main__":
    try:
        if tor_conf == 0:
            # Port 0 means to select an arbitrary unused port
            HOST, PORT = "0.0.0.0", int(port)

            server = ThreadedTCPServer((HOST, PORT), ThreadedTCPRequestHandler)
            ip, port = server.server_address

            purge_old_peers()

            # Start a thread with the server -- that thread will then start one
            # more thread for each request

            server_thread = threading.Thread(target=server.serve_forever)

            # Exit the server thread when the main thread terminates

            server_thread.daemon = True
            server_thread.start()
            app_log.warning("Server loop running in thread: {}".format(server_thread.name))
        else:
            app_log.warning("Not starting a local server to conceal identity on Tor network")

        # start connection manager
        t_manager = threading.Thread(target=manager())
        app_log.warning("Starting connection manager")
        t_manager.daemon = True
        t_manager.start()
        # start connection manager

        # server.serve_forever() #added
        server.shutdown()
        server.server_close()

    except Exception as e:
        app_log.info("Node already running?")
        app_log.info(e)
sys.exit()
