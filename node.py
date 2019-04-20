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



VERSION = "4.2.9.3"

import base64
import glob
import hashlib
import math
import os
import queue
import re
import shutil
import socketserver
import sqlite3
import sys
import tarfile
import threading
import time
from hashlib import blake2b
import platform
import requests
import socks
from Cryptodome.Hash import SHA
from Cryptodome.PublicKey import RSA
from Cryptodome.Signature import PKCS1_v1_5

import aliases
# Bis specific modules
import apihandler
from libs import node, logger, keys, client

from connections import send, receive

import dbhandler
import essentials
import wallet_keys
import log
import mempool as mp
import mining
import mining_heavy3
import options
import peershandler
import plugins
import regnet
import staking
import tokensv2 as tokens
from essentials import fee_calculate, checkpoint_set, replace_regex, download_file, most_common
from quantizer import *
import connectionmanager
from difficulty import *
from fork import *
from digest import *

# load config



getcontext().rounding = ROUND_HALF_EVEN
POW_FORK, FORK_AHEAD, FORK_DIFF = fork()


from appdirs import *

appname = "Bismuth"
appauthor = "Bismuth Foundation"

# nodes_ban_reset=config.nodes_ban_reset


# load config


def bootstrap():
    try:
        types = ['static/*.db-wal', 'static/*.db-shm']
        for t in types:
            for f in glob.glob(t):
                os.remove(f)
                print(f, "deleted")

        archive_path = node.ledger_path + ".tar.gz"
        download_file("https://bismuth.cz/ledger.tar.gz", archive_path)

        with tarfile.open(archive_path) as tar:
            tar.extractall("static/")  # NOT COMPATIBLE WITH CUSTOM PATH CONFS

    except:
        node.logger.app_log.warning("Something went wrong during bootstrapping, aborted")
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
            node.logger.app_log.warning(
                f"Status: Integrity check on database {database} failed, bootstrapping from the website")
            redownload = True

    if redownload and node.is_mainnet:
        bootstrap()

def rollback(node, db_handler, block_height):
    node.logger.app_log.warning(f"Status: Rolling back below: {block_height}")

    db_handler.rollback_to(block_height)

    # rollback indices
    db_handler.tokens_rollback(node, block_height)
    db_handler.aliases_rollback(node, block_height)
    db_handler.staking_rollback(node, block_height)
    # rollback indices

    node.logger.app_log.warning(f"Status: Chain rolled back below {block_height} and will be resynchronized")


def recompress_ledger(node, rebuild=False, depth=15000):

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


    if os.path.exists(node.hyper_path) and rebuild:
        os.remove(node.hyper_path)  # remove the old hyperblocks to rebuild
        os.rename(node.ledger_path + '.temp', node.hyper_path)


def ledger_check_heights(node, db_handler):
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
            recompress = True
        elif hdd_block_max == hdd2_block_last and not node.hyper_recompress:
            node.logger.app_log.warning("Status: Hyperblock recompression skipped")
            recompress = False
        else:
            lowest_block = min(hdd_block_max, hdd2_block_last, hdd_block_max_diff, hdd2_block_last_misc)
            highest_block = max(hdd_block_max, hdd2_block_last, hdd_block_max_diff, hdd2_block_last_misc)


            node.logger.app_log.warning(
                f"Status: Cross-integrity check failed, {highest_block} will be rolled back below {lowest_block}")

            rollback(node,db_handler_initial,lowest_block) #rollback to the lowest value

            recompress = True


    else:
        node.logger.app_log.warning("Status: Compressing ledger to Hyperblocks")
        recompress = True

    if recompress:
        recompress_ledger(node)


def bin_convert(string):
    return ''.join(format(ord(x), '8b').replace(' ', '0') for x in string)




