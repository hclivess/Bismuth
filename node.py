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


"""Bismuth full-node entry point and legacy P2P server.

This is the executable launched to run a node. It wires together the whole
process: it reads configuration (``options``), opens the ledger/hyperblock
databases, starts the mempool, the mining/consensus worker threads
(``connectionmanager`` -> ``worker``), the threaded legacy TCP P2P server
(``ThreadedTCPServer`` / ``ThreadedTCPRequestHandler``, which speaks the
socket protocol and serves/ingests blocks, balances, mempool and peer data
via ``apihandler``), and -- when ``rest_api`` is enabled -- the optional
read-only REST API. It also installs graceful-shutdown handling so the node
finishes any in-flight block write before exiting, keeping the ledger and
hyperblock heights consistent. Block ingestion itself is delegated to
``digest.digest_block``; this module is the orchestration and networking shell
around consensus.
"""


VERSION = "4.5.0.1"

import platform
import shutil
import signal
import socketserver
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

import amounts
import essentials
import mempool as mp
import mining_heavy3
import regnet
import validation_exceptions
from digest import digest_block
from chain_ops import recompress_ledger, ledger_check_heights, blocknf, check_integrity, sequencing_check, reconcile_ledger_hyper, rollback
from balances import balanceget
from node_init import setup_net_type, node_block_init, ram_init, initial_db_check, load_keys, add_indices
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
                            # reputation-weighted agreed tip (reduces to the plurality when peer
                            # reputations are uniform); validation still backstops whatever we sync to
                            block_req = node.peers.consensus_reputation_weighted
                            node.logger.app_log.warning("Reputation-weighted consensus block rule triggered")

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

                        send(self.request, amounts.display_row(block_last))
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
                                    "amount": amounts.display_amount(block_last[4]),
                                    "signature": block_last[5],
                                    "public_key": block_last[6],
                                    "block_hash": block_last[7],
                                    "fee": amounts.display_amount(block_last[8]),
                                    "reward": amounts.display_amount(block_last[9]),
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
                        # display-edge (doc/16 phase 2): reconstruct legacy decimal amounts when the
                        # ledger stores integer units (matches the blockgetjson sibling below).
                        block_desired_result = [amounts.display_row(r) for r in db_handler_instance.h.fetchall()]

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
                                        "amount": amounts.display_amount(transaction[4]),
                                        "signature": transaction[5],
                                        "public_key": transaction[6],
                                        "block_hash": transaction[7],
                                        "fee": amounts.display_amount(transaction[8]),
                                        "reward": amounts.display_amount(transaction[9]),
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

                        balanceget_result = balanceget(node, balance_address, db_handler_instance)

                        send(self.request,
                                         balanceget_result)  # return balance of the address to the client, including mempool
                        # send(self.request, balance_pre)  # return balance of the address to the client, no mempool
                    else:
                        node.logger.app_log.info("{peer_ip} not whitelisted for balanceget command")

                elif data == "balancegetjson":
                    # if (peer_ip in allowed or "any" in allowed):
                    if node.peers.is_allowed(peer_ip, data):
                        balance_address = receive(self.request)  # for which address

                        balanceget_result = balanceget(node, balance_address, db_handler_instance)
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

                        balanceget_result = balanceget(node, balance_address, db_handler_instance)[0]

                        send(self.request,
                                         balanceget_result)  # return balance of the address to the client, including mempool
                        # send(self.request, balance_pre)  # return balance of the address to the client, no mempool
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for balancegetjson command")

                elif data == "balancegethyperjson":
                    if node.peers.is_allowed(peer_ip, data):
                        balance_address = receive(self.request)  # for which address

                        balanceget_result = balanceget(node, balance_address, db_handler_instance)
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
                        result = [amounts.display_row(r) for r in db_handler_instance.h.fetchall()]
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
                                        "amount": amounts.display_amount(transaction[4]),
                                        "signature": transaction[5],
                                        "public_key": transaction[6],
                                        "block_hash": transaction[7],
                                        "fee": amounts.display_amount(transaction[8]),
                                        "reward": amounts.display_amount(transaction[9]),
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
                        result = [amounts.display_row(r) for r in db_handler_instance.h.fetchall()]
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
                        result = [amounts.display_row(r) for r in db_handler_instance.h.fetchall()]
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
                                        "amount": amounts.display_amount(transaction[4]),
                                        "signature": transaction[5],
                                        "public_key": transaction[6],
                                        "block_hash": transaction[7],
                                        "fee": amounts.display_amount(transaction[8]),
                                        "reward": amounts.display_amount(transaction[9]),
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
                        result = [amounts.display_row(r) for r in db_handler_instance.h.fetchall()]
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
                                        "amount": amounts.display_amount(transaction[4]),
                                        "signature": transaction[5],
                                        "public_key": transaction[6],
                                        "block_hash": transaction[7],
                                        "fee": amounts.display_amount(transaction[8]),
                                        "reward": amounts.display_amount(transaction[9]),
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
                        # SEAM (doc/26 stage 2): read the LMDB side-index when enabled, else index.db.
                        if node.token_index is not None:
                            result = node.token_index.aliasget(alias_address)
                        else:
                            result = db_handler_instance.aliasget(alias_address)

                        send(self.request, result)
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for aliasget command")

                elif data == "aliasesget":  # only gets the first one, for multiple addresses
                    if node.peers.is_allowed(peer_ip, data):
                        aliases.aliases_update(node, db_handler_instance)
                        aliases_request = receive(self.request)
                        if node.token_index is not None:
                            results = node.token_index.aliasesget(aliases_request)
                        else:
                            results = db_handler_instance.aliasesget(aliases_request)
                        send(self.request, results)
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for aliasesget command")

                # Not mandatory, but may help to reindex with minimal sql queries

                elif data == "tokensget":
                    # TODO: to be handled by token modules, with no sql here in node.
                    if node.peers.is_allowed(peer_ip, data):

                        tokens_address = receive(self.request)
                        # SEAM (doc/26 stage 2): the LMDB side-index when enabled, else the index.db SQL.
                        if node.token_index is not None:
                            ti = node.token_index
                            tokens_list = [(token, str(ti.token_balance(token, tokens_address)))
                                           for (token,) in ti.tokens_user(tokens_address)]
                        else:
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
                        if node.token_index is not None:
                            address_fetch = node.token_index.addfromalias(alias_address)
                        else:
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

                    # Modern plugins (doc/27): a class-based plugin may own this wire command (e.g. the
                    # tokens_aliases plugin's tokensget/aliasget/aliasesget/addfromalias). Pre-fork the core
                    # elif handlers above match first; post-fork, with those removed, this is where the
                    # plugin serves them — no token/alias command code left in the core loop.
                    plugin_cmd = node.plugin_manager.peer_command_handler(data)
                    if plugin_cmd is not None:
                        extra = True
                        plugin_cmd(data, self.request)

                    # This is the entry point for all extra commands from plugins (legacy filter hook)
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
        fork_height = getattr(node, "fork_height", None)   # fork-aware verification (doc/29 §6 bug 1)

        for row in db_handler.h.execute('SELECT * FROM transactions WHERE block_height > 0 and reward = 0 ORDER BY block_height'):  # native sql fx to keep compatibility

            db_block_height = str(row[0])
            db_timestamp = '%.2f' % (quantize_two(row[1]))
            db_address = str(row[2])[:56]
            db_recipient = str(row[3])[:56]
            # HARDFORK (doc/16): rebuild the consensus signing buffer in the frozen legacy '%.8f' form.
            # In integer-storage mode row[4] is atomic units (e.g. 250000000), so it MUST go through
            # from_units -> '2.50000000'; quantize_eight(250000000) would yield '250000000.00000000' and
            # fail EVERY signature. The hard fork that signs native integers deletes this branch.
            db_amount = amounts.from_units(row[4]) if amounts.LEDGER_INTEGER else '%.8f' % (quantize_eight(row[4]))
            db_signature_enc = str(row[5])[:essentials.MAX_TX_SIGNATURE_LEN]
            db_public_key_b64encoded = str(row[6])[:essentials.MAX_TX_PUBKEY_LEN]
            db_operation = str(row[10])[:30]
            db_openfield = str(row[11])  # no limit for backward compatibility
            db_transaction = str((db_timestamp, db_address, db_recipient, db_amount, db_operation, db_openfield)).encode("utf-8")
            # Fork-aware (doc/29 §6 bug 1): at/after fork_height an ordinary single-sig secp256k1 row is
            # verified by ecrecover over the content txid (public key dropped); pre-fork rows + post-fork
            # RSA/ED25519/multisig keep the legacy buffer+pubkey check. The old unconditional
            # verify_bis_signature flagged EVERY post-fork single-sig row as invalid.
            post_fork = fork_height is not None and int(row[0]) >= int(fork_height)

            try:
                # SignerFactory routes by scheme + fork: recoverable-over-txid for post-fork single-sig,
                # legacy buffer+pubkey otherwise — the same authority the digester/mempool use.
                SignerFactory.verify_tx_signature(post_fork, db_timestamp, db_address, db_recipient,
                                                  db_amount, db_operation, db_openfield,
                                                  db_signature_enc, db_public_key_b64encoded)
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


