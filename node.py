# c = hyperblock in ram OR hyperblock file when running only hyperblocks without ram mode on
# h = ledger file or hyperblock clone in hyperblock mode
# h2 = hyperblock file

# never remove the str() conversion in data evaluation or database inserts or you will debug for 14 days as signed types mismatch
# if you raise in the server thread, the server will die and node will stop
# never use codecs, they are bugged and do not provide proper serialization
# must unify node and client now that connections parameters are function parameters
# if you have a block of data and want to insert it into sqlite, you must use a single "commit" for the whole batch, it's 100x faster
# do not isolation_level=None/WAL hdd levels, it makes saving slow
# issues with db? perhaps you missed a commit() or two


VERSION = "4.5.0.1"

import functools
import glob
import platform
import shutil
import socketserver
import sqlite3
import tarfile
import threading
from sys import version_info

import aliases  # PREFORK_ALIASES
# import aliasesv2 as aliases # POSTFORK_ALIASES

# Bis specific modules
import apihandler
import connectionmanager
import dbhandler
import log
import options
import peershandler
import plugins
import wallet_keys
from connections import send, receive
# Consensus / digestion helpers — explicit imports (previously `from digest import *`, which
# silently relied on digest.py's own imports leaking through. That left `regnet` undefined at
# startup, because digest.py imports regnet only locally. See doc/14-known-issues-and-improvements.)
import hashlib
import os
import sys
import time
from decimal import Decimal

import essentials
import mempool as mp
import mining_heavy3
import regnet
import tokensv2 as tokens
from digest import digest_block
from difficulty import difficulty
from essentials import checkpoint_set, fee_calculate, download_file
from quantizer import quantize_eight, quantize_two
from polysign.signerfactory import SignerFactory
from libs import node, logger, keys, client
from fork import Fork
from db_hashes import db_hashes

# todo: migrate this to polysign\signer_crw.py
from Cryptodome.Hash import SHA
from Cryptodome.PublicKey import RSA
from Cryptodome.Signature import PKCS1_v1_5
import base64
# /todo

fork = Fork()

appname = "Bismuth"
appauthor = "Bismuth Foundation"

# nodes_ban_reset=config.nodes_ban_reset


def sql_trace_callback(log, id, statement):
    line = f"SQL[{id}] {statement}"
    log.warning(line)


def bootstrap():
    # TODO: Candidate for single user mode
    try:
        types = ['static/*.db-wal', 'static/*.db-shm']
        for t in types:
            for f in glob.glob(t):
                os.remove(f)
                print(f, "deleted")

        archive_path = node.ledger_path + ".tar.gz"
        download_file("https://bismuth.cz/ledger.tar.gz", archive_path)

        with tarfile.open(archive_path) as tar:
            
            import os
            
            def is_within_directory(directory, target):
                
                abs_directory = os.path.abspath(directory)
                abs_target = os.path.abspath(target)
            
                prefix = os.path.commonprefix([abs_directory, abs_target])
                
                return prefix == abs_directory
            
            def safe_extract(tar, path=".", members=None, *, numeric_owner=False):
            
                for member in tar.getmembers():
                    member_path = os.path.join(path, member.name)
                    if not is_within_directory(path, member_path):
                        raise Exception("Attempted Path Traversal in Tar File")
            
                tar.extractall(path, members, numeric_owner=numeric_owner) 
                
            
            safe_extract(tar, "static/")

    except:
        node.logger.app_log.warning("Something went wrong during bootstrapping, aborted")
        raise


def check_integrity(database):
    # TODO: Candidate for single user mode
    # check ledger integrity

    if not os.path.exists("static"):
        os.mkdir("static")

    with sqlite3.connect(database) as ledger_check:
        if node.trace_db_calls:
            ledger_check.set_trace_callback(functools.partial(sql_trace_callback,node.logger.app_log,"CHECK_INTEGRITY"))

        ledger_check.text_factory = str
        l = ledger_check.cursor()

        try:
            l.execute("PRAGMA table_info('transactions')")
            redownload = False
        except:
            redownload = True

        if len(l.fetchall()) != 12:
            node.logger.app_log.warning(
                f"Status: Integrity check on database {database} failed, bootstrapping from the website")
            redownload = True

    if redownload and node.is_mainnet:
        bootstrap()


def rollback(node, db_handler, block_height):
    node.logger.app_log.warning(f"Status: Rolling back below: {block_height}")

    db_handler.rollback_under(block_height)

    # rollback indices
    db_handler.tokens_rollback(node, block_height)
    db_handler.aliases_rollback(node, block_height)
    # rollback indices

    node.logger.app_log.warning(f"Status: Chain rolled back below {block_height} and will be resynchronized")


def recompress_ledger(node, rebuild=False, depth=15000):
    # TODO: Candidate for single user mode
    node.logger.app_log.warning(f"Status: Recompressing, please be patient")

    files_remove = [node.ledger_path + '.temp',node.ledger_path + '.temp-shm',node.ledger_path + '.temp-wal']
    for file in files_remove:
        if os.path.exists(file):
            os.remove(file)
            node.logger.app_log.warning(f"Removed old {file}")

    if rebuild:
        node.logger.app_log.warning(f"Status: Hyperblocks will be rebuilt")

        shutil.copy(node.ledger_path, node.ledger_path + '.temp')
        hyper = sqlite3.connect(node.ledger_path + '.temp')
    else:
        shutil.copy(node.hyper_path, node.ledger_path + '.temp')
        hyper = sqlite3.connect(node.ledger_path + '.temp')
    if node.trace_db_calls:
       hyper.set_trace_callback(functools.partial(sql_trace_callback,node.logger.app_log,"HYPER"))
    hyper.text_factory = str
    hyp = hyper.cursor()

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
            except Exception:
                credit = 0

        debit = Decimal("0")
        for entry in hyp.execute(
                "SELECT amount,fee FROM transactions WHERE address = ? AND (block_height < ? AND block_height > ?);",
                (x[0],) + (depth_specific, -depth_specific,)):
            try:
                debit = quantize_eight(debit) + quantize_eight(entry[0]) + quantize_eight(entry[1])
                debit = 0 if debit is None else debit
            except Exception:
                debit = 0

        end_balance = quantize_eight(credit - debit)

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

    if os.path.exists(node.hyper_path):
        os.remove(node.hyper_path)  # remove the old hyperblocks to rebuild
        os.rename(node.ledger_path + '.temp', node.hyper_path)


def ledger_check_heights(node, db_handler):
    # TODO: Candidate for single user mode
    """conversion of normal blocks into hyperblocks from ledger.db or hyper.db to hyper.db"""
    if os.path.exists(node.hyper_path):

        # cross-integrity check
        hdd_block_max = db_handler.block_height_max()
        hdd_block_max_diff = db_handler.block_height_max_diff()
        hdd2_block_last = db_handler.block_height_max_hyper()
        hdd2_block_last_misc = db_handler.block_height_max_diff_hyper()

        # cross-integrity check

        if hdd_block_max == hdd2_block_last == hdd2_block_last_misc == hdd_block_max_diff and node.hyper_recompress:  # cross-integrity check
            node.logger.app_log.warning("Status: Recompressing hyperblocks (keeping full ledger)")
            node.recompress = True

            #print (hdd_block_max,hdd2_block_last,node.hyper_recompress)
        elif hdd_block_max == hdd2_block_last and not node.hyper_recompress:
            node.logger.app_log.warning("Status: Hyperblock recompression skipped")
            node.recompress = False
        else:
            lowest_block = min(hdd_block_max, hdd2_block_last, hdd_block_max_diff, hdd2_block_last_misc)
            highest_block = max(hdd_block_max, hdd2_block_last, hdd_block_max_diff, hdd2_block_last_misc)

            node.logger.app_log.warning(
                f"Status: Cross-integrity check failed, {highest_block} will be rolled back below {lowest_block}")

            rollback(node,db_handler_initial,lowest_block) #rollback to the lowest value
            node.recompress = False

    else:
        node.logger.app_log.warning("Status: Compressing ledger to Hyperblocks")
        node.recompress = True




def bin_convert(string):
    # TODO: Move to essentials.py
    return ''.join(format(ord(x), '8b').replace(' ', '0') for x in string)


