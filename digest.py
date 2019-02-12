import os
import sys
from decimal import *
import time
from quantizer import *
import base64
import essentials
import hashlib
import staking
from fork import *
import mining
import mining_heavy3
import regnet
import mempool as mp

from essentials import db_to_drive #rework
from essentials import checkpoint_set #rework
from essentials import ledger_balance3 #rework

from difficulty import *
POW_FORK, FORK_AHEAD, FORK_DIFF = fork()

from Cryptodome.Hash import SHA
from Cryptodome.PublicKey import RSA
from Cryptodome.Signature import PKCS1_v1_5


def digest_block(node, data, sdef, peer_ip, db_handler):
    """node param for imports"""
    block_height_new = node.last_block + 1  # for logging purposes.
    block_hash = 'N/A'
    failed_cause = ''
    block_count = 0
    tx_count = 0

    if node.peers.is_banned(peer_ip):
        # no need to loose any time with banned peers
        raise ValueError("Cannot accept blocks from a banned peer")
        # since we raise, it will also drop the connection, it's fine since he's banned.

    if not node.db_lock.locked():
        node.db_lock.acquire()

        while mp.MEMPOOL.lock.locked():
            time.sleep(0.1)
            node.logger.app_log.info(f"Chain: Waiting for mempool to unlock {peer_ip}")

        node.logger.app_log.warning(f"Chain: Digesting started from {peer_ip}")
        # variables that have been quantized are prefixed by q_ So we can avoid any unnecessary quantize again later. Takes time.
        # Variables that are only used as quantized decimal are quantized once and for all.

        block_size = Decimal(sys.getsizeof(str(data))) / Decimal(1000000)
        node.logger.app_log.warning(f"Chain: Block size: {block_size} MB")

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

                        db_handler.execute_param(db_handler.h3, "SELECT block_height FROM transactions WHERE signature = ?;",
                                                 (entry_signature,))
                        tx_presence_check = db_handler.h3.fetchone()
                        if tx_presence_check:
                            # print(node.last_block)
                            raise ValueError(f"That transaction {entry_signature[:10]} is already in our ram ledger, block_height {tx_presence_check[0]}")

                        db_handler.execute_param(db_handler.c, "SELECT block_height FROM transactions WHERE signature = ?;",
                                                 (entry_signature,))
                        tx_presence_check = db_handler.c.fetchone()
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
                db_handler.execute(db_handler.c,
                                   "SELECT block_hash, block_height, timestamp FROM transactions WHERE reward != 0 ORDER BY block_height DESC LIMIT 1;")
                result = db_handler.c.fetchall()
                db_block_hash = result[0][0]
                db_block_height = result[0][1]
                q_db_timestamp_last = quantize_two(result[0][2])
                block_height_new = db_block_height + 1
                node.last_block = block_height_new
                # previous block info
                start_time_block = quantize_two(time.time())
                transaction_list_converted = []  # makes sure all the data are properly converted
                for tx_index, transaction in enumerate(transaction_list):
                    start_time_tx = quantize_two(time.time())
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

                    # if (start_time_tx < q_received_timestamp + 432000) or not quicksync:

                    # convert readable key to instance
                    received_public_key = RSA.importKey(base64.b64decode(received_public_key_hashed))

                    received_signature_dec = base64.b64decode(received_signature_enc)
                    verifier = PKCS1_v1_5.new(received_public_key)

                    essentials.validate_pem(received_public_key_hashed)

                    sha_hash = SHA.new(str((received_timestamp, received_address, received_recipient, received_amount,
                                        received_operation, received_openfield)).encode("utf-8"))
                    if not verifier.verify(sha_hash, received_signature_dec):
                        raise ValueError(f"Invalid signature from {received_address}")
                    else:
                        node.logger.app_log.info(f"Valid signature from {received_address} to {received_recipient} amount {received_amount}")
                    if float(received_amount) < 0:
                        raise ValueError("Negative balance spend attempt")

                    if received_address != hashlib.sha224(base64.b64decode(received_public_key_hashed)).hexdigest():
                        raise ValueError("Attempt to spend from a wrong address")

                    if not essentials.address_validate(received_address):
                        raise ValueError("Not a valid sender address")

                    if not essentials.address_validate(received_recipient):
                        raise ValueError("Not a valid recipient address")

                    if start_time_tx < q_received_timestamp:
                        raise ValueError(f"Future transaction not allowed, timestamp {quantize_two((q_received_timestamp - start_time_tx) / 60)} minutes in the future")
                    if q_db_timestamp_last - 86400 > q_received_timestamp:
                        raise ValueError("Transaction older than 24h not allowed.")
                        # verify signatures
                        # else:
                        # print("hyp1")

                # reject blocks older than latest block
                if q_block_timestamp <= q_db_timestamp_last:
                    raise ValueError("Block is older than the previous one, will be rejected")

                # calculate current difficulty (is done for each block in block array, not super easy to isolate)
                diff = difficulty(node, db_handler)
                node.difficulty = diff

                node.logger.app_log.warning(f"Time to generate block {db_block_height + 1}: {'%.2f' % diff[2]}")
                node.logger.app_log.warning(f"Current difficulty: {diff[3]}")
                node.logger.app_log.warning(f"Current blocktime: {diff[4]}")
                node.logger.app_log.warning(f"Current hashrate: {diff[5]}")
                node.logger.app_log.warning(f"Difficulty adjustment: {diff[6]}")
                node.logger.app_log.warning(f"Difficulty: {diff[0]} {diff[1]}")

                # node.logger.app_log.info("Transaction list: {}".format(transaction_list_converted))
                block_hash = hashlib.sha224(
                    (str(transaction_list_converted) + db_block_hash).encode("utf-8")).hexdigest()
                # node.logger.app_log.info("Last block sha_hash: {}".format(db_block_hash))
                node.logger.app_log.info(f"Calculated block sha_hash: {block_hash}")
                # node.logger.app_log.info("Nonce: {}".format(nonce))

                # check if we already have the sha_hash
                db_handler.execute_param(db_handler.h3, "SELECT block_height FROM transactions WHERE block_hash = ?", (block_hash,))
                dummy = db_handler.h3.fetchone()
                if dummy:
                    raise ValueError(
                        "Skipping digestion of block {} from {}, because we already have it on block_height {}".
                            format(block_hash[:10], peer_ip, dummy[0]))

                if node.is_mainnet:
                    if block_height_new < POW_FORK:
                        diff_save = mining.check_block(block_height_new, miner_address, nonce, db_block_hash, diff[0],
                                                       received_timestamp, q_received_timestamp, q_db_timestamp_last,
                                                       peer_ip=peer_ip, app_log=node.logger.app_log)
                    else:
                        diff_save = mining_heavy3.check_block(block_height_new, miner_address, nonce, db_block_hash,
                                                              diff[0],
                                                              received_timestamp, q_received_timestamp,
                                                              q_db_timestamp_last,
                                                              peer_ip=peer_ip, app_log=node.logger.app_log)
                elif node.is_testnet:
                    diff_save = mining_heavy3.check_block(block_height_new, miner_address, nonce, db_block_hash,
                                                          diff[0],
                                                          received_timestamp, q_received_timestamp, q_db_timestamp_last,
                                                          peer_ip=peer_ip, app_log=node.logger.app_log)
                else:
                    # it's regnet then, will use a specific fake method here.
                    diff_save = mining_heavy3.check_block(block_height_new, miner_address, nonce, db_block_hash,
                                                          regnet.REGNET_DIFF,
                                                          received_timestamp, q_received_timestamp, q_db_timestamp_last,
                                                          peer_ip=peer_ip, app_log=node.logger.app_log)

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
                                    essentials.fee_calculate(db_openfield, db_operation,
                                                  node.last_block)))  # exclude the mining tx from fees

                    # print("block_fees_address", block_fees_address, "for", db_address)
                    # node.logger.app_log.info("Digest: Inbound block credit: " + str(block_credit))
                    # node.logger.app_log.info("Digest: Inbound block debit: " + str(block_debit))
                    # include the new block

                    # if (start_time_tx < q_received_timestamp + 432000) and not quicksync:
                    # balance_pre = quantize_eight(credit_ledger - debit_ledger - fees + rewards)  # without projection
                    balance_pre = ledger_balance3(db_address, balances, db_handler)  # keep this as c (ram hyperblock access)

                    # balance = quantize_eight(credit - debit - fees + rewards)
                    balance = quantize_eight(balance_pre - block_debit_address)
                    # node.logger.app_log.info("Digest: Projected transaction address balance: " + str(balance))
                    # else:
                    #    print("hyp2")

                    fee = essentials.fee_calculate(db_openfield, db_operation, node.last_block)

                    fees_block.append(quantize_eight(fee))
                    # node.logger.app_log.info("Fee: " + str(fee))

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
                    node.logger.app_log.info(f"Chain: Appending transaction back to block with {len(block_transactions)} transactions in it")
                    block_transactions.append((str(block_height_new), str(db_timestamp), str(db_address), str(db_recipient), str(db_amount),
                                               str(db_signature), str(db_public_key_hashed), str(block_hash), str(fee), str(reward),
                                               str(db_operation), str(db_openfield)))

                    try:
                        mp.MEMPOOL.delete_transaction(db_signature)
                        node.logger.app_log.info(
                            f"Chain: Removed processed transaction {db_signature[:56]} from the mempool while digesting")
                    except:
                        # tx was not or is no more in the local mempool
                        pass
                # end for transaction_list

                # save current diff (before the new block)
                db_handler.execute_param(db_handler.c, "INSERT INTO misc VALUES (?, ?)", (block_height_new, diff_save))
                db_handler.commit(db_handler.conn)

                # quantized vars have to be converted, since Decimal is not json serializable...
                node.plugin_manager.execute_action_hook('block',
                                                        {'height': block_height_new, 'diff': diff_save,
                                                         'sha_hash': block_hash, 'timestamp': float(q_block_timestamp),
                                                         'miner': miner_address, 'ip': peer_ip})

                node.plugin_manager.execute_action_hook('fullblock',
                                                        {'height': block_height_new, 'diff': diff_save,
                                                         'sha_hash': block_hash, 'timestamp': float(q_block_timestamp),
                                                         'miner': miner_address, 'ip': peer_ip,
                                                         'transactions': block_transactions})

                # do not use "transaction" as it masks upper level variable.

                db_handler.execute_many(db_handler.c, "INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", block_transactions)
                db_handler.commit(db_handler.conn)

                # savings
                if node.is_testnet or block_height_new >= 843000:
                    # no savings for regnet
                    if int(block_height_new) % 10000 == 0:  # every x blocks

                        staking.staking_update(db_handler.conn, db_handler.c, db_handler.index, db_handler.index_cursor,
                                               "normal", block_height_new, node.logger.app_log)
                        staking.staking_payout(db_handler.conn, db_handler.c, db_handler.index, db_handler.index_cursor,
                                               block_height_new, float(q_block_timestamp), node.logger.app_log)
                        staking.staking_revalidate(db_handler.conn, db_handler.c, db_handler.index, db_handler.index_cursor,
                                                   block_height_new, node.logger.app_log)

                # new sha_hash
                db_handler.execute(db_handler.c, "SELECT * FROM transactions WHERE block_height = (SELECT max(block_height) FROM transactions)")
                # Was trying to simplify, but it's the latest mirror sha_hash. not the latest block, nor the mirror of the latest block.
                # c.execute("SELECT * FROM transactions WHERE block_height = ?", (block_height_new -1,))
                tx_list_to_hash = db_handler.c.fetchall()
                mirror_hash = hashlib.blake2b(str(tx_list_to_hash).encode(), digest_size=20).hexdigest()
                # /new sha_hash

                # dev reward
                if int(block_height_new) % 10 == 0:  # every 10 blocks
                    db_handler.execute_param(db_handler.c, "INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                             (-block_height_new, str(q_block_timestamp), "Development Reward", str(node.genesis_conf),
                                              str(mining_reward), "0", "0", mirror_hash, "0", "0", "0", "0"))
                    db_handler.commit(db_handler.conn)

                    db_handler.execute_param(db_handler.c, "INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                             (-block_height_new, str(q_block_timestamp), "Hypernode Payouts",
                                              "3e08b5538a4509d9daa99e01ca5912cda3e98a7f79ca01248c2bde16",
                                              "8", "0", "0", mirror_hash, "0", "0", "0", "0"))
                    db_handler.commit(db_handler.conn)
                # /dev reward

                # node.logger.app_log.warning("Block: {}: {} valid and saved from {}".format(block_height_new, block_hash[:10], peer_ip))
                node.logger.app_log.warning(
                    f"Valid block: {block_height_new}: {block_hash[:10]} with {len(transaction_list)} txs, digestion from {peer_ip} completed in {str(time.time() - float(start_time_block))[:5]}s.")

                del block_transactions[:]
                node.peers.unban(peer_ip)

                # This new block may change the int(diff). Trigger the hook whether it changed or not.
                diff = difficulty(node, db_handler)
                node.difficulty = diff
                node.plugin_manager.execute_action_hook('diff', diff[0])
                # We could recalc diff after inserting block, and then only trigger the block hook, but I fear this would delay the new block event.

                # /whole block validation
                # NEW: returns new block sha_hash

            checkpoint_set(node, block_height_new)
            return block_hash

        except Exception as e:
            node.logger.app_log.warning(f"Chain processing failed: {e}")

            node.logger.app_log.info(f"Received data dump: {data}")

            failed_cause = str(e)
            # Temp

            exc_type, exc_obj, exc_tb = sys.exc_info()
            fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
            print(exc_type, fname, exc_tb.tb_lineno)

            if node.peers.warning(sdef, peer_ip, "Rejected block", 2):
                raise ValueError(f"{peer_ip} banned")
            raise ValueError("Chain: digestion aborted")

        finally:
            if node.full_ledger or node.ram_conf:
                # first case move stuff from hyper.db to ledger.db; second case move stuff from ram to both
                db_to_drive(node, db_handler)
            node.db_lock.release()
            delta_t = time.time() - float(start_time_tx)
            # node.logger.app_log.warning("Block: {}: {} digestion completed in {}s.".format(block_height_new,  block_hash[:10], delta_t))
            node.plugin_manager.execute_action_hook('digestblock',
                                                    {'failed': failed_cause, 'ip': peer_ip, 'deltat': delta_t,
                                                     "blocks": block_count, "txs": tx_count})

    else:
        node.logger.app_log.warning(f"Chain: Skipping processing from {peer_ip}, someone delivered data faster")
        node.plugin_manager.execute_action_hook('digestblock', {'failed': "skipped", 'ip': peer_ip})