def balanceget(balance_address, db_handler):
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
                db_handler.rollback_to(db_block_height)
                node.hdd_block = db_handler.block_height_max()
                # /roll back hdd too

                # rollback indices
                db_handler.tokens_rollback(node, db_block_height)
                db_handler.aliases_rollback(node, db_block_height)
                db_handler.staking_rollback(node, db_block_height)
                # /rollback indices

        except Exception as e:
            node.logger.app_log.warning(e)


        finally:
            node.db_lock.release()
            node.logger.app_log.warning(f"Database lock released")

            if skip:
                rollback = {"timestamp": my_time, "height": db_block_height, "ip": peer_ip,
                            "sha_hash": db_block_hash, "skipped": True, "reason": reason}
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
                                "sha_hash": db_block_hash, "tx_count": nb_tx, "skipped": False, "reason": ""}
                    node.plugin_manager.execute_action_hook('rollback', rollback)

                except Exception as e:
                    node.logger.app_log.warning(f"Error during moving txs back to mempool: {e}")

    else:
        reason = "Skipping rollback, other ledger operation in progress"
        rollback = {"timestamp": my_time, "ip": peer_ip, "skipped": True, "reason": reason}
        node.plugin_manager.execute_action_hook('rollback', rollback)
        node.logger.app_log.info(reason)