def balanceget(balance_address, db_handler):
    # TODO: To move in db_handler, call by db_handler.balance_get(address)
    # verify balance

    # node.logger.app_log.info("Mempool: Verifying balance")
    # node.logger.app_log.info("Mempool: Received address: " + str(balance_address))

    base_mempool = mp.MEMPOOL.mp_get(balance_address)

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

    try:
        db_handler.execute_param(db_handler.h, "SELECT amount FROM transactions WHERE recipient = ?;", (balance_address,))
        entries = db_handler.h.fetchall()
    except:
        entries = []

    try:
        for entry in entries:
            credit_ledger = quantize_eight(credit_ledger) + quantize_eight(entry[0])
            credit_ledger = 0 if credit_ledger is None else credit_ledger
    except:
        credit_ledger = 0

    fees = Decimal("0")
    debit_ledger = Decimal("0")

    try:
        db_handler.execute_param(db_handler.h, "SELECT fee, amount FROM transactions WHERE address = ?;", (balance_address,))
        entries = db_handler.h.fetchall()
    except:
        entries = []

    try:
        for entry in entries:
            fees = quantize_eight(fees) + quantize_eight(entry[0])
            fees = 0 if fees is None else fees
    except:
        fees = 0

    try:
        for entry in entries:
            debit_ledger = debit_ledger + Decimal(entry[1])
            debit_ledger = 0 if debit_ledger is None else debit_ledger
    except:
        debit_ledger = 0

    debit = quantize_eight(debit_ledger + debit_mempool)

    rewards = Decimal("0")

    try:
        db_handler.execute_param(db_handler.h, "SELECT reward FROM transactions WHERE recipient = ?;", (balance_address,))
        entries = db_handler.h.fetchall()
    except:
        entries = []

    try:
        for entry in entries:
            rewards = quantize_eight(rewards) + quantize_eight(entry[0])
            rewards = 0 if str(rewards) == "0E-8" else rewards
            rewards = 0 if rewards is None else rewards
    except:
        rewards = 0

    balance = quantize_eight(credit_ledger - debit - fees + rewards)
    balance_no_mempool = float(credit_ledger) - float(debit_ledger) - float(fees) + float(rewards)
    # node.logger.app_log.info("Mempool: Projected transction address balance: " + str(balance))
    return str(balance), str(credit_ledger), str(debit), str(fees), str(rewards), str(balance_no_mempool)


def blocknf(node, block_hash_delete, peer_ip, db_handler, hyperblocks=False):
    """
    Rolls back a single block, updates node object variables.
    Rollback target must be above checkpoint.
    Hash to rollback must match in case our ledger moved.
    Not trusting hyperblock nodes for old blocks because of trimming,
    they wouldn't find the hash and cause rollback.
    """
    node.logger.app_log.info(f"Rollback operation on {block_hash_delete} initiated by {peer_ip}")

    my_time = time.time()

    if not node.db_lock.locked():
        node.db_lock.acquire()
        node.logger.app_log.warning(f"Database lock acquired")
        backup_data = None  # used in "finally" section
        skip = False
        reason = ""

        try:
            block_max_ram = db_handler.block_max_ram()
            db_block_height = block_max_ram ['block_height']
            db_block_hash = block_max_ram ['block_hash']

            ip = {'ip': peer_ip}
            node.plugin_manager.execute_filter_hook('filter_rollback_ip', ip)
            if ip['ip'] == 'no':
                reason = "Filter blocked this rollback"
                skip = True

            elif db_block_height < node.checkpoint:
                reason = "Block is past checkpoint, will not be rolled back"
                skip = True

            elif db_block_hash != block_hash_delete:
                # print db_block_hash
                # print block_hash_delete
                reason = "We moved away from the block to rollback, skipping"
                skip = True

            elif hyperblocks and node.last_block_ago > 30000: #more than 5000 minutes/target blocks away
                reason = f"{peer_ip} is running on hyperblocks and our last block is too old, skipping"
                skip = True

            else:
                backup_data = db_handler.backup_higher(db_block_height)

                node.logger.app_log.warning(f"Node {peer_ip} didn't find block {db_block_height}({db_block_hash})")

                # roll back hdd too
                db_handler.rollback_under(db_block_height)
                # /roll back hdd too

                # rollback indices
                db_handler.tokens_rollback(node, db_block_height)
                db_handler.aliases_rollback(node, db_block_height)
                # /rollback indices

                node.last_block_timestamp = db_handler.last_block_timestamp()
                node.last_block_hash = db_handler.last_block_hash()
                node.last_block = db_block_height - 1
                node.hdd_hash = db_handler.last_block_hash()
                node.hdd_block = db_block_height - 1
                tokens.tokens_update(node, db_handler)

        except Exception as e:
            node.logger.app_log.warning(e)

        finally:
            node.db_lock.release()

            node.logger.app_log.warning(f"Database lock released")

            if skip:
                rollback = {"timestamp": my_time, "height": db_block_height, "ip": peer_ip,
                            "hash": db_block_hash, "skipped": True, "reason": reason}
                node.plugin_manager.execute_action_hook('rollback', rollback)
                node.logger.app_log.info(f"Skipping rollback: {reason}")
            else:
                try:
                    nb_tx = 0
                    for tx in backup_data:
                        tx_short = f"{tx[1]} - {tx[2]} to {tx[3]}: {tx[4]} ({tx[11]})"
                        if tx[9] == 0:
                            try:
                                nb_tx += 1
                                node.logger.app_log.info(
                                    mp.MEMPOOL.merge((tx[1], tx[2], tx[3], tx[4], tx[5], tx[6], tx[10], tx[11]),
                                                     peer_ip, db_handler.c, False, revert=True))  # will get stuck if you change it to respect node.db_lock
                                node.logger.app_log.warning(f"Moved tx back to mempool: {tx_short}")
                            except Exception as e:
                                node.logger.app_log.warning(f"Error during moving tx back to mempool: {e}")
                        else:
                            # It's the coinbase tx, so we get the miner address
                            miner = tx[3]
                            height = tx[0]
                    rollback = {"timestamp": my_time, "height": height, "ip": peer_ip, "miner": miner,
                                "hash": db_block_hash, "tx_count": nb_tx, "skipped": False, "reason": ""}
                    node.plugin_manager.execute_action_hook('rollback', rollback)

                except Exception as e:
                    node.logger.app_log.warning(f"Error during moving txs back to mempool: {e}")

    else:
        reason = "Skipping rollback, other ledger operation in progress"
        rollback = {"timestamp": my_time, "ip": peer_ip, "skipped": True, "reason": reason}
        node.plugin_manager.execute_action_hook('rollback', rollback)
        node.logger.app_log.info(reason)


def sequencing_check(db_handler):
    # TODO: Candidate for single user mode
    try:
        with open("sequencing_last", 'r') as filename:
            sequencing_last = int(filename.read())

    except:
        node.logger.app_log.warning("Sequencing anchor not found, going through the whole chain")
        sequencing_last = 0

    node.logger.app_log.warning(f"Status: Testing chain sequencing, starting with block {sequencing_last}")

    chains_to_check = [node.ledger_path, node.hyper_path]

    for chain in chains_to_check:
        conn = sqlite3.connect(chain)
        if node.trace_db_calls:
            conn.set_trace_callback(functools.partial(sql_trace_callback,node.logger.app_log,"SEQUENCE-CHECK-CHAIN"))
        c = conn.cursor()

        # perform test on transaction table
        y = None
        # Egg: not sure block_height != (0 OR 1)  gives the proper result, 0 or 1  = 1. not in (0, 1) could be better.
        for row in c.execute(
                "SELECT block_height FROM transactions WHERE reward != 0 AND block_height > 1 AND block_height >= ? ORDER BY block_height ASC",
                (sequencing_last,)):
            y_init = row[0]

            if y is None:
                y = y_init

            if row[0] != y:

                for chain2 in chains_to_check:
                    conn2 = sqlite3.connect(chain2)
                    if node.trace_db_calls:
                        conn2.set_trace_callback(functools.partial(sql_trace_callback,node.logger.app_log,"SEQUENCE-CHECK-CHAIN2"))
                    c2 = conn2.cursor()
                    node.logger.app_log.warning(f"Status: Chain {chain} transaction sequencing error at: {row[0]}. {row[0]} instead of {y}")
                    c2.execute("DELETE FROM transactions WHERE block_height >= ? OR block_height <= ?", (row[0], -row[0],))
                    conn2.commit()
                    c2.execute("DELETE FROM misc WHERE block_height >= ?", (row[0],))
                    conn2.commit()

                    # rollback indices
                    db_handler.tokens_rollback(node, y)
                    db_handler.aliases_rollback(node, y)

                    # rollback indices

                    node.logger.app_log.warning(f"Status: Due to a sequencing issue at block {y}, {chain} has been rolled back and will be resynchronized")
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
                    if node.trace_db_calls:
                        conn2.set_trace_callback(functools.partial(sql_trace_callback,node.logger.app_log,"SEQUENCE-CHECK-CHAIN2B"))
                    c2 = conn2.cursor()
                    node.logger.app_log.warning(
                        f"Status: Chain {chain} difficulty sequencing error at: {row[0]}. {row[0]} instead of {y}")
                    c2.execute("DELETE FROM transactions WHERE block_height >= ?", (row[0],))
                    conn2.commit()
                    c2.execute("DELETE FROM misc WHERE block_height >= ?", (row[0],))
                    conn2.commit()

                    db_handler.execute_param(conn2, (
                        'DELETE FROM transactions WHERE address = "Development Reward" AND block_height <= ?'),
                                             (-row[0],))
                    conn2.commit()

                    db_handler.execute_param(conn2, (
                        'DELETE FROM transactions WHERE address = "Hypernode Payouts" AND block_height <= ?'),
                                             (-row[0],))
                    conn2.commit()
                    conn2.close()

                    # rollback indices
                    db_handler.tokens_rollback(node, y)
                    db_handler.aliases_rollback(node, y)
                    # rollback indices

                    node.logger.app_log.warning(f"Status: Due to a sequencing issue at block {y}, {chain} has been rolled back and will be resynchronized")
                break

            y = y + 1

        node.logger.app_log.warning(f"Status: Chain sequencing test complete for {chain}")
        conn.close()

        if y:
            with open("sequencing_last", 'w') as filename:
                filename.write(str(y - 1000))  # room for rollbacks