if __name__ == "__main__":
    # classes
    node = node.Node()
    node.logger = logger.Logger()
    node.keys = keys.Keys()

    # Graceful shutdown on SIGTERM/SIGINT: just raise the flag the main loop already honours — it waits for
    # db_lock (so an in-flight block finishes writing to BOTH ledger.db and hyper.db), closes Heavy3 and
    # exits. This stops `kill`/Ctrl-C from terminating mid-write and leaving the ledger/hyper heights split
    # (which is what tripped the cross-integrity rollback on the last restart).
    def _graceful_stop(signum, _frame):
        node.IS_STOPPING = True
    signal.signal(signal.SIGTERM, _graceful_stop)
    signal.signal(signal.SIGINT, _graceful_stop)

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
    node.log_color = config.log_color
    node.egress = config.egress
    node.genesis = config.genesis
    node.accept_peers = config.accept_peers
    node.full_ledger = config.full_ledger
    node.trace_db_calls = config.trace_db_calls
    node.heavy3_path = config.heavy3_path
    node.old_sqlite = config.old_sqlite
    node.heavy = config.heavy
    node.rollback_depth = config.rollback_depth  # max blocks the node will roll back to rejoin a longer chain
    node.rest_api = config.rest_api              # opt-in modern parallel REST API (see doc/15)
    # rest_api_port is env-overridable so co-located instances (the two-node API-sync harness) each get
    # their own REST port without separate config files.
    node.rest_api_port = int(os.environ.get("BISMUTH_REST_API_PORT", config.rest_api_port))
    node.rest_api_write = getattr(config, "rest_api_write", False)   # POST /api/transaction (post-hardfork submit path)
    node.rest_api_proxy = getattr(config, "rest_api_proxy", True)    # GET /api/proxy same-origin relay for the explorer (read-only, SSRF-guarded; on by default)
    node.rest_api_geo = getattr(config, "rest_api_geo", True)        # GET /api/stats/geo node-map geolocation lookup (ip-api.com, cached; on by default)
    node.rest_api_market = getattr(config, "rest_api_market", True)  # GET /api/stats/market price/market-cap lookup (coingecko, cached; on by default)
    # API-sync: catch up over a peer's REST API instead of the socket (doc/17). Off by default; env-overridable
    # for the harness. The consume loop (api_sync_worker) is started after the node is fully initialized.
    node.api_sync_enabled = os.environ.get("BISMUTH_API_SYNC", "").lower() in ("1", "true", "yes") \
        or getattr(config, "api_sync", False)
    node.api_sync_source = os.environ.get("BISMUTH_API_SYNC_SOURCE", "") or getattr(config, "api_sync_source", "")
    node.autoheal = getattr(config, "autoheal", True)                       # live tip sequence/dupe self-heal (no restart)
    node.autoheal_interval = int(os.environ.get("BISMUTH_AUTOHEAL_INTERVAL") or getattr(config, "autoheal_interval", 300))
    # doc/35 peer-difficulty divergence detector + guarded self-heal (#23). SAFE BY DEFAULT: detect + log only.
    # The prod node picks this up on its next restart; with these defaults it never pauses mining or rolls back.
    # pause/heal are OPT-IN. env-overridable so the regnet harness can flip them without a separate config file.
    def _envbool(name, default):
        v = os.environ.get(name)
        return v.lower() in ("1", "true", "yes") if v is not None else bool(default)
    node.diff_divergence_detect = _envbool("BISMUTH_DIFF_DIVERGENCE_DETECT", getattr(config, "diff_divergence_detect", True))
    node.diff_divergence_pause_mining = _envbool("BISMUTH_DIFF_DIVERGENCE_PAUSE_MINING", getattr(config, "diff_divergence_pause_mining", False))
    node.diff_divergence_autoheal = _envbool("BISMUTH_DIFF_DIVERGENCE_AUTOHEAL", getattr(config, "diff_divergence_autoheal", False))
    node.diff_divergence_interval = int(os.environ.get("BISMUTH_DIFF_DIVERGENCE_INTERVAL") or getattr(config, "diff_divergence_interval", 300))
    # doc/30 from-genesis sync: trust horizon. At/below it the per-item validation (signature, timestamp,
    # PoW, duplicate, overspend) is skipped, ANCHORED by the hardcoded block-hash checkpoints in
    # validation_exceptions.py (the node recomputes each block hash and must reproduce the canonical value
    # at every checkpoint it passes — a mismatch halts). Inert for a synced node (never re-digests these
    # heights) and for networks without checkpoints (regnet/testnet). 0 = off = full validation everywhere.
    node.assume_valid_height = int(os.environ.get("BISMUTH_ASSUME_VALID_HEIGHT")
                                   or getattr(config, "assume_valid_height", 4000000) or 0)
    # doc/30: optional external JSON of historical validation waivers (coin rescues / fork-edge edits),
    # merged over the in-source MAINNET_EXCEPTIONS. None when unset -> built-in mainnet registry is used.
    node.validation_exceptions_file = os.environ.get("BISMUTH_VALIDATION_EXCEPTIONS_FILE") \
        or getattr(config, "validation_exceptions_file", "")
    node.validation_exceptions = validation_exceptions.load(node)
    # doc/30: sync the chain from BLOCK 1 instead of bootstrapping from a snapshot. On a fresh ledger the
    # node seeds the canonical genesis block and skips the snapshot download, then catch-up builds 2..tip
    # (the trusted prefix below assume_valid_height is anchored by the checkpoints). No effect once synced.
    node.sync_from_genesis = os.environ.get("BISMUTH_SYNC_FROM_GENESIS", "").lower() in ("1", "true", "yes") \
        or getattr(config, "sync_from_genesis", False)
    # doc/30: optional checkpoint override (JSON {height: blockhash}) — used by tests/private chains to
    # anchor a trusted prefix; unset -> validation_exceptions.MAINNET_CHECKPOINTS (mainnet) is used.
    _cp_env = os.environ.get("BISMUTH_CHECKPOINTS")
    if _cp_env:
        import json as _json
        node.checkpoints = {int(k): str(v) for k, v in _json.loads(_cp_env).items()}
    node.rollback_consensus = config.rollback_consensus                      # AUTO-RECOVERY: reputation-gated deep rollback, ON by default (doc/14)
    node.rollback_consensus_threshold = config.rollback_consensus_threshold
    node.rollback_consensus_min_peers = config.rollback_consensus_min_peers
    node.rollback_consensus_min_reputable = config.rollback_consensus_min_reputable  # anti-sybil gate for deep rollback
    node.ledger_integer_amounts = config.ledger_integer_amounts   # doc/16 phase 2 cutover (default off)
    amounts.LEDGER_INTEGER = node.ledger_integer_amounts          # module flag read by every ledger amount site
    node.bootstrap_url = config.bootstrap_url     # configurable bootstrap source (the old fixed host can vanish)
    node.bootstrap_file = config.bootstrap_file   # local bootstrap archive; if set/present, used instead of downloading
    node.block_store_enabled = config.block_store # opt-in LMDB block-body mirror (doc/17 phase 7)
    node.block_store = None                        # the store object, created at startup if enabled
    node.block_writer = None                       # the stage-4 write seam over block_store (doc/26); set with it
    node.fork_signal = config.fork_signal          # hf2: stamp the readiness signal when mining (doc/18).
    # Signalling hf2 asserts readiness for the WHOLE bundle, including the blake2b Heavy3 (doc/18-D).
    node.mine = config.mine                        # opt-in built-in solo miner (miner.py)
    node.fork_window = config.fork_window          # hf2 signal window / boundary / burial
    node.fork_boundary = config.fork_boundary
    node.fork_bury = config.fork_bury
    # hf2 activation height (the ONE fork: serialization/rewards/LWMA/fees + blake2b PoW): once locked in
    # it is persisted (fork_lockin-<ledger>.json beside the ledger, namespaced per network — see
    # fork.lockin_path) and REPLAYED at startup. The actual load happens in setup_net_type(): only
    # there is node.ledger_path final (regnet/testnet override it after this point). None until then.
    node.fork_height = None
    node.base_fee = None                             # post-fork dynamic base fee (fee_dynamics), set per block
    node.fee_post_fork = False
    node.rpc_bitcoin = config.rpc_bitcoin            # opt-in bitcoind-compatible JSON-RPC (doc/17)
    node.rpc_bitcoin_port = config.rpc_bitcoin_port
    node.rpc_ethereum = config.rpc_ethereum          # opt-in eth_* compatibility shim (doc/17)
    node.rpc_ethereum_port = config.rpc_ethereum_port
    node.balance_index_enabled = config.balance_index  # opt-in O(1) display-balance index (doc/17)
    node.balance_index = None                          # the index object, built at startup if enabled
    node.balance_index_consensus = getattr(config, "balance_index_consensus", "off")  # doc/26 stage 4: off|shadow|primary
    node.txid_index_consensus = getattr(config, "txid_index_consensus", "off")  # doc/26 stage 4: off|shadow|primary (the dup-sig replay read off SQLite)
    node.txid_index = None                             # the LMDB txid->height projection, built at startup when txid_index_consensus != off
    node.parity_strict = getattr(config, "parity_strict", False)  # doc/26 stage 4: raise (not warn) on a parity mismatch
    node.vm_enabled = config.vm                         # opt-in decentralized-apps VM (doc/17); POST-FORK only
    node.vm_state = None                               # the contract state store, built at startup if enabled
    node.vm_state_root = None                          # committed VM state root (doc/19), maintained post-fork
    node.shield_enabled = config.shield                # opt-in shielded value (doc/22); POST-FORK only
    node.shielded_state = None                         # the note/nullifier sidecar, built at startup if enabled
    node.token_index = None                            # the LMDB token/alias side-index store; set by the
    #                                                    tokens_aliases plugin at startup (doc/27) when the
    #                                                    token_index flag is on, else None (legacy index.db).

    node.logger.app_log = log.log("node.log", node.debug_level, node.terminal_output, node.log_color)
    node.logger.app_log.warning("Configuration settings loaded")
    node.logger.app_log.warning(f"Python version: {node.py_version}")

    # upgrade wallet location after nuitka-required "files" folder introduction
    if os.path.exists("../wallet.der") and not os.path.exists("wallet.der") and "Windows" in platform.system():
        print("Upgrading wallet location")
        os.rename("../wallet.der", "wallet.der")
    # upgrade wallet location after nuitka-required "files" folder introduction

    # doc/30: from-genesis sync — seed the canonical genesis block into a FRESH ledger before the
    # hyperblock clone / integrity check run, so neither the clone (needs hyper.db to exist) nor
    # check_integrity/initial_db_check trigger a snapshot bootstrap. No-op once the ledger has blocks.
    if getattr(node, "sync_from_genesis", False):
        from chain_ops import seed_genesis
        seed_genesis(node)

    if not node.full_ledger and os.path.exists(node.ledger_path) and node.is_mainnet:
        os.remove(node.ledger_path)
        node.logger.app_log.warning("Removed full ledger for hyperblock mode")
    if not node.full_ledger:
        node.logger.app_log.warning("Cloning hyperblocks to ledger file")
        shutil.copy(node.hyper_path, node.ledger_path)  # hacked to remove all the endless checks
    try:
        # create a plugin manager, load all plugin modules and init
        node.plugin_manager = plugins.PluginManager(app_log=node.logger.app_log, config=config, init=True, node=node)
        # get the potential extra command prefixes from plugin
        extra_commands = {}  # global var, used by the server part.
        extra_commands = node.plugin_manager.execute_filter_hook('extra_commands_prefixes', extra_commands)
        print("Extra prefixes: ", ",".join(extra_commands.keys()))

        setup_net_type(node)
        load_keys(node)

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

            node.apihandler = apihandler.ApiHandler(node.logger.app_log, config, node=node)
            mp.MEMPOOL = mp.Mempool(node.logger.app_log, config, node.db_lock, node.is_testnet, trace_db_calls=node.trace_db_calls)

            check_integrity(node, node.hyper_path)
            #PLACEHOLDER FOR FRESH HYPERBLOCK BUILDER

            # if node.rebuild_db: #does nothing
            #    db_maintenance(init_database)

            # db_manager = db_looper.DbManager(node.logger.app_log)
            # db_manager.start()

            # Heal any ledger.db-vs-hyper.db tip split left by an unclean exit (OOM/power loss) BEFORE
            # anything reads heights or recompresses. Done with private connections (no DbHandler cache),
            # so it sees ground truth; if it trims, it does so while no other connection holds the files.
            reconcile_ledger_hyper(node)

            db_handler_initial = dbhandler.DbHandler(node.index_db, node.ledger_path, node.hyper_path, node.ram, node.ledger_ram_file, node.logger, trace_db_calls=node.trace_db_calls)

            ledger_check_heights(node, db_handler_initial)


            if node.recompress:
                #todo: do not close database and move files, swap tables instead
                db_handler_initial.close()
                recompress_ledger(node)
                db_handler_initial = dbhandler.DbHandler(node.index_db, node.ledger_path, node.hyper_path, node.ram, node.ledger_ram_file, node.logger, trace_db_calls=node.trace_db_calls)

            # Modern plugins (doc/27): start the class-based plugins now that the net type + ledger path are
            # finalized and the ledger is readable. The tokens_aliases plugin opens its isolated LMDB
            # token/alias side-index and registers it as the `token_index` service; expose it as
            # node.token_index so the read/rollback seams drive the plugin's store (and the core token/alias
            # write paths defer to it). Done BEFORE node_block_init so the legacy alias indexing there sees
            # node.token_index set and no-ops (no stray index.db writes when the plugin owns it).
            node.plugin_manager.start(node)
            node.token_index = node.plugin_manager.get_service("token_index")

            # doc/30 recovery: one-shot deep rollback. Drop a file `rollback_to` containing a height to
            # force a clean rollback of the chain + ALL derived indexes (difficulty/misc, balances, token
            # & alias indexes, VM/shielded state) to that tip, then resync from peers — rebuilds derived
            # state corrupted by a past event (e.g. a difficulty drift the recursive controller locked in).
            # Self-limiting: the trigger file is removed immediately, so it fires exactly once. Runs here,
            # after token_index is live (for the index rollback) but before ram_init / node_block_init read
            # the tip and before the balance/txid stores are built (they rebuild from the truncated ledger).
            if os.path.exists("rollback_to"):
                try:
                    _rbk_target = int(open("rollback_to").read().strip())
                finally:
                    os.remove("rollback_to")
                node.logger.app_log.warning(f"Status: rollback_to trigger -> deep rollback to height {_rbk_target}, then resync from peers")
                rollback(node, db_handler_initial, _rbk_target + 1)   # rollback_under drops >= target+1 -> new tip = target
                node.logger.app_log.warning(f"Status: rolled back to {_rbk_target}; derived state will rebuild and the chain resync")

            ram_init(node, db_handler_initial)
            node_block_init(node, db_handler_initial)
            initial_db_check(node)

            if not node.is_regnet:
                sequencing_check(node, db_handler_initial)

            if node.verify:
                verify(db_handler_initial)

            add_indices(node, db_handler_initial)

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

            # optional modern parallel REST API (read-only; see doc/15). Off unless rest_api=True.
            if node.rest_api:
                import rest_api
                node.rest_server = rest_api.BismuthRESTServer(node, port=node.rest_api_port)
                node.rest_server.start()

            # optional bitcoind-compatible JSON-RPC adapter (doc/17). Off unless rpc_bitcoin=True.
            if getattr(node, "rpc_bitcoin", False):
                try:
                    import rpc_bitcoin
                    node.rpc_bitcoin_server = rpc_bitcoin.BitcoinRPCServer(node, port=node.rpc_bitcoin_port)
                    node.rpc_bitcoin_server.start()
                except Exception as e:
                    node.logger.app_log.warning("Status: Bitcoin RPC could not start: {}".format(e))

            # optional eth_* compatibility shim (doc/17). Off unless rpc_ethereum=True.
            if getattr(node, "rpc_ethereum", False):
                try:
                    import rpc_ethereum
                    node.rpc_ethereum_server = rpc_ethereum.EthereumRPCServer(node, port=node.rpc_ethereum_port)
                    node.rpc_ethereum_server.start()
                except Exception as e:
                    node.logger.app_log.warning("Status: Ethereum RPC could not start: {}".format(e))

            # optional maintained O(1) balance index (doc/17). Off unless balance_index=True. Rebuilt
            # from the ledger at startup, maintained on commit, rolled back via chain_ops. DISPLAY path
            # ONLY — the consensus overspend check stays on ledger_balance3, so a stale/wrong index can
            # never enable spending (attack-vector safety).
            if getattr(node, "balance_index_enabled", False):
                try:
                    import os as _os
                    import balance_index as _bi_mod
                    bi_path = _os.path.join(_os.path.dirname(node.ledger_path) or ".", "balanceindex")
                    node.balance_index = _bi_mod.BalanceIndex(bi_path)
                    _bidb = dbhandler.DbHandler(node.index_db, node.ledger_path, node.hyper_path, node.ram,
                                                node.ledger_ram_file, node.logger,
                                                trace_db_calls=node.trace_db_calls)
                    try:
                        n = node.balance_index.rebuild_from_cursor(_bidb.c)
                        node.logger.app_log.warning("Status: balance index enabled, rebuilt {} addresses".format(n))
                    finally:
                        _bidb.close()
                except Exception as e:
                    node.logger.app_log.warning("Status: balance index could not start: {}".format(e))
                    node.balance_index = None

            # optional maintained txid -> height index (doc/26 stage 4). Off unless txid_index_consensus
            # != "off". Backs the duplicate-signature replay check (digest.check_duplicate_signatures) with
            # an O(1) content-txid lookup instead of the SQLite signature scan, and keys dedup on the txid
            # (audit M-3/M-4). POST-FORK only; rebuilt from the ledger at startup (empty pre-fork), maintained
            # on commit, rebuilt on a reorg. Like the balance index it never changes the dedup OUTCOME until
            # the flag flips to "primary" -- in "shadow" it only cross-checks the SQLite verdict.
            if getattr(node, "txid_index_consensus", "off") != "off":
                try:
                    import os as _os
                    import txid_index as _txi_mod
                    txi_path = _os.path.join(_os.path.dirname(node.ledger_path) or ".", "txidindex")
                    node.txid_index = _txi_mod.TxidIndex(txi_path)
                    _txidb = dbhandler.DbHandler(node.index_db, node.ledger_path, node.hyper_path, node.ram,
                                                 node.ledger_ram_file, node.logger,
                                                 trace_db_calls=node.trace_db_calls)
                    try:
                        n = node.txid_index.rebuild_from_cursor(_txidb.c, node.fork_height)
                        node.logger.app_log.warning("Status: txid index ({}), rebuilt {} post-fork txids".format(
                            node.txid_index_consensus, n))
                    finally:
                        _txidb.close()
                except Exception as e:
                    node.logger.app_log.warning("Status: txid index could not start: {}".format(e))
                    node.txid_index = None

            # optional decentralized-apps VM contract-state store (doc/17). Off unless vm=True. The store
            # persists on disk and the digester maintains it as it processes blocks (POST-FORK only) and
            # rebuilds it on a reorg, so no startup rescan is needed — just open it.
            if getattr(node, "vm_enabled", False):
                try:
                    import os as _os
                    import vm_state as _vm_state_mod
                    vm_path = _os.path.join(_os.path.dirname(node.ledger_path) or ".", "vmstate")
                    node.vm_state = _vm_state_mod.VMState(vm_path)
                    node.vm_state_root = node.vm_state.state_root()
                    node.logger.app_log.warning("Status: VM enabled (executes vm: txs post-fork)")
                except Exception as e:
                    node.logger.app_log.warning("Status: VM could not start: {}".format(e))
                    node.vm_state = None

            # optional shielded-value sidecar (doc/22). Off unless shield=True. A note/nullifier projection
            # of the chain's shield: txs, maintained by the digester POST-FORK and rolled back on a reorg,
            # namespaced per ledger (no regnet->mainnet bleed). Just open it; no startup rescan needed.
            if getattr(node, "shield_enabled", False):
                try:
                    import shieldedv1 as _shield_mod
                    node.shielded_state = _shield_mod.open_state_for(node.ledger_path)
                    node.logger.app_log.warning(
                        "Status: shielded value enabled (validates shield: txs post-fork): {}".format(
                            node.shielded_state.stats()))
                except Exception as e:
                    node.logger.app_log.warning("Status: shielded value could not start: {}".format(e))
                    node.shielded_state = None

            # (The LMDB token/alias side-index is now owned by the tokens_aliases PLUGIN — opened in
            # node.plugin_manager.start(node) above and exposed as node.token_index. doc/26 stage 2 + doc/27.)

            # optional LMDB block-body store mirror (doc/17 phase 7). Off unless block_store=True.
            # Additive shadow: the digester writes blocks here AFTER the normal commit; reads/consensus
            # are untouched, so the block hash and mining are unaffected.
            if node.block_store_enabled:
                try:
                    import os as _os
                    import block_store
                    bs_path = _os.path.join(_os.path.dirname(node.ledger_path) or ".", "blockstore")
                    node.block_store = block_store.BlockStore(bs_path)
                    import storage_backend as _sb
                    node.block_writer = _sb.LmdbWriteBackend(node.block_store)   # stage-4 write seam (doc/26)
                    node.logger.app_log.warning(
                        f"Status: block store enabled at {bs_path} (tip {node.block_store.tip()})")
                except Exception as e:
                    node.logger.app_log.warning(f"Status: block store could not start: {e}")
                    node.block_store = None
                    node.block_writer = None

        except Exception as e:
            node.logger.app_log.info(e)
            raise

    except Exception as e:
        node.logger.app_log.info(e)
        raise

    node.logger.app_log.warning("Status: Bismuth loop running.")

    # Built-in solo miner (miner.py): opt-in via mine=True. Runs in its own thread with its own DB handle
    # (SQLite handles are per-thread); it mines real Heavy3 blocks with mempool txs + the hf2 coinbase
    # and digests them, serialised with sync via db_lock. Inert unless mine=True.
    if getattr(node, "mine", False):
        def _solo_mine():
            _mdb = dbhandler.DbHandler(node.index_db, node.ledger_path, node.hyper_path, node.ram,
                                       node.ledger_ram_file, node.logger, trace_db_calls=node.trace_db_calls)
            import miner
            miner.mining_loop(node, _mdb)
        threading.Thread(target=_solo_mine, daemon=True, name="solo-miner").start()

    # API-sync consume loop (doc/17, the "API fork"): catch up the chain over a peer's REST API instead of
    # the socket. Off unless api_sync=True + api_sync_source set. Own thread + DB handle, serialised with the
    # rest via db_lock. Started here, after all stores are open and node.last_block is primed, so any block it
    # ingests maintains the same projections (block_store / balance_index / txid_index) as socket-delivered.
    if getattr(node, "api_sync_enabled", False):
        try:
            import api_sync_worker
            api_sync_worker.start(node)
        except Exception as e:
            node.logger.app_log.warning(f"Status: API-sync worker could not start: {e}")

    # Live self-heal (doc/14): periodically scan the recent tip for a sequence/dupe corruption and, if found,
    # roll back to the clean prefix + resync WITHOUT a restart. The check is a cheap recent-tail scan; it
    # only truncates on a genuine break (healthy chains have strictly-consecutive heights -> no-op). Runs in
    # its own thread + DB handle, serialised with digestion via a non-blocking db_lock acquire (skips while a
    # block is mid-digest). On by default; set autoheal=False to disable.
    if getattr(node, "autoheal", True):
        def _autoheal_loop():
            import chain_ops as _chain_ops
            try:
                _hdb = dbhandler.DbHandler(node.index_db, node.ledger_path, node.hyper_path, node.ram,
                                           node.ledger_ram_file, node.logger, trace_db_calls=node.trace_db_calls)
            except Exception as e:
                node.logger.app_log.warning(f"autoheal: could not open DB handle: {e}")
                return
            interval = max(30, int(getattr(node, "autoheal_interval", 300)))
            while not node.IS_STOPPING:
                time.sleep(interval)
                if node.IS_STOPPING:
                    break
                if not node.db_lock.acquire(blocking=False):
                    continue   # node is digesting a block right now; check next cycle
                try:
                    _chain_ops.autoheal_live(node, _hdb)
                except Exception as e:
                    node.logger.app_log.warning(f"autoheal loop: {e}")
                finally:
                    node.db_lock.release()
        threading.Thread(target=_autoheal_loop, daemon=True, name="autoheal").start()

    # doc/35 (#23): peer-difficulty DIVERGENCE detector + opt-in guarded self-heal. Prevents a silent
    # recurrence of the difficulty-corruption incident (node mined at 84.48 while the network was ~89.8).
    # SAFE BY DEFAULT — with the shipped config the prod node only polls peers every 5 min and LOUD-logs on
    # confirmed divergence; it NEVER pauses mining or rolls back unless the operator opts in. OBSERVE-ONLY:
    # reads the cached node.difficulty + an HTTP poll, NEVER scans the ledger. Own thread + DB handle (the DB
    # handle is only used as the detect_difficulty_divergence signature parameter — the detector does not
    # touch it; kept for template parity + future use). Modeled exactly on _autoheal_loop above.
    if getattr(node, "diff_divergence_detect", True):
        def _difficulty_divergence_loop():
            import chain_ops as _chain_ops
            try:
                _ddb = dbhandler.DbHandler(node.index_db, node.ledger_path, node.hyper_path, node.ram,
                                           node.ledger_ram_file, node.logger, trace_db_calls=node.trace_db_calls)
            except Exception as e:
                node.logger.app_log.warning(f"diff-divergence: could not open DB handle: {e}")
                _ddb = None
            interval = max(60, int(getattr(node, "diff_divergence_interval", 300)))
            # Positive startup confirmation (the detector is otherwise SILENT on a healthy CLEAN reading, so a
            # plain log can't distinguish "running + healthy" from "never started"). One line per boot.
            node.logger.app_log.warning(
                "Status: difficulty-divergence detector started [#23] — detect=%s pause_mining=%s autoheal=%s "
                "interval=%ss (first check after a %ss startup grace; acts only on a 3-cycle confirmed divergence)"
                % (getattr(node, "diff_divergence_detect", True), getattr(node, "diff_divergence_pause_mining", False),
                   getattr(node, "diff_divergence_autoheal", False), interval, interval))
            grace = time.time() + interval        # startup grace: let the tip + peers settle before judging
            confirm = 0                           # N_confirm debounce counter
            last_median = None
            last_tip = -1
            rest_port_cache = {}                  # ip -> rest_port, persists across cycles
            N_CONFIRM = 3
            while not node.IS_STOPPING:
                time.sleep(interval)
                if node.IS_STOPPING:
                    break
                try:
                    if time.time() < grace:
                        continue
                    tip = int(getattr(node, "last_block", 0) or 0)
                    # Skip while initial-syncing (we're not at the network tip, so our difficulty legitimately
                    # lags): require our tip within a small band of peer consensus.
                    consensus = getattr(node.peers, "consensus", None)
                    if consensus is not None and tip < int(consensus) - 5:
                        confirm = 0
                        continue
                    # Skip within ±FORK_WINDOW of the hf2 activation (the LWMA transition can legitimately
                    # diverge from the legacy controller). Inert pre-fork (fork_height is None).
                    fh = getattr(node, "fork_height", None)
                    if fh is not None and abs(tip - int(fh)) <= _chain_ops.DIFF_DIVERGENCE_FORK_WINDOW:
                        confirm = 0
                        continue

                    diverged, local, median, n = _chain_ops.detect_difficulty_divergence(
                        node, _ddb, node.peers, rest_port_cache=rest_port_cache)

                    if not diverged:
                        # CLEAN or ABSTAIN: reset debounce; clear an opt-in mining pause once we read clean.
                        confirm = 0
                        last_median = None
                        if n >= _chain_ops.DIFF_DIVERGENCE_MIN_PEERS:
                            # genuinely CLEAN (peers agree AND we match), not a thin-data ABSTAIN: restore the
                            # heal budget (so a corruption a heal actually fixed doesn't leave us advisory-only)
                            # and clear an opt-in mining pause.
                            _chain_ops.diffheal_note_clean(node)
                            if getattr(node, "mining_paused", False):
                                node.mining_paused = False
                                node.logger.app_log.warning(
                                    "Status: difficulty divergence CLEARED (peer-quorum agrees) — mining un-paused")
                        continue

                    # DIVERGED this cycle. Debounce: require N_CONFIRM consecutive at a non-decreasing tip
                    # with a stable median; any CLEAN/abstain resets (handled above).
                    median_stable = (last_median is None
                                     or abs(median - last_median) <= _chain_ops._diff_effective_threshold(
                                         local, _chain_ops.DIFF_DIVERGENCE_ABS_THRESHOLD))
                    if tip >= last_tip and median_stable:
                        confirm += 1
                    else:
                        confirm = 1
                    last_median = median
                    last_tip = tip

                    node.logger.app_log.warning(
                        f"Status: DIFFICULTY DIVERGENCE detected (confirm {confirm}/{N_CONFIRM}) — "
                        f"local={local} peer_median={median} samples={n} tip={tip}")

                    if confirm < N_CONFIRM:
                        continue

                    # CONFIRMED. ALWAYS loud-log + plugin alert (the safe default action).
                    node.logger.app_log.warning(
                        f"Status: DIFFICULTY DIVERGENCE CONFIRMED — local={local} vs peer_median={median} "
                        f"({n} height-matched peers). This node's cached difficulty disagrees with the network.")
                    try:
                        node.plugin_manager.execute_action_hook(
                            'diff_divergence',
                            {'local': local, 'median': median, 'samples': n, 'tip': tip})
                    except Exception:
                        pass

                    # OPT-IN: pause mining (a wrong local difficulty orphan-binds our blocks either way).
                    if getattr(node, "diff_divergence_pause_mining", False) and not node.mining_paused:
                        node.mining_paused = True
                        node.logger.app_log.warning(
                            "Status: difficulty divergence — MINING PAUSED (diff_divergence_pause_mining). "
                            "Auto-clears on the next CLEAN peer-quorum reading.")

                    # OPT-IN: guarded self-heal (cooldown / max-heals / bounded-depth / once-per-boot).
                    if getattr(node, "diff_divergence_autoheal", False):
                        state = _chain_ops.diffheal_state_read(node)
                        ok, reason = _chain_ops.diffheal_guards_ok(node, state)
                        if not ok:
                            node.logger.app_log.warning(
                                f"Status: difficulty-heal SUPPRESSED ({reason}) — staying advisory-only "
                                f"(NO rollback, NO restart). Manual intervention may be required.")
                        else:
                            target = _chain_ops.diffheal_target(node, state)
                            if target is None:
                                node.logger.app_log.warning(
                                    "Status: difficulty-heal target not permitted (depth/anti-sybil) — advisory-only")
                            elif _chain_ops.diffheal_arm(node, target):
                                node.logger.app_log.warning(
                                    "Status: difficulty-heal armed — requesting clean restart to apply rollback+resync")
                                node.IS_STOPPING = True   # graceful stop; systemd auto-restarts -> startup consumes rollback_to
                                break
                except Exception as e:
                    node.logger.app_log.warning(f"diff-divergence loop: {type(e).__name__}: {e}")
        threading.Thread(target=_difficulty_divergence_loop, daemon=True, name="diff-divergence").start()

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