def sequencing_check(db_handler):
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
                    c2 = conn2.cursor()
                    node.logger.app_log.warning(f"Status: Chain {chain} transaction sequencing error at: {row[0]}. {row[0]} instead of {y}")
                    c2.execute("DELETE FROM transactions WHERE block_height >= ? OR block_height <= ?", (row[0], -row[0],))
                    conn2.commit()
                    c2.execute("DELETE FROM misc WHERE block_height >= ?", (row[0],))
                    conn2.commit()

                    # rollback indices
                    db_handler.tokens_rollback(node, y)
                    db_handler.aliases_rollback(node, y)
                    db_handler.staking_rollback(node, y)

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
                    c2 = conn2.cursor()
                    node.logger.app_log.warning(
                        f"Status: Chain {chain} difficulty sequencing error at: {row[0]} {row[0]} instead of {y}")
                    c2.execute("DELETE FROM transactions WHERE block_height >= ?", row[0],)
                    conn2.commit()
                    c2.execute("DELETE FROM misc WHERE block_height >= ?", row[0],)
                    conn2.commit()

                    db_handler.execute_param(conn2, (
                        'DELETE FROM transactions WHERE address = "Development Reward" AND block_height <= ?'),
                                             (-row[0],))
                    conn2.commit()
                    conn2.close()

                    # rollback indices
                    db_handler.tokens_rollback(node, y)
                    db_handler.aliases_rollback(node, y)
                    db_handler.staking_rollback(node, y)
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
                node.logger.app_log.warning(f"Free capacity for {peer_ip} unavailable, disconnected")
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
            client_instance.banned = True
            self.request.close()
            node.logger.app_log.info(f"IP {peer_ip} banned, disconnected")


        timeout_operation = 120  # timeout
        timer_operation = time.time()  # start counting

        if not client_instance.banned and node.peers.version_allowed(peer_ip, node.version_allow) and client_instance.connected:
            db_handler_instance = dbhandler.DbHandler(node.index_db, node.ledger_path, node.hyper_path, node.ram, node.ledger_ram_file, node.logger)

        while not client_instance.banned and node.peers.version_allowed(peer_ip, node.version_allow) and client_instance.connected:
            try:

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

                    # send own

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

                        if int(received_block_height) >= block_req:

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
                                f"Inbound: Distant peer {peer_ip} is at {received_block_height}, should be at least {block_req}")

                    send(self.request, "sync")

                elif data == "blockheight":
                    try:
                        received_block_height = receive(self.request)  # receive client's last block height
                        node.logger.app_log.info(
                            f"Inbound: Received block height {received_block_height} from {peer_ip} ")

                        # consensus pool 1 (connection from them)
                        consensus_blockheight = int(received_block_height)  # str int to remove leading zeros
                        # consensus_add(peer_ip, consensus_blockheight, self.request)
                        node.peers.consensus_add(peer_ip, consensus_blockheight, self.request, node.last_block)
                        # consensus pool 1 (connection from them)

                        db_block_height = db_handler_instance.block_height_max()

                        # append zeroes to get static length
                        send(self.request, db_block_height)
                        # send own block height

                        if int(received_block_height) > db_block_height:
                            node.logger.app_log.warning("Inbound: Client has higher block")

                            db_handler_instance.execute(db_handler_instance.c,
                                                        'SELECT block_hash FROM transactions ORDER BY block_height DESC LIMIT 1')
                            db_block_hash = db_handler_instance.c.fetchone()[0]  # get latest block_hash

                            node.logger.app_log.info(f"Inbound: block_hash to send: {db_block_hash}")
                            send(self.request, db_block_hash)

                            # receive their latest sha_hash
                            # confirm you know that sha_hash or continue receiving

                        elif int(received_block_height) <= db_block_height:
                            if int(received_block_height) == db_block_height:
                                node.logger.app_log.info(
                                    f"Inbound: We have the same height as {peer_ip} ({received_block_height}), hash will be verified")
                            else:
                                node.logger.app_log.warning(
                                    f"Inbound: We have higher ({db_block_height}) block height than {peer_ip} ({received_block_height}), hash will be verified")

                            data = receive(self.request)  # receive client's last block_hash
                            # send all our followup hashes

                            node.logger.app_log.info(f"Inbound: Will seek the following block: {data}")

                            try:
                                db_handler_instance.execute_param(db_handler_instance.h,
                                                                  "SELECT block_height FROM transactions WHERE block_hash = ?;", (data,))
                                client_block = db_handler_instance.h.fetchone()[0]
                            except Exception:
                                node.logger.app_log.warning(f"Inbound: Block {data[:8]} of {peer_ip} not found")
                                if node.full_ledger:
                                    send(self.request, "blocknf") #announce block hash was not found
                                else:
                                    send(self.request, "blocknfhb") #announce we are on hyperblocks
                                send(self.request, data)

                            else:
                                node.logger.app_log.info(f"Inbound: Client is at block {client_block}")  # now check if we have any newer

                                db_handler_instance.execute(db_handler_instance.h,
                                                            'SELECT block_hash FROM transactions ORDER BY block_height DESC LIMIT 1')
                                db_block_hash = db_handler_instance.h.fetchone()[0]  # get latest block_hash
                                if db_block_hash == data or not node.egress:
                                    if not node.egress:
                                        node.logger.app_log.warning(f"Outbound: Egress disabled for {peer_ip}")
                                    else:
                                        node.logger.app_log.info(f"Inbound: Client {peer_ip} has the latest block")

                                    time.sleep(int(node.pause))  # reduce CPU usage
                                    send(self.request, "nonewblk")

                                else:

                                    blocks_fetched = []
                                    del blocks_fetched[:]
                                    while sys.getsizeof(
                                            str(blocks_fetched)) < 500000:  # limited size based on txs in blocks
                                        db_handler_instance.execute_param(db_handler_instance.h, (
                                            "SELECT timestamp,address,recipient,amount,signature,public_key,operation,openfield FROM transactions WHERE block_height > ? AND block_height <= ?;"),
                                                                          (str(int(client_block)), str(int(client_block + 1)),))
                                        result = db_handler_instance.h.fetchall()
                                        if not result:
                                            break
                                        blocks_fetched.extend([result])
                                        client_block = int(client_block) + 1

                                    # blocks_send = [[l[1:] for l in group] for _, group in groupby(blocks_fetched, key=itemgetter(0))]  # remove block number

                                    # node.logger.app_log.info("Inbound: Selected " + str(blocks_fetched) + " to send")

                                    send(self.request, "blocksfnd")

                                    confirmation = receive(self.request)

                                    if confirmation == "blockscf":
                                        node.logger.app_log.info("Inbound: Client confirmed they want to sync from us")
                                        send(self.request, blocks_fetched)

                                    elif confirmation == "blocksrj":
                                        node.logger.app_log.info(
                                            "Inbound: Client rejected to sync from us because we're don't have the latest block")



                    except Exception as e:
                        node.logger.app_log.info(f"Inbound: Sync failed {e}")

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
                    node.logger.app_log.info("Outbound: Deletion complete, sending sync request")

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
                    node.logger.app_log.info("Outbound: Deletion complete, sending sync request")

                    while node.db_lock.locked():
                        time.sleep(node.pause)
                    send(self.request, "sync")

                elif data == "block":
                    # if (peer_ip in allowed or "any" in allowed):  # from miner
                    if node.peers.is_allowed(peer_ip, data):  # from miner
                        # TODO: rights management could be done one level higher instead of repeating the same check everywhere

                        node.logger.app_log.info(f"Outbound: Received a block from miner {peer_ip}")
                        # receive block
                        segments = receive(self.request)
                        # node.logger.app_log.info("Inbound: Combined mined segments: " + segments)

                        # check if we have the latest block

                        db_block_height = db_handler_instance.block_height_max()

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
                                node.logger.app_log.info(reason)
                            elif node.db_lock.locked():
                                reason = "Outbound: Block from miner skipped because we are digesting already"
                                mined['reason'] = reason
                                node.plugin_manager.execute_action_hook('mined', mined)
                                node.logger.app_log.warning(reason)
                            elif db_block_height >= node.peers.consensus_max - 3:
                                mined['result'] = True
                                node.plugin_manager.execute_action_hook('mined', mined)
                                node.logger.app_log.info("Outbound: Processing block from miner")
                                digest_block(node, segments, self.request, peer_ip, db_handler_instance)
                                # This new block may change the int(diff). Trigger the hook whether it changed or not.
                                #node.difficulty = difficulty(node, db_handler_instance)

                            else:
                                reason = f"Outbound: Mined block was orphaned because node was not synced, we are at block {db_block_height}, should be at least {node.peers.consensus_max - 3}"
                                mined['reason'] = reason
                                node.plugin_manager.execute_action_hook('mined', mined)
                                node.logger.app_log.warning(reason)
                        else:
                            digest_block(node, segments, self.request, peer_ip, db_handler_instance)

                    else:
                        receive(self.request)  # receive block, but do nothing about it
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for block command")

                elif data == "blocklast":
                    # if (peer_ip in allowed or "any" in allowed):  # only sends the miner part of the block!
                    if node.peers.is_allowed(peer_ip, data):
                        db_handler_instance.execute(db_handler_instance.c,
                                                    "SELECT * FROM transactions WHERE reward != 0 ORDER BY block_height DESC LIMIT 1;")
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

                    # node.logger.app_log.info("Outbound: Extracted from the mempool: " + str(mempool_txs))  # improve: sync based on signatures only

                    # if len(mempool_txs) > 0: #wont sync mempool until we send something, which is bad
                    # send own
                    send(self.request, response_list)

                elif data == "mpget" and node.peers.is_allowed(peer_ip, data):
                    mempool_txs = mp.MEMPOOL.fetchall(mp.SQL_SELECT_TX_TO_SEND)

                    # node.logger.app_log.info("Outbound: Extracted from the mempool: " + str(mempool_txs))  # improve: sync based on signatures only

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

                        send(self.request, result)
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for addlistlimmir command")


                elif data == "aliasget":  # all for a single address, no protection against overlapping
                    if node.peers.is_allowed(peer_ip, data):
                        aliases.aliases_update(node.index_db, node.ledger_path, "normal", node.logger.app_log)

                        alias_address = receive(self.request)
                        result = db_handler_instance.aliasget(alias_address)

                        send(self.request, result)
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for aliasget command")

                elif data == "aliasesget":  # only gets the first one, for multiple addresses
                    if node.peers.is_allowed(peer_ip, data):
                        aliases.aliases_update(node.index_db, node.ledger_path, "normal", node.logger.app_log)
                        aliases_request = receive(self.request)
                        results = db_handler_instance.aliasesget(aliases_request)
                        send(self.request, results)
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for aliasesget command")

                # Not mandatory, but may help to reindex with minimal sql queries
                elif data == "tokensupdate":
                    if node.peers.is_allowed(peer_ip, data):
                        tokens.tokens_update(node.index_db, node.ledger_path, "normal", node.logger.app_log,
                                             node.plugin_manager)
                #
                elif data == "tokensget":
                    if node.peers.is_allowed(peer_ip, data):
                        tokens.tokens_update(node.index_db, node.ledger_path, "normal", node.logger.app_log,
                                             node.plugin_manager)
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

                        aliases.aliases_update(node.index_db, node.ledger_path, "normal", node.logger.app_log)
                        alias_address = receive(self.request)
                        address_fetch = db_handler_instance.addfromalias(alias_address)
                        node.logger.app_log.warning(f"Fetched the following alias address: {address_fetch}")
                        send(self.request, address_fetch)

                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for addfromalias command")

                elif data == "pubkeyget":
                    if node.peers.is_allowed(peer_ip, data):
                        pub_key_address = receive(self.request)
                        target_public_key_hashed = db_handler_instance.pubkeyget(pub_key_address)
                        send(self.request, target_public_key_hashed)

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
                    if node.peers.is_allowed(peer_ip, data):
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
                                  "testnet": node.is_testnet,  # config data
                                  "blocks": node.last_block, "timeoffset": 0,
                                  "connections": node.peers.consensus_size,
                                  "connections_list": node.peers.peer_opinion_dict,
                                  "difficulty": tempdiff[0],  # live status, bitcoind format
                                  "threads": threading.active_count(),
                                  "uptime": uptime, "consensus": node.peers.consensus,
                                  "consensus_percent": node.peers.consensus_percentage,
                                  "server_timestamp": '%.2f' % time.time()}  # extra data
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
                            print(e)

                elif data == "diffget":
                    if node.peers.is_allowed(peer_ip, data):
                        diff = node.difficulty
                        send(self.request, diff)
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for diffget command")

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


                elif data == "hyperlane":
                    pass

                else:
                    if data == '*':
                        raise ValueError("Broken pipe")

                    extra = False  # This is the entry point for all extra commands from plugins
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
            node.logger.app_log.warning(f"Inbound: Closing connection to old {peer_ip} node: {node.peers.ip_to_mainnet['peer_ip']}")
        return


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

    if "testnet" in node.version or node.is_testnet:
        node.is_testnet = True
        node.is_mainnet = False

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
        # Allow 18 for transition period. Will be auto removed at fork block.
        if node.version != 'mainnet0020':
            node.version = 'mainnet0019'  # Force in code.
        if "mainnet0020" not in node.version_allow:
            node.version_allow = ['mainnet0019', 'mainnet0020', 'mainnet0021']
        # Do not allow bad configs.
        if not 'mainnet' in node.version:
            node.logger.app_log.error("Bad mainnet version, check config.txt")
            sys.exit()
        num_ver = just_int_from(node.version)
        if num_ver < 19:
            node.logger.app_log.error("Too low mainnet version, check config.txt")
            sys.exit()
        for allowed in node.version_allow:
            num_ver = just_int_from(allowed)
            if num_ver < 19:
                node.logger.app_log.error("Too low allowed version, check config.txt")
                sys.exit()

    if node.is_testnet:
        node.port = 2829
        node.hyper_path = "static/test.db"
        node.ledger_path = "static/test.db"  # for tokens
        node.ledger_ram_file = "file:ledger_testnet?mode=memory&cache=shared"
        node.hyper_recompress = False
        node.peerfile = "peers_test.txt"
        node.index_db = "static/index_test.db"
        if not 'testnet' in node.version:
            node.logger.app_log.error("Bad testnet version, check config.txt")
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
        node.hyper_path = regnet.REGNET_DB
        node.ledger_path = regnet.REGNET_DB
        node.ledger_ram_file = "file:ledger_regnet?mode=memory&cache=shared"
        node.hyper_recompress = False
        node.peerfile = regnet.REGNET_PEERS
        node.index_db = regnet.REGNET_INDEX
        if not 'regnet' in node.version:
            node.logger.app_log.error("Bad regnet version, check config.txt")
            sys.exit()
        node.logger.app_log.warning("Regnet init...")
        regnet.init(node.logger.app_log)
        regnet.DIGEST_BLOCK = digest_block
        mining_heavy3.is_regnet = True
        """
        node.logger.app_log.warning("Regnet still is WIP atm.")
        sys.exit()
        """