# init

class ThreadedTCPRequestHandler(socketserver.BaseRequestHandler):
    def handle(self):
        # this is a dedicated thread for each client (not ip)
        if node.IS_STOPPING:
            node.logger.app_log.warning("Inbound: Rejected incoming cnx, node is stopping")
            return

        db_handler_instance = dbhandler.DbHandler(node.index_db, node.ledger_path, node.hyper_path, node.ram, node.ledger_ram_file, node.logger, trace_db_calls=node.trace_db_calls)

        client_instance = client.Client()

        try:
            peer_ip = self.request.getpeername()[0]
        except:
            node.logger.app_log.warning("Inbound: Transport endpoint was not connected")
            return

        threading.current_thread().name = f"in_{peer_ip}"
        # if threading.active_count() < node.thread_limit or peer_ip == "127.0.0.1":
        # Always keep a slot for whitelisted (wallet could be there)
        if threading.active_count() < node.thread_limit / 3 * 2 or node.peers.is_whitelisted(peer_ip):  # inbound
            client_instance.connected = True
        else:
            try:
                node.logger.app_log.info(f"Free capacity for {peer_ip} unavailable, disconnected")
                self.request.close()
                # if you raise here, you kill the whole server
            except Exception as e:
                node.logger.app_log.warning(f"{e}")
                pass
            finally:
                return

        dict_ip = {'ip': peer_ip}
        node.plugin_manager.execute_filter_hook('peer_ip', dict_ip)

        if node.peers.is_banned(peer_ip) or dict_ip['ip'] == 'banned':
            self.request.close()
            node.logger.app_log.info(f"IP {peer_ip} banned, disconnected")

        # TODO: I'd like to call
        """
        node.peers.peersync({peer_ip: node.port})
        so we can save the peers that connected to us. 
        But not ok in current architecture: would delay the command, and we're not even sure it would be saved.
        TODO: Workaround: make sure our external ip and port is present in the peers we announce, or new nodes are likely never to be announced. 
        Warning: needs public ip/port, not local ones!
        """

        timeout_operation = 120  # timeout
        timer_operation = time.time()  # start counting

        while not node.peers.is_banned(peer_ip) and node.peers.version_allowed(peer_ip, node.version_allow) and client_instance.connected:
            try:
                extra = False  # Flag for plugin and regtest_* commands
                # Failsafe
                if self.request == -1:
                    raise ValueError(f"Inbound: Closed socket from {peer_ip}")

                if not time.time() <= timer_operation + timeout_operation:  # return on timeout
                    if node.peers.warning(self.request, peer_ip, "Operation timeout", 2):
                        node.logger.app_log.info(f"{peer_ip} banned")
                        break

                    raise ValueError(f"Inbound: Operation timeout from {peer_ip}")

                data = receive(self.request)

                node.logger.app_log.info(
                    f"Inbound: Received: {data} from {peer_ip}")  # will add custom ports later

                if data.startswith('regtest_'):
                    if not node.is_regnet:
                        send(self.request, "notok")
                        return
                    else:
                        db_handler_instance.execute(db_handler_instance.c, "SELECT block_hash FROM transactions WHERE block_height= (select max(block_height) from transactions)")
                        block_hash = db_handler_instance.c.fetchone()[0]
                        # feed regnet with current thread db handle. refactor needed.
                        regnet.conn, regnet.c, regnet.hdd, regnet.h, regnet.hdd2, regnet.h2, regnet.h = db_handler_instance.conn, db_handler_instance.c, db_handler_instance.hdd, db_handler_instance.h, db_handler_instance.hdd2, db_handler_instance.h2, db_handler_instance.h
                        regnet.command(self.request, data, block_hash, node, db_handler_instance)
                    # Set extra flag or the regtest_* command will thrown an exception
                    extra = True

                if data == 'version':
                    data = receive(self.request)
                    if data not in node.version_allow:
                        node.logger.app_log.warning(
                            f"Protocol version mismatch: {data}, should be {node.version_allow}")
                        send(self.request, "notok")
                        return
                    else:
                        node.logger.app_log.warning(f"Inbound: Protocol version matched with {peer_ip}: {data}")
                        send(self.request, "ok")
                        node.peers.store_mainnet(peer_ip, data)

                elif data == 'getversion':
                    send(self.request, node.version)

                elif data == 'mempool':

                    # receive theirs
                    segments = receive(self.request)
                    node.logger.app_log.info(mp.MEMPOOL.merge(segments, peer_ip, db_handler_instance.c, False))
                    #improvement possible - pass peer_ip from worker

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
                    # node.logger.app_log.info("Inbound: Extracted from the mempool: " + str(mempool_txs))  # improve: sync based on signatures only

                    # if len(mempool_txs) > 0: same as the other
                    send(self.request, mempool_txs)

                elif data == "hello":
                    if node.is_regnet:
                        node.logger.app_log.info("Inbound: Got hello but I'm in regtest mode, closing.")
                        return

                    send(self.request, "peers")
                    peers_send = node.peers.peer_list_disk_format()
                    send(self.request, peers_send)

                    while node.db_lock.locked():
                        time.sleep(quantize_two(node.pause))
                    node.logger.app_log.info("Inbound: Sending sync request")

                    send(self.request, "sync")

                elif data == "sendsync":
                    while node.db_lock.locked():
                        time.sleep(quantize_two(node.pause))

                    while len(node.syncing) >= 3:
                        time.sleep(int(node.pause))

                    send(self.request, "sync")

                elif data == "blocksfnd":
                    node.logger.app_log.info(f"Inbound: Client {peer_ip} has the block(s)")  # node should start sending txs in this step

                    # node.logger.app_log.info("Inbound: Combined segments: " + segments)
                    # print peer_ip
                    if node.db_lock.locked():
                        node.logger.app_log.info(f"Skipping sync from {peer_ip}, syncing already in progress")

                    else:
                        node.last_block_timestamp = db_handler_instance.last_block_timestamp()

                        if node.last_block_timestamp < time.time() - 600:
                            # block_req = most_common(consensus_blockheight_list)
                            block_req = node.peers.consensus_most_common
                            node.logger.app_log.warning("Most common block rule triggered")

                        else:
                            # block_req = max(consensus_blockheight_list)
                            block_req = node.peers.consensus_max
                            node.logger.app_log.warning("Longest chain rule triggered")

                        if int(received_block_height) >= block_req and int(received_block_height) > node.last_block:

                            try:  # they claim to have the longest chain, things must go smooth or ban
                                node.logger.app_log.warning(f"Confirming to sync from {peer_ip}")
                                node.plugin_manager.execute_action_hook('sync', {'what': 'syncing_from', 'ip': peer_ip})
                                send(self.request, "blockscf")

                                segments = receive(self.request)

                            except:
                                if node.peers.warning(self.request, peer_ip, "Failed to deliver the longest chain"):
                                    node.logger.app_log.info(f"{peer_ip} banned")
                                    break
                            else:
                                digest_block(node, segments, self.request, peer_ip, db_handler_instance)
                        else:
                            node.logger.app_log.warning(f"Rejecting to sync from {peer_ip}")
                            send(self.request, "blocksrj")
                            node.logger.app_log.info(
                                f"Inbound: Distant peer {peer_ip} is at {received_block_height}, should be at least {max(block_req,node.last_block+1)}")
                    send(self.request, "sync")

                elif data == "blockheight":
                    try:
                        received_block_height = receive(self.request)  # receive client's last block height
                        node.logger.app_log.info(
                            f"Inbound: Received block height {received_block_height} from {peer_ip} ")

                        # consensus pool 1 (connection from them)
                        consensus_blockheight = int(received_block_height)  # str int to remove leading zeros
                        # consensus_add(peer_ip, consensus_blockheight, self.request)
                        node.peers.consensus_add(peer_ip, consensus_blockheight, self.request, node.hdd_block)
                        # consensus pool 1 (connection from them)

                        # append zeroes to get static length
                        send(self.request, node.hdd_block)
                        # send own block height

                        if int(received_block_height) > node.hdd_block:
                            node.logger.app_log.warning("Inbound: Client has higher block")

                            node.logger.app_log.info(f"Inbound: block_hash to send: {node.hdd_hash}")
                            send(self.request, node.hdd_hash)

                            # receive their latest sha_hash
                            # confirm you know that sha_hash or continue receiving

                        elif int(received_block_height) <= node.hdd_block:
                            if int(received_block_height) == node.hdd_block:
                                node.logger.app_log.info(
                                    f"Inbound: We have the same height as {peer_ip} ({received_block_height}), hash will be verified")
                            else:
                                node.logger.app_log.warning(
                                    f"Inbound: We have higher ({node.hdd_block}) block height than {peer_ip} ({received_block_height}), hash will be verified")

                            data = receive(self.request)  # receive client's last block_hash
                            # send all our followup hashes
                            if data == "*":
                                # connection lost, no need to go on, that was banning the node like it forked.
                                node.logger.app_log.warning(f"Inbound: {peer_ip} dropped connection")
                                break
                            node.logger.app_log.info(f"Inbound: Will seek the following block: {data}")

                            client_block = db_handler_instance.block_height_from_hash(data)
                            if client_block is None:
                                node.logger.app_log.warning(f"Inbound: Block {data[:8]} of {peer_ip} not found")
                                if node.full_ledger:
                                    send(self.request, "blocknf")  # announce block hash was not found
                                else:
                                    send(self.request, "blocknfhb")  # announce we are on hyperblocks
                                send(self.request, data)

                                if node.peers.warning(self.request, peer_ip, "Forked", 2):
                                    node.logger.app_log.info(f"{peer_ip} banned")
                                    break

                            else:
                                node.logger.app_log.info(f"Inbound: Client is at block {client_block}")  # now check if we have any newer

                                if node.hdd_hash == data or not node.egress:
                                    if not node.egress:
                                        node.logger.app_log.warning(f"Inbound: Egress disabled for {peer_ip}")
                                    else:
                                        node.logger.app_log.info(f"Inbound: Client {peer_ip} has the latest block")

                                    time.sleep(int(node.pause))  # reduce CPU usage
                                    send(self.request, "nonewblk")

                                else:

                                    blocks_fetched = db_handler_instance.blocksync(client_block)

                                    node.logger.app_log.info(f"Inbound: Selected {blocks_fetched}")

                                    send(self.request, "blocksfnd")

                                    confirmation = receive(self.request)

                                    if confirmation == "blockscf":
                                        node.logger.app_log.info("Inbound: Client confirmed they want to sync from us")
                                        send(self.request, blocks_fetched)

                                    elif confirmation == "blocksrj":
                                        node.logger.app_log.info(
                                            "Inbound: Client rejected to sync from us because we're don't have the latest block")

                    except Exception as e:
                        node.logger.app_log.warning(f"Inbound: Sync failed {e}")

                elif data == "nonewblk":
                    send(self.request, "sync")

                elif data == "blocknf":
                    block_hash_delete = receive(self.request)
                    # print peer_ip
                    if consensus_blockheight == node.peers.consensus_max:
                        blocknf(node, block_hash_delete, peer_ip, db_handler_instance)
                        if node.peers.warning(self.request, peer_ip, "Rollback", 2):
                            node.logger.app_log.info(f"{peer_ip} banned")
                            break
                    node.logger.app_log.info("Inbound: Deletion complete, sending sync request")

                    while node.db_lock.locked():
                        time.sleep(node.pause)
                    send(self.request, "sync")

                elif data == "blocknfhb": #node announces it's running hyperblocks
                    block_hash_delete = receive(self.request)
                    # print peer_ip
                    if consensus_blockheight == node.peers.consensus_max:
                        blocknf(node, block_hash_delete, peer_ip, db_handler_instance, hyperblocks=True)
                        if node.peers.warning(self.request, peer_ip, "Rollback", 2):
                            node.logger.app_log.info(f"{peer_ip} banned")
                            break
                    node.logger.app_log.info("Inbound: Deletion complete, sending sync request")

                    while node.db_lock.locked():
                        time.sleep(node.pause)
                    send(self.request, "sync")

                elif data == "block":
                    # if (peer_ip in allowed or "any" in allowed):  # from miner
                    if node.peers.is_allowed(peer_ip, data):  # from miner
                        # TODO: rights management could be done one level higher instead of repeating the same check everywhere
                        node.logger.app_log.info(f"Inbound: Received a block from miner {peer_ip}")
                        # receive block
                        segments = receive(self.request)
                        # node.logger.app_log.info("Inbound: Combined mined segments: " + segments)
                        mined = {"timestamp": time.time(), "last": node.last_block, "ip": peer_ip, "miner": "",
                                 "result": False, "reason": ''}
                        try:
                            mined['miner'] = segments[0][-1][1]  # sender, to be consistent with block event.
                        except:
                            # Block is sent by miners/pools, we can drop the connection
                            # If there is a reason not to, use "continue" here and below instead of returns.
                            return  # missing info, bye
                        if node.is_mainnet:
                            if len(node.peers.connection_pool) < 5 and not node.peers.is_whitelisted(peer_ip):
                                reason = "Inbound: Mined block ignored, insufficient connections to the network"
                                mined['reason'] = reason
                                node.plugin_manager.execute_action_hook('mined', mined)
                                node.logger.app_log.info(reason)
                                return
                            elif node.db_lock.locked():
                                reason = "Inbound: Block from miner skipped because we are digesting already"
                                mined['reason'] = reason
                                node.plugin_manager.execute_action_hook('mined', mined)
                                node.logger.app_log.warning(reason)
                                return
                            elif node.last_block >= node.peers.consensus_max - 3:
                                mined['result'] = True
                                node.plugin_manager.execute_action_hook('mined', mined)
                                node.logger.app_log.info("Inbound: Processing block from miner")
                                try:
                                    digest_block(node, segments, self.request, peer_ip, db_handler_instance)
                                except ValueError as e:
                                    node.logger.app_log.warning("Inbound: block {}".format(str(e)))
                                    return
                                except Exception as e:
                                    node.logger.app_log.error("Inbound: Processing block from miner {}".format(e))
                                    return
                                # This new block may change the int(diff). Trigger the hook whether it changed or not.
                                #node.difficulty = difficulty(node, db_handler_instance)
                            else:
                                reason = f"Inbound: Mined block was orphaned because node was not synced, " \
                                         f"we are at block {node.last_block}, " \
                                         f"should be at least {node.peers.consensus_max - 3}"
                                mined['reason'] = reason
                                node.plugin_manager.execute_action_hook('mined', mined)
                                node.logger.app_log.warning(reason)
                        else:
                            # Not mainnet
                            try:
                                digest_block(node, segments, self.request, peer_ip, db_handler_instance)
                            except ValueError as e:
                                node.logger.app_log.warning("Inbound: block {}".format(str(e)))
                                return
                            except Exception as e:
                                node.logger.app_log.error("Inbound: Processing block from miner {}".format(e))
                                return
                    else:
                        receive(self.request)  # receive block, but do nothing about it
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for block command")

                elif data == "blocklast":
                    # if (peer_ip in allowed or "any" in allowed):  # only sends the miner part of the block!
                    if node.peers.is_allowed(peer_ip, data):
                        db_handler_instance.execute(db_handler_instance.c, "SELECT * FROM transactions "
                                                                           "WHERE reward != 0 "
                                                                           "ORDER BY block_height DESC LIMIT 1;")
                        block_last = db_handler_instance.c.fetchall()[0]

                        send(self.request, block_last)
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for blocklast command")

                elif data == "blocklastjson":
                    # if (peer_ip in allowed or "any" in allowed):  # only sends the miner part of the block!
                    if node.peers.is_allowed(peer_ip, data):
                        db_handler_instance.execute(db_handler_instance.c,
                                                    "SELECT * FROM transactions WHERE reward != 0 ORDER BY block_height DESC LIMIT 1;")
                        block_last = db_handler_instance.c.fetchall()[0]

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

                        send(self.request, response)
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for blocklastjson command")

                elif data == "blockget":
                    # if (peer_ip in allowed or "any" in allowed):
                    if node.peers.is_allowed(peer_ip, data):
                        block_desired = receive(self.request)

                        db_handler_instance.execute_param(db_handler_instance.h, "SELECT * FROM transactions WHERE block_height = ?;",
                                                          (block_desired,))
                        block_desired_result = db_handler_instance.h.fetchall()

                        send(self.request, block_desired_result)
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for blockget command")

                elif data == "blockgetjson":
                    # if (peer_ip in allowed or "any" in allowed):
                    if node.peers.is_allowed(peer_ip, data):
                        block_desired = receive(self.request)

                        db_handler_instance.execute_param(db_handler_instance.h, "SELECT * FROM transactions WHERE block_height = ?;",
                                                          (block_desired,))
                        block_desired_result = db_handler_instance.h.fetchall()

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

                        send(self.request, response_list)
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for blockget command")

                elif data == "mpinsert":
                    # if (peer_ip in allowed or "any" in allowed):
                    if node.peers.is_allowed(peer_ip, data):
                        mempool_insert = receive(self.request)
                        node.logger.app_log.warning("mpinsert command")
                        mpinsert_result = mp.MEMPOOL.merge(mempool_insert, peer_ip, db_handler_instance.c, True, True)
                        node.logger.app_log.warning(f"mpinsert result: {mpinsert_result}")
                        send(self.request, mpinsert_result)
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for mpinsert command")

                elif data == "balanceget":
                    # if (peer_ip in allowed or "any" in allowed):
                    if node.peers.is_allowed(peer_ip, data):
                        balance_address = receive(self.request)  # for which address

                        balanceget_result = balanceget(balance_address, db_handler_instance)

                        send(self.request,
                                         balanceget_result)  # return balance of the address to the client, including mempool
                        # send(self.request, balance_pre)  # return balance of the address to the client, no mempool
                    else:
                        node.logger.app_log.info("{peer_ip} not whitelisted for balanceget command")

                elif data == "balancegetjson":
                    # if (peer_ip in allowed or "any" in allowed):
                    if node.peers.is_allowed(peer_ip, data):
                        balance_address = receive(self.request)  # for which address

                        balanceget_result = balanceget(balance_address, db_handler_instance)
                        response = {"balance": balanceget_result[0],
                                    "credit": balanceget_result[1],
                                    "debit": balanceget_result[2],
                                    "fees": balanceget_result[3],
                                    "rewards": balanceget_result[4],
                                    "balance_no_mempool": balanceget_result[5]}

                        send(self.request,
                                         response)  # return balance of the address to the client, including mempool
                        # send(self.request, balance_pre)  # return balance of the address to the client, no mempool
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for balancegetjson command")

                elif data == "balancegethyper":
                    # if (peer_ip in allowed or "any" in allowed):
                    if node.peers.is_allowed(peer_ip, data):
                        balance_address = receive(self.request)  # for which address

                        balanceget_result = balanceget(balance_address, db_handler_instance)[0]

                        send(self.request,
                                         balanceget_result)  # return balance of the address to the client, including mempool
                        # send(self.request, balance_pre)  # return balance of the address to the client, no mempool
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for balancegetjson command")

                elif data == "balancegethyperjson":
                    if node.peers.is_allowed(peer_ip, data):
                        balance_address = receive(self.request)  # for which address

                        balanceget_result = balanceget(balance_address, db_handler_instance)
                        response = {"balance": balanceget_result[0]}

                        send(self.request,
                                         response)  # return balance of the address to the client, including mempool
                        # send(self.request, balance_pre)  # return balance of the address to the client, no mempool
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for balancegethyperjson command")

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

                    # node.logger.app_log.info("Inbound: Extracted from the mempool: " + str(mempool_txs))  # improve: sync based on signatures only

                    # if len(mempool_txs) > 0: #wont sync mempool until we send something, which is bad
                    # send own
                    send(self.request, response_list)

                elif data == "mpget" and node.peers.is_allowed(peer_ip, data):
                    mempool_txs = mp.MEMPOOL.fetchall(mp.SQL_SELECT_TX_TO_SEND)

                    # node.logger.app_log.info("Inbound: Extracted from the mempool: " + str(mempool_txs))  # improve: sync based on signatures only

                    # if len(mempool_txs) > 0: #wont sync mempool until we send something, which is bad
                    # send own
                    send(self.request, mempool_txs)

                elif data == "mpclear" and peer_ip == "127.0.0.1":  # reserved for localhost
                    mp.MEMPOOL.clear()

                elif data == "keygen":
                    # if (peer_ip in allowed or "any" in allowed):
                    if node.peers.is_allowed(peer_ip, data):
                        (gen_private_key_readable, gen_public_key_readable, gen_address) = wallet_keys.generate()
                        send(self.request, (gen_private_key_readable, gen_public_key_readable, gen_address))
                        (gen_private_key_readable, gen_public_key_readable, gen_address) = (None, None, None)
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for keygen command")

                elif data == "keygenjson":
                    # if (peer_ip in allowed or "any" in allowed):
                    if node.peers.is_allowed(peer_ip, data):
                        (gen_private_key_readable, gen_public_key_readable, gen_address) = wallet_keys.generate()
                        response = {"private_key": gen_private_key_readable,
                                    "public_key": gen_public_key_readable,
                                    "address": gen_address}

                        send(self.request, response)
                        (gen_private_key_readable, gen_public_key_readable, gen_address) = (None, None, None)
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for keygen command")

                elif data == "addlist":
                    # if (peer_ip in allowed or "any" in allowed):
                    if node.peers.is_allowed(peer_ip, data):
                        address_tx_list = receive(self.request)
                        db_handler_instance.execute_param(db_handler_instance.h, (
                            "SELECT * FROM transactions WHERE (address = ? OR recipient = ?) ORDER BY block_height DESC"),
                                                          (address_tx_list, address_tx_list,))
                        result = db_handler_instance.h.fetchall()
                        send(self.request, result)
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for addlist command")

                elif data == "listlimjson":
                    # if (peer_ip in allowed or "any" in allowed):
                    if node.peers.is_allowed(peer_ip, data):
                        list_limit = receive(self.request)
                        # print(address_tx_list_limit)
                        db_handler_instance.execute_param(db_handler_instance.h, "SELECT * FROM transactions ORDER BY block_height DESC LIMIT ?",
                                                          (list_limit,))
                        result = db_handler_instance.h.fetchall()

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

                        send(self.request, response_list)
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for listlimjson command")

                elif data == "listlim":
                    if node.peers.is_allowed(peer_ip, data):
                        list_limit = receive(self.request)
                        # print(address_tx_list_limit)
                        db_handler_instance.execute_param(db_handler_instance.h, "SELECT * FROM transactions ORDER BY block_height DESC LIMIT ?",
                                                          (list_limit,))
                        result = db_handler_instance.h.fetchall()
                        send(self.request, result)
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for listlim command")

                elif data == "addlistlim":
                    if node.peers.is_allowed(peer_ip, data):
                        address_tx_list = receive(self.request)
                        address_tx_list_limit = receive(self.request)

                        # print(address_tx_list_limit)
                        db_handler_instance.execute_param(db_handler_instance.h, (
                            "SELECT * FROM transactions WHERE (address = ? OR recipient = ?) ORDER BY block_height DESC LIMIT ?"),
                                                          (address_tx_list, address_tx_list, address_tx_list_limit,))
                        result = db_handler_instance.h.fetchall()
                        send(self.request, result)
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for addlistlim command")

                elif data == "addlistlimjson":
                    if node.peers.is_allowed(peer_ip, data):
                        address_tx_list = receive(self.request)
                        address_tx_list_limit = receive(self.request)

                        # print(address_tx_list_limit)
                        db_handler_instance.execute_param(db_handler_instance.h, (
                            "SELECT * FROM transactions WHERE (address = ? OR recipient = ?) ORDER BY block_height DESC LIMIT ?"),
                                                          (address_tx_list, address_tx_list, address_tx_list_limit,))
                        result = db_handler_instance.h.fetchall()

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

                        send(self.request, response_list)
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for addlistlimjson command")

                elif data == "addlistlimmir":
                    if node.peers.is_allowed(peer_ip, data):
                        address_tx_list = receive(self.request)
                        address_tx_list_limit = receive(self.request)

                        # print(address_tx_list_limit)
                        db_handler_instance.execute_param(db_handler_instance.h, (
                            "SELECT * FROM transactions WHERE (address = ? OR recipient = ?) AND block_height < 1 ORDER BY block_height ASC LIMIT ?"),
                                                          (address_tx_list, address_tx_list, address_tx_list_limit,))
                        result = db_handler_instance.h.fetchall()
                        send(self.request, result)
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for addlistlimmir command")

                elif data == "addlistlimmirjson":
                    if node.peers.is_allowed(peer_ip, data):
                        address_tx_list = receive(self.request)
                        address_tx_list_limit = receive(self.request)

                        # print(address_tx_list_limit)
                        db_handler_instance.execute_param(db_handler_instance.h, (
                            "SELECT * FROM transactions WHERE (address = ? OR recipient = ?) AND block_height < 1 ORDER BY block_height ASC LIMIT ?"),
                                                          (address_tx_list, address_tx_list, address_tx_list_limit,))
                        result = db_handler_instance.h.fetchall()

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

                        send(self.request, response_list)
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for addlistlimmir command")

                elif data == "aliasget":  # all for a single address, no protection against overlapping
                    if node.peers.is_allowed(peer_ip, data):
                        aliases.aliases_update(node, db_handler_instance)

                        alias_address = receive(self.request)
                        result = db_handler_instance.aliasget(alias_address)

                        send(self.request, result)
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for aliasget command")

                elif data == "aliasesget":  # only gets the first one, for multiple addresses
                    if node.peers.is_allowed(peer_ip, data):
                        aliases.aliases_update(node, db_handler_instance)
                        aliases_request = receive(self.request)
                        results = db_handler_instance.aliasesget(aliases_request)
                        send(self.request, results)
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for aliasesget command")

                # Not mandatory, but may help to reindex with minimal sql queries

                elif data == "tokensget":
                    # TODO: to be handled by token modules, with no sql here in node.
                    if node.peers.is_allowed(peer_ip, data):

                        tokens_address = receive(self.request)
                        tokens_user = db_handler_instance.tokens_user(tokens_address)

                        tokens_list = []
                        for token in tokens_user:
                            token = token[0]
                            db_handler_instance.execute_param(db_handler_instance.index_cursor,
                                                              "SELECT sum(amount) FROM tokens WHERE recipient = ? AND token = ?;",
                                                              (tokens_address,) + (token,))
                            credit = db_handler_instance.index_cursor.fetchone()[0]
                            db_handler_instance.execute_param(db_handler_instance.index_cursor,
                                                              "SELECT sum(amount) FROM tokens WHERE address = ? AND token = ?;",
                                                              (tokens_address,) + (token,))
                            debit = db_handler_instance.index_cursor.fetchone()[0]

                            debit = 0 if debit is None else debit
                            credit = 0 if credit is None else credit

                            balance = str(Decimal(credit) - Decimal(debit))

                            tokens_list.append((token, balance))

                        send(self.request, tokens_list)
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for tokensget command")

                elif data == "addfromalias":
                    if node.peers.is_allowed(peer_ip, data):

                        aliases.aliases_update(node, db_handler_instance)

                        alias_address = receive(self.request)
                        address_fetch = db_handler_instance.addfromalias(alias_address)
                        node.logger.app_log.warning(f"Fetched the following alias address: {address_fetch}")
                        send(self.request, address_fetch)

                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for addfromalias command")

                elif data == "pubkeyget":
                    if node.peers.is_allowed(peer_ip, data):
                        pub_key_address = receive(self.request)
                        target_public_key_b64encoded = db_handler_instance.pubkeyget(pub_key_address)
                        # returns as stored in the DB, that is b64 encoded, except for RSA where it's b64 encoded twice.
                        send(self.request, target_public_key_b64encoded)

                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for pubkeyget command")

                elif data == "aliascheck":
                    if node.peers.is_allowed(peer_ip, data):
                        reg_string = receive(self.request)

                        registered_pending = mp.MEMPOOL.fetchone(
                            "SELECT timestamp FROM transactions WHERE openfield = ?;",
                            ("alias=" + reg_string,))

                        db_handler_instance.execute_param(db_handler_instance.h, "SELECT timestamp FROM transactions WHERE openfield = ?;", ("alias=" + reg_string,) )
                        registered_already = db_handler_instance.h.fetchone()

                        if registered_already is None and registered_pending is None:
                            send(self.request, "Alias free")
                        else:
                            send(self.request, "Alias registered")
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for aliascheck command")

                elif data == "txsend":
                    """
                    This is most unsafe and should never be used.
                    - node gets the privkey
                    - dup code for assembling and signing the TX
                    TODO: DEPRECATED
                    """
                    if node.peers.is_allowed(peer_ip, data):
                        node.logger.app_log.warning("txsend is unsafe and deprecated, please don't use.")
                        tx_remote = receive(self.request)

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

                        remote_tx_pubkey_b64encoded = base64.b64encode(remote_tx_pubkey.encode('utf-8')).decode("utf-8")

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
                                         str(remote_tx_pubkey_b64encoded), str(remote_tx_operation),
                                         str(remote_tx_openfield)))

                        node.logger.app_log.info(mp.MEMPOOL.merge(mempool_data, peer_ip, db_handler_instance.c, True, True))

                        send(self.request, str(remote_signature_enc))
                        # wipe variables
                        (tx_remote, remote_tx_privkey, tx_remote_key) = (None, None, None)
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for txsend command")

                # less important methods
                elif data == "addvalidate":
                    if node.peers.is_allowed(peer_ip, data):

                        address_to_validate = receive(self.request)
                        if essentials.address_validate(address_to_validate):
                            result = "valid"
                        else:
                            result = "invalid"

                        send(self.request, result)
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for addvalidate command")

                elif data == "annget":
                    if node.peers.is_allowed(peer_ip):

                        # with open(peerlist, "r") as peer_list:
                        #    peers_file = peer_list.read()

                        result = db_handler_instance.annget(node)

                        send(self.request, result)
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for annget command")

                elif data == "annverget":
                    if node.peers.is_allowed(peer_ip):
                        result = db_handler_instance.annverget(node)
                        send(self.request, result)

                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for annget command")

                elif data == "peersget":
                    if node.peers.is_allowed(peer_ip, data):
                        send(self.request, node.peers.peer_list_disk_format())

                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for peersget command")

                elif data == "statusget":
                    if node.peers.is_allowed(peer_ip, data):
                        nodes_count = node.peers.consensus_size
                        nodes_list = node.peers.peer_opinion_dict
                        threads_count = threading.active_count()
                        uptime = int(time.time() - node.startup_time)
                        diff = node.difficulty
                        server_timestamp = '%.2f' % time.time()
                        if node.reveal_address:
                            revealed_address = node.keys.address
                        else:
                            revealed_address = "private"
                        send(self.request, (
                            revealed_address, nodes_count, nodes_list, threads_count, uptime, node.peers.consensus,
                            node.peers.consensus_percentage, VERSION, diff, server_timestamp))
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for statusget command")

                elif data == "statusjson":
                    # not only sends as an explicit dict, but also embeds extra info
                    if node.peers.is_allowed(peer_ip, data):
                        uptime = int(time.time() - node.startup_time)
                        tempdiff = node.difficulty
                        if node.reveal_address:
                            revealed_address = node.keys.address
                        else:
                            revealed_address = "private"
                        status = {"protocolversion": node.version,
                                  "address": revealed_address,
                                  "walletversion": VERSION,
                                  "testnet": node.is_testnet,
                                  "blocks": node.hdd_block, "timeoffset": 0,
                                  "connections": node.peers.consensus_size,
                                  "connections_list": node.peers.peer_opinion_dict,
                                  "difficulty": tempdiff[0],
                                  "threads": threading.active_count(),
                                  "uptime": uptime, "consensus": node.peers.consensus,
                                  "consensus_percent": node.peers.consensus_percentage,
                                  "python_version": str(version_info[:3]),
                                  "last_block_ago": node.last_block_ago,
                                  "server_timestamp": '%.2f' % time.time()}
                        if node.is_regnet:
                            status['regnet'] = True
                        send(self.request, status)
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for statusjson command")
                elif data[:4] == 'api_':
                    if node.peers.is_allowed(peer_ip, data):
                        try:
                            node.apihandler.dispatch(data, self.request, db_handler_instance, node.peers)
                        except Exception as e:
                            if node.debug:
                                raise
                            else:
                                node.logger.app_log.warning(e)

                elif data == "diffget":
                    if node.peers.is_allowed(peer_ip, data):
                        diff = node.difficulty
                        send(self.request, diff)
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for diffget command")

                elif data == "portget":
                    if node.peers.is_allowed(peer_ip, data):
                        send(self.request, {"port": node.port})
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for portget command")

                elif data == "diffgetjson":
                    if node.peers.is_allowed(peer_ip, data):
                        diff = node.difficulty
                        response = {"difficulty": diff[0],
                                    "diff_dropped": diff[1],
                                    "time_to_generate": diff[2],
                                    "diff_block_previous": diff[3],
                                    "block_time": diff[4],
                                    "hashrate": diff[5],
                                    "diff_adjustment": diff[6],
                                    "block_height": diff[7]}

                        send(self.request, response)
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for diffgetjson command")

                elif data == "difflast":
                    if node.peers.is_allowed(peer_ip, data):
                        difflast = db_handler_instance.difflast()

                        send(self.request, difflast)
                    else:
                        node.logger.app_log.info("f{peer_ip} not whitelisted for difflastget command")

                elif data == "difflastjson":
                    if node.peers.is_allowed(peer_ip, data):

                        difflast = db_handler_instance.difflast()
                        response = {"block": difflast[0],
                                    "difficulty": difflast[1]
                                    }
                        send(self.request, response)
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for difflastjson command")

                elif data == "stop":
                    if node.peers.is_allowed(peer_ip, data):
                        node.logger.app_log.warning(f"Received stop from {peer_ip}")
                        node.IS_STOPPING = True
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for stop command")

                elif data == "block_height_from_hash":
                    if node.peers.is_allowed(peer_ip, data):
                        hash = receive(self.request)
                        response = db_handler_instance.block_height_from_hash(hash)
                        send(self.request, response)
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for block_height_from_hash command")

                elif data == "addpeers":
                    if node.peers.is_allowed(peer_ip, data):
                        data = receive(self.request)
                        # peersync expects a dict encoded as json string, not a straight dict
                        try:
                            res = node.peers.peersync(data)
                        except:
                            node.logger.app_log.warning(f"{peer_ip} sent invalid peers list")
                            raise
                        send(self.request, {"added": res})
                        node.logger.app_log.warning(f"{res} peers added")
                    else:
                        node.logger.app_log.warning(f"{peer_ip} not whitelisted for addpeers")

                else:
                    if data == '*':
                        raise ValueError("Broken pipe")

                    # This is the entry point for all extra commands from plugins
                    for prefix, callback in extra_commands.items():
                        if data.startswith(prefix):
                            extra = True
                            callback(data, self.request)

                    if not extra:
                        raise ValueError("Unexpected error, received: " + str(data)[:32] + ' ...')

                if not time.time() <= timer_operation + timeout_operation:
                    timer_operation = time.time()  # reset timer
                # time.sleep(float(node.pause))  # prevent cpu overload
                node.logger.app_log.info(f"Server loop finished for {peer_ip}")

            except Exception as e:

                node.logger.app_log.info(f"Inbound: Lost connection to {peer_ip}")
                node.logger.app_log.info(f"Inbound: {e}")

                # remove from consensus (connection from them)
                node.peers.consensus_remove(peer_ip)
                # remove from consensus (connection from them)
                self.request.close()

                if node.debug:
                    raise  # major debug client
                else:
                    return

        if not node.peers.version_allowed(peer_ip, node.version_allow):
            node.logger.app_log.warning(f"Inbound: Closing connection to old {peer_ip} node: {node.peers.ip_to_mainnet[peer_ip]}")
        return