def node_block_init(database):
    node.hdd_block = database.block_height_max()
    node.last_block = node.hdd_block  # ram equals drive at this point
    checkpoint_set(node, node.hdd_block)

    if node.is_mainnet and (node.hdd_block >= POW_FORK - FORK_AHEAD):
        limit_version(node)

def ram_init(database):
    try:
        if node.ram:
            node.logger.app_log.warning("Status: Moving database to RAM")

            if node.py_version >= 370:
                temp_target = sqlite3.connect(node.ledger_ram_file, uri=True, isolation_level=None, timeout=1)
                temp_source = sqlite3.connect(node.hyper_path, uri=True, isolation_level=None, timeout=1)
                temp_source.backup(temp_target)
                temp_source.close()

            else:
                source_db = sqlite3.connect(node.hyper_path, timeout=1)
                database.to_ram = sqlite3.connect(node.ledger_ram_file, uri=True, timeout=1, isolation_level=None)
                database.to_ram.text_factory = str
                database.tr = database.to_ram.cursor()

                query = "".join(line for line in source_db.iterdump())
                database.to_ram.executescript(query)
                source_db.close()

            node.logger.app_log.warning("Status: Moved database to RAM")

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
    # force bootstrap via adding an empty "fresh_sync" file in the dir.
    if os.path.exists("fresh_sync") and node.is_mainnet:
        node.logger.app_log.warning("Status: Fresh sync required, bootstrapping from the website")
        os.remove("fresh_sync")
        bootstrap()
    # UPDATE mainnet DB if required
    if node.is_mainnet:
        upgrade = sqlite3.connect(node.ledger_path)
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

    node.logger.app_log.warning(f"Status: Indexing tokens from ledger {node.ledger_path}")
    tokens.tokens_update(node.index_db, node.ledger_path, "normal", node.logger.app_log, node.plugin_manager)
    node.logger.app_log.warning("Status: Indexing aliases")
    aliases.aliases_update(node.index_db, node.ledger_path, "normal", node.logger.app_log)