class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    pass


def just_int_from(s):
    # TODO: move to essentials.py
    return int(''.join(i for i in s if i.isdigit()))


def setup_net_type():
    """
    Adjust globals depending on mainnet, testnet or regnet
    """
    # TODO: only deals with 'node' structure, candidate for single user mode.
    # Defaults value, dup'd here for clarity sake.
    node.is_mainnet = True
    node.is_testnet = False
    node.is_regnet = False

    if "testnet" in node.version or node.is_testnet:
        node.is_testnet = True
        node.is_mainnet = False
        node.version_allow = "testnet"

    if "regnet" in node.version or node.is_regnet:
        node.is_regnet = True
        node.is_testnet = False
        node.is_mainnet = False

    node.logger.app_log.warning(f"Testnet: {node.is_testnet}")
    node.logger.app_log.warning(f"Regnet : {node.is_regnet}")

    # default mainnet config
    node.peerfile = "peers.txt"
    node.ledger_ram_file = "file:ledger?mode=memory&cache=shared"
    node.index_db = "static/index.db"

    if node.is_mainnet:
        # Allow only 21 and up
        if node.version != 'mainnet0022':
            node.version = 'mainnet0022'  # Force in code.
        if "mainnet0021" not in node.version_allow:
            node.version_allow = ['mainnet0021', 'mainnet0022', 'mainnet0023']
        # Do not allow bad configs.
        if not 'mainnet' in node.version:
            node.logger.app_log.error("Bad mainnet version, check config.txt")
            sys.exit()
        num_ver = just_int_from(node.version)
        if num_ver <21:
            node.logger.app_log.error("Too low mainnet version, check config.txt")
            sys.exit()
        for allowed in node.version_allow:
            num_ver = just_int_from(allowed)
            if num_ver < 20:
                node.logger.app_log.error("Too low allowed version, check config.txt")
                sys.exit()

    if "testnet" in node.version or node.is_testnet:
        node.port = 2829
        node.hyper_path = "static/hyper_test.db"
        node.ledger_path = "static/ledger_test.db"

        node.ledger_ram_file = "file:ledger_testnet?mode=memory&cache=shared"
        #node.hyper_recompress = False
        node.peerfile = "peers_test.txt"
        node.index_db = "static/index_test.db"
        if not 'testnet' in node.version:
            node.logger.app_log.error("Bad testnet version, check config.txt")
            sys.exit()

        redownload_test = input("Status: Welcome to the testnet. Redownload test ledger? y/n")
        if redownload_test == "y":
            types = ['static/ledger_test.db-wal', 'static/ledger_test.db-shm', 'static/index_test.db', 'static/hyper_test.db-wal', 'static/hyper_test.db-shm']
            for type in types:
                for file in glob.glob(type):
                    os.remove(file)
                    print(file, "deleted")
            download_file("https://bismuth.cz/test.tar.gz", "static/test.tar.gz")
            with tarfile.open("static/test.tar.gz") as tar:
                def is_within_directory(directory, target):
                    
                    abs_directory = os.path.abspath(directory)
                    abs_target = os.path.abspath(target)
                
                    prefix = os.path.commonprefix([abs_directory, abs_target])
                    
                    return prefix == abs_directory
                
                def safe_extract(tar, path=".", members=None, *, numeric_owner=False):
                
                    for member in tar.getmembers():
                        member_path = os.path.join(path, member.name)
                        if not is_within_directory(path, member_path):
                            raise Exception("Attempted Path Traversal in Tar File")
                
                    tar.extractall(path, members, numeric_owner=numeric_owner) 
                    
                
                safe_extract(tar, "static/")
        else:
            print("Not redownloading test db")

    if "regnet" in node.version or node.is_regnet:
        node.port = regnet.REGNET_PORT
        node.hyper_path = regnet.REGNET_DB
        node.ledger_path = regnet.REGNET_DB
        node.ledger_ram_file = "file:ledger_regnet?mode=memory&cache=shared"
        node.hyper_recompress = False
        node.peerfile = regnet.REGNET_PEERS
        node.index_db = regnet.REGNET_INDEX
        if not 'regnet' in node.version:
            node.logger.app_log.error("Bad regnet version, check config.txt")
            sys.exit()
        if not node.heavy:
            node.logger.app_log.warning("Regnet with no heavy file...")
            mining_heavy3.heavy = False
        node.logger.app_log.warning("Regnet init...")
        regnet.init(node.logger.app_log)
        regnet.DIGEST_BLOCK = digest_block
        mining_heavy3.is_regnet = True
        """
        node.logger.app_log.warning("Regnet still is WIP atm.")
        sys.exit()
        """