def load_keys():
    """Initial loading of crypto keys"""

    essentials.keys_check(node.logger.app_log, "wallet.der")

    node.keys.key, node.keys.public_key_readable, node.keys.private_key_readable, _, _, node.keys.public_key_hashed, node.keys.address, node.keys.keyfile = essentials.keys_load(
        "privkey.der", "pubkey.der")

    if node.is_regnet:
        regnet.PRIVATE_KEY_READABLE = node.keys.private_key_readable
        regnet.PUBLIC_KEY_HASHED = node.keys.public_key_hashed
        regnet.ADDRESS = node.keys.address
        regnet.KEY = node.keys.key

    node.logger.app_log.warning(f"Status: Local address: {node.keys.address}")


def verify(db_handler):
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

        for row in db_handler.h.execute('SELECT * FROM transactions WHERE block_height > 1 and reward = 0 ORDER BY block_height'):  # native sql fx to keep compatibility

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
            sha_hash = SHA.new(str(db_transaction).encode("utf-8"))
            if verifier.verify(sha_hash, db_signature_dec):
                pass
            else:
                try:
                    if sha_hash.hexdigest() != db_hashes[db_block_height + "-" + db_timestamp]:
                        node.logger.app_log.warning("Signature validation problem: {} {}".format(db_block_height, db_transaction))
                        invalid = invalid + 1
                except:
                    node.logger.app_log.warning("Signature validation problem: {} {}".format(db_block_height, db_transaction))
                    invalid = invalid + 1

        if invalid == 0:
            node.logger.app_log.warning("All transacitons in the local ledger are valid")

    except Exception as e:
        node.logger.app_log.warning("Error: {}".format(e))
        raise


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

    mining_heavy3.mining_open()
    try:
        # create a plugin manager, load all plugin modules and init
        node.plugin_manager = plugins.PluginManager(app_log=node.logger.app_log, init=True)
        # get the potential extra command prefixes from plugin
        extra_commands = {}  # global var, used by the server part.
        extra_commands = node.plugin_manager.execute_filter_hook('extra_commands_prefixes', extra_commands)
        print("Extra prefixes: ", ",".join(extra_commands.keys()))                                        

        setup_net_type()
        load_keys()

        node.logger.app_log.warning(f"Status: Starting node version {VERSION}")
        node.startup_time = time.time()
        try:

            node.peers = peershandler.Peers(node.logger.app_log, config, node)

            # print(peers.peer_list_old_format())
            # sys.exit()

            node.apihandler = apihandler.ApiHandler(node.logger.app_log, config)
            mp.MEMPOOL = mp.Mempool(node.logger.app_log, config, node.db_lock, node.is_testnet)

            check_integrity(node.hyper_path)
            #PLACEHOLDER FOR FRESH HYPERBLOCK BUILDER

            # if node.rebuild_db: #does nothing
            #    db_maintenance(init_database)

            # db_manager = db_looper.DbManager(node.logger.app_log)
            # db_manager.start()

            db_handler_initial = dbhandler.DbHandler(node.index_db, node.ledger_path, node.hyper_path, node.ram, node.ledger_ram_file, node.logger)
            ledger_check_heights(node, db_handler_initial)
            ram_init(db_handler_initial)
            node_block_init(db_handler_initial)
            initial_db_check()


            if not node.is_regnet:
                sequencing_check(db_handler_initial)

            if node.verify:
                verify(db_handler_initial)

            #db_handler_initial.close_all()

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

            # hyperlane_manager = hyperlane.HyperlaneManager(node.logger.app_log)
            # hyperlane_manager.start()

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