def node_block_init(database):
    # TODO: candidate for single user mode
    node.hdd_block = database.block_height_max()
    node.difficulty = difficulty(node, db_handler_initial)  # check diff for miner

    node.last_block = node.hdd_block  # ram equals drive at this point

    node.last_block_hash = database.last_block_hash()
    node.hdd_hash = node.last_block_hash # ram equals drive at this point

    node.last_block_timestamp = database.last_block_timestamp()

    checkpoint_set(node)

    node.logger.app_log.warning("Status: Indexing aliases")

    aliases.aliases_update(node, database)


def ram_init(database):
    # TODO: candidate for single user mode
    try:
        if node.ram:
            node.logger.app_log.warning("Status: Moving database to RAM")

            if node.py_version >= 370:
                temp_target = sqlite3.connect(node.ledger_ram_file, uri=True, isolation_level=None, timeout=1)
                if node.trace_db_calls:
                    temp_target.set_trace_callback(functools.partial(sql_trace_callback,node.logger.app_log,"TEMP-TARGET"))

                temp_source = sqlite3.connect(node.hyper_path, uri=True, isolation_level=None, timeout=1)
                if node.trace_db_calls:
                    temp_source.set_trace_callback(functools.partial(sql_trace_callback,node.logger.app_log,"TEMP-SOURCE"))
                temp_source.backup(temp_target)
                temp_source.close()

            else:
                source_db = sqlite3.connect(node.hyper_path, timeout=1)
                if node.trace_db_calls:
                    source_db.set_trace_callback(functools.partial(sql_trace_callback,node.logger.app_log,"SOURCE-DB"))
                database.to_ram = sqlite3.connect(node.ledger_ram_file, uri=True, timeout=1, isolation_level=None)
                if node.trace_db_calls:
                    database.to_ram.set_trace_callback(functools.partial(sql_trace_callback,node.logger.app_log,"DATABASE-TO-RAM"))
                database.to_ram.text_factory = str
                database.tr = database.to_ram.cursor()

                query = "".join(line for line in source_db.iterdump())
                database.to_ram.executescript(query)
                source_db.close()

            node.logger.app_log.warning("Status: Hyperblock ledger moved to RAM")

            #source = sqlite3.connect('existing_db.db')
            #dest = sqlite3.connect(':memory:')
            #source.backup(dest)

    except Exception as e:
        node.logger.app_log.warning(e)
        raise


def initial_db_check():
    """
    Initial bootstrap check and chain validity control
    """
    # TODO: candidate for single user mode
    # force bootstrap via adding an empty "fresh_sync" file in the dir.
    if os.path.exists("fresh_sync") and node.is_mainnet:
        node.logger.app_log.warning("Status: Fresh sync required, bootstrapping from the website")
        os.remove("fresh_sync")
        bootstrap()
    # UPDATE mainnet DB if required
    if node.is_mainnet:
        upgrade = sqlite3.connect(node.ledger_path)
        if node.trace_db_calls:
            upgrade.set_trace_callback(functools.partial(sql_trace_callback,node.logger.app_log,"INITIAL_DB_CHECK"))
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


def load_keys():
    """Initial loading of crypto keys"""
    # TODO: candidate for single user mode
    essentials.keys_check(node.logger.app_log, "wallet.der")

    node.keys.key, node.keys.public_key_readable, node.keys.private_key_readable, _, _, node.keys.public_key_b64encoded, node.keys.address, node.keys.keyfile = essentials.keys_load(
        "privkey.der", "pubkey.der")

    if node.is_regnet:
        regnet.PRIVATE_KEY_READABLE = node.keys.private_key_readable
        regnet.PUBLIC_KEY_B64ENCODED = node.keys.public_key_b64encoded
        regnet.ADDRESS = node.keys.address
        regnet.KEY = node.keys.key

    node.logger.app_log.warning(f"Status: Local address: {node.keys.address}")


def verify(db_handler):
    # TODO: candidate for single user mode
    try:
        node.logger.app_log.warning("Blockchain verification started...")
        # verify blockchain
        db_handler.execute(db_handler.h, "SELECT Count(*) FROM transactions")
        db_rows = db_handler.h.fetchone()[0]
        node.logger.app_log.warning("Total steps: {}".format(db_rows))

        # verify genesis
        try:
            db_handler.execute(db_handler.h, "SELECT block_height, recipient FROM transactions WHERE block_height = 1")
            result = db_handler.h.fetchall()[0]
            block_height = result[0]
            genesis = result[1]
            node.logger.app_log.warning(f"Genesis: {genesis}")
            if str(genesis) != node.genesis and int(
                    block_height) == 0:
                node.logger.app_log.warning("Invalid genesis address")
                sys.exit(1)
        except:
            node.logger.app_log.warning("Hyperblock mode in use")
        # verify genesis

        invalid = 0

        for row in db_handler.h.execute('SELECT * FROM transactions WHERE block_height > 0 and reward = 0 ORDER BY block_height'):  # native sql fx to keep compatibility

            db_block_height = str(row[0])
            db_timestamp = '%.2f' % (quantize_two(row[1]))
            db_address = str(row[2])[:56]
            db_recipient = str(row[3])[:56]
            db_amount = '%.8f' % (quantize_eight(row[4]))
            db_signature_enc = str(row[5])[:684]
            db_public_key_b64encoded = str(row[6])[:1068]
            db_operation = str(row[10])[:30]
            db_openfield = str(row[11])  # no limit for backward compatibility
            db_transaction = str((db_timestamp, db_address, db_recipient, db_amount, db_operation, db_openfield)).encode("utf-8")

            try:
                # Signer factory is aware of the different tx schemes, and will b64 decode public_key once or twice as needed.
                SignerFactory.verify_bis_signature(db_signature_enc, db_public_key_b64encoded, db_transaction, db_address)
            except Exception as e:
                sha_hash = SHA.new(db_transaction)
                try:
                    if sha_hash.hexdigest() != db_hashes[db_block_height + "-" + db_timestamp]:
                        node.logger.app_log.warning("Signature validation problem: {} {}".format(db_block_height, db_transaction))
                        invalid = invalid + 1
                except Exception as e:
                    node.logger.app_log.warning("Signature validation problem: {} {}".format(db_block_height, db_transaction))
                    invalid = invalid + 1

        if invalid == 0:
            node.logger.app_log.warning("All transacitons in the local ledger are valid")

    except Exception as e:
        node.logger.app_log.warning("Error: {}".format(e))
        raise


def add_indices(db_handler: dbhandler.DbHandler):
    CREATE_TXID4_INDEX_IF_NOT_EXISTS = "CREATE INDEX IF NOT EXISTS TXID4_Index ON transactions(substr(signature,1,4))"
    CREATE_MISC_BLOCK_HEIGHT_INDEX_IF_NOT_EXISTS = "CREATE INDEX IF NOT EXISTS 'Misc Block Height Index' on misc(block_height)"

    node.logger.app_log.warning("Creating indices")

    # ledger.db
    if not node.old_sqlite:
        db_handler.execute(db_handler.h, CREATE_TXID4_INDEX_IF_NOT_EXISTS)
    else:
        node.logger.app_log.warning("Setting old_sqlite is True, lookups will be slower.")
    db_handler.execute(db_handler.h, CREATE_MISC_BLOCK_HEIGHT_INDEX_IF_NOT_EXISTS)

    # hyper.db
    if not node.old_sqlite:
        db_handler.execute(db_handler.h2, CREATE_TXID4_INDEX_IF_NOT_EXISTS)
    db_handler.execute(db_handler.h2, CREATE_MISC_BLOCK_HEIGHT_INDEX_IF_NOT_EXISTS)

    # RAM or hyper.db
    if not node.old_sqlite:
        db_handler.execute(db_handler.c, CREATE_TXID4_INDEX_IF_NOT_EXISTS)
    db_handler.execute(db_handler.c, CREATE_MISC_BLOCK_HEIGHT_INDEX_IF_NOT_EXISTS)

    node.logger.app_log.warning("Finished creating indices")


if __name__ == "__main__":
    # classes
    node = node.Node()
    node.logger = logger.Logger()
    node.keys = keys.Keys()

    node.is_testnet = False
    # regnet takes over testnet
    node.is_regnet = False
    # if it's not testnet, nor regnet, it's mainnet
    node.is_mainnet = True

    config = options.Get()
    config.read()
    # classes

    node.app_version = VERSION
    # TODO: we could just loop over config items, and assign them to node.
    # or just do node.config = config
    # and use node.config.port... aso

    # TODO: Simplify. Just do node.config = config, then use node.config.required_option
    node.version = config.version
    node.debug_level = config.debug_level
    node.port = config.port
    node.verify = config.verify
    node.thread_limit = config.thread_limit
    node.rebuild_db = config.rebuild_db
    node.debug = config.debug
    node.debug_level = config.debug_level
    node.pause = config.pause
    node.ledger_path = config.ledger_path
    node.hyper_path = config.hyper_path
    node.hyper_recompress = config.hyper_recompress
    node.tor = config.tor
    node.ram = config.ram
    node.version_allow = config.version_allow
    node.reveal_address = config.reveal_address
    node.terminal_output = config.terminal_output
    node.egress = config.egress
    node.genesis = config.genesis
    node.accept_peers = config.accept_peers
    node.full_ledger = config.full_ledger
    node.trace_db_calls = config.trace_db_calls
    node.heavy3_path = config.heavy3_path
    node.old_sqlite = config.old_sqlite
    node.heavy = config.heavy

    node.logger.app_log = log.log("node.log", node.debug_level, node.terminal_output)
    node.logger.app_log.warning("Configuration settings loaded")
    node.logger.app_log.warning(f"Python version: {node.py_version}")

    # upgrade wallet location after nuitka-required "files" folder introduction
    if os.path.exists("../wallet.der") and not os.path.exists("wallet.der") and "Windows" in platform.system():
        print("Upgrading wallet location")
        os.rename("../wallet.der", "wallet.der")
    # upgrade wallet location after nuitka-required "files" folder introduction

    if not node.full_ledger and os.path.exists(node.ledger_path) and node.is_mainnet:
        os.remove(node.ledger_path)
        node.logger.app_log.warning("Removed full ledger for hyperblock mode")
    if not node.full_ledger:
        node.logger.app_log.warning("Cloning hyperblocks to ledger file")
        shutil.copy(node.hyper_path, node.ledger_path)  # hacked to remove all the endless checks
    try:
        # create a plugin manager, load all plugin modules and init
        node.plugin_manager = plugins.PluginManager(app_log=node.logger.app_log, config=config, init=True)
        # get the potential extra command prefixes from plugin
        extra_commands = {}  # global var, used by the server part.
        extra_commands = node.plugin_manager.execute_filter_hook('extra_commands_prefixes', extra_commands)
        print("Extra prefixes: ", ",".join(extra_commands.keys()))

        setup_net_type()
        load_keys()

        # needed for docker logs
        node.logger.app_log.warning(f"Checking Heavy3 file, can take up to 5 minutes...")
        mining_heavy3.mining_open(node.heavy3_path)
        node.logger.app_log.warning(f"Heavy3 file Ok!")

        node.logger.app_log.warning(f"Status: Starting node version {VERSION}")
        node.startup_time = time.time()
        try:

            node.peers = peershandler.Peers(node.logger.app_log, config=config, node=node)

            # print(peers.peer_list_old_format())
            # sys.exit()

            node.apihandler = apihandler.ApiHandler(node.logger.app_log, config)
            mp.MEMPOOL = mp.Mempool(node.logger.app_log, config, node.db_lock, node.is_testnet, trace_db_calls=node.trace_db_calls)

            check_integrity(node.hyper_path)
            #PLACEHOLDER FOR FRESH HYPERBLOCK BUILDER

            # if node.rebuild_db: #does nothing
            #    db_maintenance(init_database)

            # db_manager = db_looper.DbManager(node.logger.app_log)
            # db_manager.start()

            db_handler_initial = dbhandler.DbHandler(node.index_db, node.ledger_path, node.hyper_path, node.ram, node.ledger_ram_file, node.logger, trace_db_calls=node.trace_db_calls)

            ledger_check_heights(node, db_handler_initial)


            if node.recompress:
                #todo: do not close database and move files, swap tables instead
                db_handler_initial.close()
                recompress_ledger(node)
                db_handler_initial = dbhandler.DbHandler(node.index_db, node.ledger_path, node.hyper_path, node.ram, node.ledger_ram_file, node.logger, trace_db_calls=node.trace_db_calls)

            ram_init(db_handler_initial)
            node_block_init(db_handler_initial)
            initial_db_check()

            if not node.is_regnet:
                sequencing_check(db_handler_initial)

            if node.verify:
                verify(db_handler_initial)

            add_indices(db_handler_initial)

            # TODO: until here, we are in single user mode.
            # All the above goes into a "bootup" function, with methods from single_user module only.

            if not node.tor:
                # Port 0 means to select an arbitrary unused port
                host, port = "0.0.0.0", int(node.port)

                ThreadedTCPServer.allow_reuse_address = True
                ThreadedTCPServer.daemon_threads = True
                ThreadedTCPServer.timeout = 60
                ThreadedTCPServer.request_queue_size = 100

                server = ThreadedTCPServer((host, port), ThreadedTCPRequestHandler)
                ip, node.port = server.server_address

                # Start a thread with the server -- that thread will then start one
                # more thread for each request

                server_thread = threading.Thread(target=server.serve_forever)
                server_thread.daemon = True
                server_thread.start()

                node.logger.app_log.warning("Status: Server loop running.")

            else:
                node.logger.app_log.warning("Status: Not starting a local server to conceal identity on Tor network")

            # start connection manager
            connection_manager = connectionmanager.ConnectionManager(node, mp)
            connection_manager.start()
            # start connection manager

        except Exception as e:
            node.logger.app_log.info(e)
            raise

    except Exception as e:
        node.logger.app_log.info(e)
        raise

    node.logger.app_log.warning("Status: Bismuth loop running.")

    while True:
        if node.IS_STOPPING:
            if node.db_lock.locked():
                time.sleep(0.5)
            else:
                mining_heavy3.mining_close()
                node.logger.app_log.warning("Status: Securely disconnected main processes, subprocess termination in progress.")
                break
        time.sleep(0.1)
    node.logger.app_log.warning("Status: Clean Stop")
