"""Block ingestion and validation -- the consensus core of the node.

``digest_block`` is the single entry point through which every candidate block
(whether mined locally or received from a peer) is admitted to the ledger. The
``BlockProcessor`` class enforces the consensus rules: it checks signatures and
rejects duplicates, validates the proof of work against the required
difficulty, sorts and validates the block's transactions, computes balances and
fees, applies the mining reward, writes the block atomically to the ledger and
hyperblock databases, removes spent transactions from the mempool and runs the
post-block hooks (e.g. alias/token/staking indexers). Errors trigger rollback
and cleanup. This module decides what is and isn't a valid block, so any change
here is consensus-affecting.
"""

import hashlib
import os
import sys
import time
import traceback
from decimal import Decimal
from typing import List, Any, Tuple

import amounts
import bismuth_serialize
import essentials
import mempool as mp
import mining_heavy3
from difficulty import difficulty
from essentials import checkpoint_set, ledger_balance3
from fork import Fork
import tokensv2 as tokens

# Block/tx data model (value objects + consensus quantizers) lives in digest_tx; this module keeps
# the BlockProcessor engine and the digest_block orchestration.
from digest_tx import quantize_two, quantize_eight, Transaction, MinerTransaction, Block

fork = Fork()


class BlockProcessor:
    """Handles the processing and validation of blocks."""

    def __init__(self, node, db_handler, peer_ip):
        self.node = node
        self.db_handler = db_handler
        self.peer_ip = peer_ip
        self.block_transactions = []

    def check_fork_reward(self, block_instance: Block) -> None:
        """Check and handle fork reward validation."""
        if self.node.is_testnet:
            if self.node.last_block > fork.POW_FORK_TESTNET:
                if not fork.check_postfork_reward_testnet(self.db_handler):
                    self.db_handler.rollback_under(fork.POW_FORK_TESTNET - 1)
                    raise ValueError("Rolling back chain due to old fork data")
        else:
            if self.node.last_block > fork.POW_FORK:
                if not fork.check_postfork_reward(self.db_handler):
                    print("Rolling back")
                    self.db_handler.rollback_under(fork.POW_FORK - 1)
                    raise ValueError("Rolling back chain due to old fork data")

    def check_duplicate_signatures(self, block: list, block_instance: Block) -> None:
        """Check for duplicate transactions in block and ledger."""
        signature_list = []

        for entry in block:
            entry_signature = entry[4]

            if not entry_signature:
                raise ValueError(f"Empty signature from {self.peer_ip}")

            signature_list.append(entry_signature)

            # Check if signature exists in main ledger
            if self._signature_exists_in_ledger(entry_signature, self.db_handler.h):
                raise ValueError(f"Transaction {entry_signature[:10]} already in ledger")

            # Check if signature exists in RAM ledger
            if self._signature_exists_in_ledger(entry_signature, self.db_handler.c):
                raise ValueError(f"Transaction {entry_signature[:10]} already in RAM ledger")

        # Check for duplicates within the block
        if block_instance.tx_count != len(set(signature_list)):
            raise ValueError("There are duplicate transactions in this block, rejected")

    def _signature_exists_in_ledger(self, signature: str, cursor) -> bool:
        """Check if a signature exists in the specified ledger."""
        if self.node.old_sqlite:
            self.db_handler.execute_param(
                cursor,
                "SELECT block_height FROM transactions WHERE signature = ?1;",
                (signature,)
            )
        else:
            self.db_handler.execute_param(
                cursor,
                "SELECT block_height FROM transactions WHERE substr(signature,1,4) = substr(?1,1,4) and signature = ?1;",
                (signature,)
            )
        return cursor.fetchone() is not None

    def sort_and_validate_transactions(self, block: list, block_instance: Block) -> MinerTransaction:
        """Sort and validate all transactions in a block."""
        miner_tx = None

        for tx_index, raw_transaction in enumerate(block):
            tx = Transaction()
            potential_miner_tx = tx.from_raw_transaction(raw_transaction, tx_index, block_instance.tx_count)

            if potential_miner_tx:
                miner_tx = potential_miner_tx

            # Check for token operations
            if tx.received_operation in ["token:issue", "token:transfer"]:
                block_instance.tokens_operation_present = True

            # Validate transaction
            tx.validate(self.node, self.node.last_block_timestamp)

            # Add to converted list
            block_instance.transaction_list_converted.append(tx.to_tuple())

            # Log validation success
            self.node.logger.app_log.info(
                f"Valid signature from {tx.received_address} to {tx.received_recipient} "
                f"amount {tx.received_amount}"
            )

        return miner_tx

    def calculate_mining_reward(self, block_instance: Block) -> Decimal:
        """Calculate the mining reward for the current block."""
        if self.node.is_testnet and self.node.last_block >= fork.POW_FORK_TESTNET:
            reward = 15 - (block_instance.block_height_new - fork.POW_FORK_TESTNET) / 1100000 - 9.5
        elif self.node.is_mainnet and self.node.last_block >= fork.POW_FORK:
            reward = 15 - (block_instance.block_height_new - fork.POW_FORK) / 1100000 - 9.5
        else:
            reward = 15 - (quantize_eight(block_instance.block_height_new) / quantize_eight(1000000 / 2)) - Decimal(
                "2.4")

        return max(reward, 0.5)

    def process_transaction_balances(self, block: list, block_instance: Block, miner_tx: MinerTransaction) -> List[
        Decimal]:
        """Process transactions and validate balances."""
        fees_block = []
        balances = {}  # Cache for multiple tx from same address

        # Calculate oldest possible transaction time
        if block_instance.block_height_new >= 1450000:
            oldest_possible_tx = miner_tx.q_block_timestamp - 60 * 60 * 2
        else:
            oldest_possible_tx = miner_tx.q_block_timestamp - 60 * 60 * 24

        for tx_index, transaction in enumerate(block):
            # Validate transaction age
            if float(transaction[0]) < oldest_possible_tx:
                raise ValueError(
                    f"txid {transaction[4][:56]} from {transaction[1]} is older ({transaction[0]}) "
                    f"than oldest possible date ({oldest_possible_tx})"
                )

            # Parse transaction fields
            # HARDFORK (doc/16): db_timestamp/db_amount are rebuilt into the frozen legacy '%.2f'/'%.8f'
            # string forms so the stored row matches what consensus signed/hashed. In integer-storage
            # mode the amount string is then re-parsed to units below (decimal -> string -> int, a
            # double conversion). The hard fork that signs native integers deletes this.
            db_timestamp = '%.2f' % quantize_two(transaction[0])
            db_address = str(transaction[1])[:56]
            db_recipient = str(transaction[2])[:56]
            db_amount = '%.8f' % quantize_eight(transaction[3])
            db_signature = str(transaction[4])[:684]
            db_public_key_b64encoded = str(transaction[5])[:1068]
            db_operation = str(transaction[6])[:30]
            db_openfield = str(transaction[7])[:100000]

            # Calculate block debits and fees for address
            block_debit_address, block_fees_address = self._calculate_address_totals(
                block, db_address, db_operation, db_openfield
            )

            # Process mining transaction
            if tx_index == block_instance.tx_count - 1:
                db_amount = 0  # Prevent spending from another address
                block_instance.mining_reward = self.calculate_mining_reward(block_instance)
                reward = '{:.8f}'.format(Decimal(block_instance.mining_reward) + sum(fees_block))
                fee = 0
            else:
                # Regular transaction
                reward = 0
                fee = essentials.fee_calculate(db_openfield, db_operation, self.node.last_block,
                                               base_fee=getattr(self.node, "base_fee", None),
                                               vm_surcharge=getattr(self.node, "fee_post_fork", False))
                fees_block.append(quantize_eight(fee))

                # Validate balance
                self._validate_balance(
                    db_address, db_amount, block_debit_address,
                    block_fees_address, balances
                )

            # Append to block transactions
            # storage form: amount/fee/reward as integer atomic units when enabled (the block hash
            # uses the separate decimal-string to_tuple, so it is unaffected). doc/16 phase 2.
            self.block_transactions.append((
                str(block_instance.block_height_new), str(db_timestamp), str(db_address),
                str(db_recipient),
                str(amounts.to_units(db_amount) if amounts.LEDGER_INTEGER else db_amount),
                str(db_signature), str(db_public_key_b64encoded), str(block_instance.block_hash),
                str(amounts.to_units(fee) if amounts.LEDGER_INTEGER else fee),
                str(amounts.to_units(reward) if amounts.LEDGER_INTEGER else reward),
                str(db_operation), str(db_openfield)
            ))

            # Remove from mempool if present
            self._remove_from_mempool(db_signature)

        return fees_block

    def _calculate_address_totals(self, block: list, address: str, operation: str, openfield: str) -> Tuple[
        Decimal, Decimal]:
        """Calculate total debits and fees for an address in the block."""
        block_debit_address = Decimal(0)
        block_fees_address = Decimal(0)

        for x in block:
            if x[1] == address:
                block_debit_address = quantize_eight(block_debit_address + Decimal(x[3]))

                # Exclude mining tx from fees
                if x != block[-1]:
                    fee = essentials.fee_calculate(x[7], x[6], self.node.last_block,
                                                   base_fee=getattr(self.node, "base_fee", None),
                                                   vm_surcharge=getattr(self.node, "fee_post_fork", False))
                    block_fees_address = quantize_eight(block_fees_address + Decimal(fee))

        return block_debit_address, block_fees_address

    def _validate_balance(self, address: str, amount: str, debit: Decimal, fees: Decimal, balances: dict) -> None:
        """Validate that an address has sufficient balance."""
        first_seen = address not in balances          # first appearance this block: comparable to balance_index
        balance_pre = ledger_balance3(address, balances, self.db_handler)
        balance = quantize_eight(balance_pre - debit)

        if quantize_eight(balance_pre) < quantize_eight(amount):
            raise ValueError(f"{address} sending more than owned: {amount}/{balance_pre}")

        if quantize_eight(balance) - quantize_eight(fees) < 0:
            raise ValueError(f"{address} Cannot afford to pay fees (balance: {balance}, block fees: {fees})")

        # Balance-read cross-check SHADOW (doc/26 stage 4). The overspend check above reads the authoritative
        # ledger_balance3 (a ledger scan) — the LAST consensus read still on SQLite and the gate for retiring
        # the SQLite write. Here we shadow the maintained LMDB balance_index against it to build the evidence
        # that the index is byte-faithful before that read could ever be flipped (a wrong index = inflation,
        # so this is the highest-risk migration). Compared only on an address's FIRST appearance in the block,
        # where the two line up: ledger_balance3 has no intra-block delta cached yet and balance_index reflects
        # the last committed block. LOG-ONLY — balance_index stays display-only and ledger_balance3 stays
        # authoritative, so a mismatch can never affect validation; it just flags an index bug to fix.
        if first_seen:
            self._balance_index_crosscheck(address, balance_pre)

    def _balance_index_crosscheck(self, address: str, authoritative: Decimal) -> None:
        node = self.node
        bi = getattr(node, "balance_index", None)
        fork_height = getattr(node, "fork_height", None)
        if bi is None or fork_height is None or (getattr(node, "last_block", 0) or 0) < fork_height:
            return
        try:
            indexed = bi.get_balance(address)
            if quantize_eight(indexed) != quantize_eight(authoritative):
                node.logger.app_log.warning(
                    f"balance seam: index {indexed} != ledger_balance3 {authoritative} for {address[:16]} "
                    f"at height {node.last_block} — investigate before trusting the LMDB balance read")
        except Exception as e:
            node.logger.app_log.warning(f"balance seam cross-check failed for {address[:16]}: {e}")

    def _remove_from_mempool(self, signature: str) -> None:
        """Remove processed transaction from mempool."""
        try:
            mp.MEMPOOL.delete_transaction(signature)
            self.node.logger.app_log.info(
                f"Chain: Removed processed transaction {signature[:56]} from the mempool while digesting"
            )
        except:
            pass  # Transaction not in local mempool

    def apply_rewards(self, block_instance: Block, miner_tx: MinerTransaction) -> None:
        """Apply dev and HN rewards if applicable."""
        if block_instance.block_height_new % 10 == 0 and block_instance.block_height_new < 4380000:
            self.db_handler.dev_reward(
                self.node, block_instance, miner_tx,
                block_instance.mining_reward, block_instance.mirror_hash
            )
            self.db_handler.hn_reward(
                self.node, block_instance, miner_tx,
                block_instance.mirror_hash
            )

    def verify_proof_of_work(self, block_instance: Block, miner_tx: MinerTransaction,
                             tx: Transaction, diff: tuple) -> Any:
        """Verify the proof of work for the block."""
        _ufh = getattr(self.node, "fork_height", None)
        new_pow = _ufh is not None and block_instance.block_height_new >= _ufh   # blake2b Heavy3 from hf2 (doc/18-D, bundled)
        if self.node.is_mainnet or self.node.is_testnet:
            return mining_heavy3.check_block(
                block_instance.block_height_new,
                miner_tx.miner_address,
                miner_tx.nonce,
                self.node.last_block_hash,
                diff[0],
                tx.received_timestamp,
                tx.q_received_timestamp,
                self.node.last_block_timestamp,
                peer_ip=self.peer_ip,
                app_log=self.node.logger.app_log,
                new_pow=new_pow
            )
        else:
            # Regnet
            import regnet
            return mining_heavy3.check_block(
                block_instance.block_height_new,
                miner_tx.miner_address,
                miner_tx.nonce,
                self.node.last_block_hash,
                regnet.REGNET_DIFF,
                tx.received_timestamp,
                tx.q_received_timestamp,
                self.node.last_block_timestamp,
                peer_ip=self.peer_ip,
                app_log=self.node.logger.app_log,
                new_pow=new_pow
            )


def digest_block(node, data, sdef, peer_ip, db_handler):
    """
    Main function to digest and validate incoming blocks.

    Args:
        node: Node instance containing blockchain state
        data: Block data to process
        sdef: Socket definition
        peer_ip: IP address of the peer sending the block
        db_handler: Database handler instance

    Returns:
        Last block hash on success

    Raises:
        ValueError: On validation failure
    """
    # Check if peer is banned
    if node.peers.is_banned(peer_ip):
        raise ValueError("Cannot accept blocks from a banned peer")

    # Acquire database lock
    if not node.db_lock.locked():
        node.db_lock.acquire()
        node.logger.app_log.warning("Database lock acquired")

        # Wait for mempool to unlock
        while mp.MEMPOOL.lock.locked():
            time.sleep(0.1)
            node.logger.app_log.info(f"Chain: Waiting for mempool to unlock {peer_ip}")

        node.logger.app_log.warning(f"Chain: Digesting started from {peer_ip}")

        # Log block size
        block_size = Decimal(sys.getsizeof(str(data))) / Decimal(1000000)
        node.logger.app_log.warning(f"Chain: Block size: {block_size} MB")

        try:
            # Process all blocks in the data
            processor = BlockProcessor(node, db_handler, peer_ip)
            last_block_hash = process_block_data(node, data, processor, db_handler, peer_ip)

            # "validate the height is real" (Bitcoin-style): the peer backed its claim with PoW-valid
            # block(s) — reward its reputation.
            node.peers.reward(peer_ip)
            checkpoint_set(node)
            return last_block_hash

        except Exception as e:
            # Penalize a peer for a bad block ONLY when WE are synced. While CATCHING UP, digest failures
            # (a peer sends a block we already have, or out-of-order delivery) are normal SYNC NOISE, not
            # misbehaviour — penalizing them bans every peer and isolates a freshly-restarted node (it did
            # exactly that). Also skip our own fork-rollback and benign "already in ledger" duplicates.
            msg = str(e)
            benign = ("Rolling back" in msg) or ("already in" in msg)
            try:
                consensus = getattr(node.peers, "consensus", None)
                synced = consensus is not None and int(node.hdd_block) >= int(consensus) - 5
            except Exception:
                synced = False
            if synced and not benign:
                import peers_reputation
                node.peers.penalize(peer_ip, peers_reputation.PENALTY_INVALID_BLOCK, "invalid block")
            handle_processing_error(node, db_handler, sdef, peer_ip, e)

        finally:
            cleanup_after_processing(node, db_handler, peer_ip)

    else:
        node.logger.app_log.warning(f"Chain: Skipping processing from {peer_ip}, someone delivered data faster")
        node.plugin_manager.execute_action_hook('digestblock', {'failed': "skipped", 'ip': peer_ip})


def process_block_data(node, data, processor, db_handler, peer_ip) -> str:
    """Process the block data and return the last block hash."""
    block_count = len(data)

    for block in data:
        if node.IS_STOPPING:
            node.logger.app_log.warning("Process_blocks aborted, node is stopping")
            return node.last_block_hash

        # hf2 activation height (ONE fork: serialization/rewards/LWMA/fees AND the blake2b PoW all gate
        # on it): derive ONCE from the on-chain signal, then cache + persist so the gated switches flip
        # deterministically network-wide. Runs BEFORE this block is validated, against CONFIRMED history
        # only (node.last_block): the rules an incoming block is judged under must derive from the chain
        # BELOW it. This ordering makes a lost sidecar self-healing — a node restarting on a chain
        # already past activation re-derives the height here before judging (or mining against)
        # anything, instead of wrongly validating the first post-restart block in the pre-fork era and
        # only then noticing. The activation height itself is unchanged by the ordering: it is a pure
        # function of which height closes the first all-signalled window in confirmed history. Hot-path
        # cost: on the node's FIRST digest we run one full forward scan (catches a resync/restart that
        # lands PAST a completed window); every block after that is the O(window) trailing check
        # (fork.lockin_at_tip) — cheap when unsignalled. The persisted sidecar (loaded at startup)
        # remains the authority across restarts when present.
        try:
            import fork as _fork
            if getattr(node, "fork_height", None) is None:
                _h = node.last_block
                _first_scan = not getattr(node, "_fork_caught_up", False)
                _rd = _fork.db_fork_signal_reader(db_handler)
                _w = getattr(node, "fork_window", _fork.FORK2_WINDOW)
                _b = getattr(node, "fork_boundary", _fork.FORK2_BOUNDARY)
                _u = getattr(node, "fork_bury", _fork.FORK2_BURY)
                _fh = (_fork.dynamic_fork_height(_rd, _h, _w, _b, _u) if _first_scan
                       else _fork.lockin_at_tip(_rd, _h, _w, _b, _u))
                if _fh is not None:
                    node.fork_height = _fh
                    _fork.save_locked_height(node.ledger_path, "hf2", _fh)
                    node.logger.app_log.warning(f"Status: hf2 activation height locked at {_fh}")
            node._fork_caught_up = True   # the one-time full forward scan is done; go incremental
        except Exception as e:
            # rare (a real bug, not the no-signal case which returns None) — but a SILENT failure here
            # means the fork never activates and the VM/LWMA gates stay inert with no explanation.
            node.logger.app_log.warning(f"fork-height detection failed: {type(e).__name__}: {e}")

        # Create block instance
        block_instance = Block(node)
        block_instance.tx_count = len(block)
        block_instance.block_count = block_count

        # Check fork reward
        processor.check_fork_reward(block_instance)

        # post-fork DYNAMIC base fee (fee_dynamics, doc/17): computed ONCE per block from recent demand;
        # the per-tx fee checks in sort_and_validate read node.base_fee. Pre-fork it's the static BASE_FEE,
        # so historical fees are unchanged. Deterministic (a pure function of the recent chain).
        _bff = getattr(node, "fork_height", None)
        node.fee_post_fork = _bff is not None and (node.last_block + 1) >= _bff
        if node.fee_post_fork:
            import fee_dynamics
            # congestion = block WEIGHT (tx count + openfield bytes // W_UNIT), so large RingCT/VM txs price
            # in their real footprint, not just count. Read from the post-fork BLOCK STORE (LMDB) — NO
            # SQLite post-fork. Deterministic, storage-mode-independent, and the store is rolled back with
            # the chain so the window is always canonical. No store -> empty window -> static base fee.
            _bs = getattr(node, "block_store", None)
            _weights = (_bs.recent_block_weights(node.last_block, fee_dynamics.WINDOW, fee_dynamics.W_UNIT)
                        if _bs is not None else [])
            node.base_fee = fee_dynamics.base_fee(essentials.BASE_FEE, _weights,
                                                  target=fee_dynamics.TARGET_WEIGHT)
        else:
            node.base_fee = essentials.BASE_FEE

        # Sort and validate transactions
        miner_tx = processor.sort_and_validate_transactions(block, block_instance)

        # Validate block timestamp
        if miner_tx.q_block_timestamp <= node.last_block_timestamp:
            raise ValueError(
                f"Block is older {miner_tx.q_block_timestamp} than the previous one "
                f"{node.last_block_timestamp}, will be rejected"
            )

        # Check for duplicate signatures
        processor.check_duplicate_signatures(block, block_instance)

        # Calculate difficulty
        diff = difficulty(node, db_handler)
        node.difficulty = diff
        log_difficulty_info(node, diff)

        # Calculate block hash
        block_instance.block_hash = bismuth_serialize.block_hash(
            block_instance.transaction_list_converted, node.last_block_hash
        )

        # Check if we already have this block
        if block_already_exists(db_handler, block_instance.block_hash, peer_ip):
            continue

        # Verify proof of work
        # Get last transaction for PoW verification
        last_tx = Transaction()
        last_tx.from_raw_transaction(block[-1], len(block) - 1, len(block))
        diff_save = processor.verify_proof_of_work(block_instance, miner_tx, last_tx, diff)

        # Process transaction balances
        processor.process_transaction_balances(block, block_instance, miner_tx)

        # Update node state
        node.last_block = block_instance.block_height_new
        node.last_block_hash = block_instance.block_hash

        # Execute plugin hooks
        execute_block_hooks(node, block_instance, miner_tx, diff_save, peer_ip, processor.block_transactions)

        # state-root ENFORCEMENT (doc/19): post-fork the coinbase COMMITS the pre-state VM root; if it
        # disagrees with ours, a VM has diverged -> REJECT the block BEFORE committing it (caught, not
        # silent). node.vm_state_root is still the pre-state here (this block's vm: txs run after to_db).
        _ev = getattr(node, "vm_state", None)
        _efh = getattr(node, "fork_height", None)
        if _ev is not None and _efh is not None and block_instance.block_height_new >= _efh:
            import vm_engine
            _claimed = None
            for _t in processor.block_transactions:
                try:
                    if _t[9] and float(_t[9]) != 0:                  # coinbase = the reward tx
                        _claimed = vm_engine.extract_state_root(_t[11])
                        break
                except (ValueError, TypeError):
                    continue
            _local = getattr(node, "vm_state_root", None)
            if _claimed is None:
                # MANDATORY post-fork: a coinbase with no committed root would otherwise bypass the check,
                # letting a miner hide a divergent VM. Reject it (upgraded miners always embed the root).
                raise ValueError(
                    f"post-fork coinbase at {block_instance.block_height_new} commits no VM state root "
                    f"(required since hf2 activation at {_efh})")
            if _claimed != _local:
                raise ValueError(
                    f"VM state-root mismatch at {block_instance.block_height_new}: coinbase "
                    f"{_claimed[:16]} != local {str(_local)[:16]}")

        # native multisig (doc/23 §2): a multisig SENDER is a post-fork-only base-layer address type. The
        # M-of-N threshold itself is verified per-tx in Transaction.validate (SignerMultisig factory route);
        # here we add the hf2 TIMING gate — before activation an upgraded node MUST NOT accept a multisig
        # spend that a pre-fork node rejects (chain-split safety), so reject any block carrying a multisig
        # SENDER at/below the fork height. Receiving INTO a multisig address is always fine (just an address).
        _mfh = getattr(node, "fork_height", None)
        # active at height >= fork_height, identical to the shield/VM gates above (which use >= _fh). Using
        # '<' (not '<=') so multisig activates at the SAME block as shield/VM, not one block later.
        if _mfh is None or block_instance.block_height_new < _mfh:
            from polysign.signerfactory import SignerFactory as _SF
            for _t in processor.block_transactions:
                if _SF.address_is_multisig(str(_t[2])):
                    raise ValueError(
                        f"multisig sender {str(_t[2])[:12]} at block {block_instance.block_height_new} "
                        f"requires hf2 activation (fork_height {_mfh})")

        # shielded value (doc/22): consensus-validate this block's shield: txs BEFORE committing — a block
        # that double-spends a nullifier, spends an unknown/already-spent note, fails an ownership proof,
        # or breaks value conservation RAISES here and is rejected (never written). POST-FORK only; inert
        # pre-fork exactly like vm:. The parsed ops are stashed to apply after to_db succeeds.
        _shield_parsed = []
        _sst = getattr(node, "shielded_state", None)
        _sfh = getattr(node, "fork_height", None)
        if _sst is not None and _sfh is not None and block_instance.block_height_new >= _sfh:
            import shieldedv1
            _shield_parsed = shieldedv1.validate_block(
                _sst, processor.block_transactions, block_instance.block_height_new)

        # Save to database
        db_handler.to_db(block_instance, diff_save, processor.block_transactions)

        # LMDB block-body write through the stage-4 write seam (doc/26): ONE store, ONE atomic txn — no
        # commit_marker/ATTACH. Today it runs AFTER the SQLite commit and is still additive (the SQLite
        # ledger remains the source of truth for the consensus reads, esp. the ledger_balance3 overspend
        # check), so it never affects the block hash, validation, or mining. It is the canonical-write
        # candidate: reorg-safe (rolled back with the ledger in chain_ops) and the foundation for retiring
        # the SQLite lockstep once the consensus reads migrate and this write is trust-verified.
        if getattr(node, "block_writer", None) is not None:
            try:
                node.block_writer.append_block(block_instance.block_height_new,
                                               block_instance.block_hash, processor.block_transactions)
            except Exception as e:
                node.logger.app_log.warning(
                    f"block store write (seam) failed at {block_instance.block_height_new}: {e}")

            # Continuous trust check (doc/26 stage 4): post-fork, verify the LMDB store's block-linkage matches
            # the hash consensus just committed. This is the prerequisite for ever making block_store.tip() /
            # block_hash the crash-recovery + last-block-linkage authority (and retiring the SQLite lockstep):
            # you prove the LMDB linkage tracks consensus every block before you trust it. LOG-ONLY — a
            # derived-store divergence must never halt consensus; a mismatch flags an LMDB write bug to fix
            # before the canonical flip. (The last-block-linkage SQLite read itself is only a startup/rollback
            # seed kept in memory during digestion, so there is no per-block linkage read to swap here.)
            _wfh = getattr(node, "fork_height", None)
            if _wfh is not None and block_instance.block_height_new >= _wfh:
                try:
                    _lmdb_hash = node.block_store.block_hash(block_instance.block_height_new)
                    if _lmdb_hash != block_instance.block_hash:
                        node.logger.app_log.warning(
                            f"storage seam: LMDB block hash {_lmdb_hash} != committed "
                            f"{block_instance.block_hash} at {block_instance.block_height_new} — investigate "
                            f"before trusting LMDB as canonical")
                except Exception as e:
                    node.logger.app_log.warning(
                        f"storage seam linkage check failed at {block_instance.block_height_new}: {e}")

        # Calculate mirror hash
        block_instance.mirror_hash = calculate_mirror_hash(db_handler)

        # Apply rewards
        processor.apply_rewards(block_instance, miner_tx)

        # (hf2 fork-height detection runs at the TOP of this loop, BEFORE the block is validated —
        # the rules a block is judged under must derive from the chain below it.)

        # Decentralized-apps VM (doc/17): execute this block's vm: transactions, POST-FORK ONLY, behind the
        # vm flag. Inert until the fork activates — it adds NO behaviour to the current chain. Failures are
        # isolated (a bad contract is a no-op), never breaking block digestion.
        _vms = getattr(node, "vm_state", None)
        _vfh = getattr(node, "fork_height", None)
        if _vms is not None and _vfh is not None and block_instance.block_height_new >= _vfh:
            try:
                import vm_engine
                # value custody (doc/19): execute the block's vm: txs; each TRANSFER queues a payout that
                # we settle as a consensus-generated ledger row FROM the vm_custody sink to the recipient
                # (the sink's balance mirrors the contracts' custody, kept in vm_state -> rolls back + is
                # rebuilt deterministically).
                payouts = vm_engine.apply_block_rows(_vms, processor.block_transactions)
                for _to, _amt in payouts:
                    db_handler.vm_payout(block_instance.block_height_new, miner_tx.q_block_timestamp,
                                         block_instance.mirror_hash, _to, _amt)
                # consensus-committable STATE ROOT (now covers contract custody balances too).
                node.vm_state_root = _vms.state_root()
            except Exception as e:
                node.logger.app_log.warning(
                    f"vm execution failed at {block_instance.block_height_new}: {e}")

        # shielded value (doc/22): commit this block's validated shield: ops to the sidecar AFTER to_db, and
        # settle each redeem as a consensus payout row SHIELD_SINK -> recipient (a negative-height mirror,
        # like vm payouts / dev rewards — picked up by the balance index below and rolled back with the
        # ledger). Placed BEFORE the balance-index maintenance so the payout mirror rows are indexed.
        if _shield_parsed:
            try:
                import shieldedv1
                for _to, _units in shieldedv1.apply_block(node.shielded_state, _shield_parsed):
                    db_handler.shield_payout(block_instance.block_height_new, miner_tx.q_block_timestamp,
                                             block_instance.mirror_hash, _to, _units)
            except Exception as e:
                node.logger.app_log.warning(
                    f"shielded apply failed at {block_instance.block_height_new}: {e}")

        # Optional maintained balance index (doc/17): apply this block's net effect — the positive txs plus
        # any negative-height "mirror" rows (concluded dev/HN rewards AND VM custody payouts, both written
        # above) — so the index stays bit-identical to ledger_balance3. DISPLAY path only: the overspend
        # check uses ledger_balance3, so a wrong/stale index can never enable spending.
        if getattr(node, "balance_index", None) is not None:
            try:
                node.balance_index.apply_rows(processor.block_transactions)
                h = block_instance.block_height_new
                db_handler.execute_param(db_handler.c,
                                         "SELECT * FROM transactions WHERE block_height = ?", (-h,))
                mirrors = db_handler.c.fetchall()
                if mirrors:
                    node.balance_index.apply_rows(mirrors)
            except Exception as e:
                node.logger.app_log.warning(
                    f"balance index maintain failed at {block_instance.block_height_new}: {e}")

        # Log success
        node.logger.app_log.warning(
            f"Valid block: {block_instance.block_height_new}: {block_instance.block_hash[:10]} "
            f"with {len(block)} txs, digestion from {peer_ip} completed in "
            f"{str(time.time() - float(block_instance.start_time_block))[:5]}s."
        )

        # Update tokens if necessary
        if block_instance.tokens_operation_present:
            tokens.tokens_update(node, db_handler)

        # Clear transactions and unban peer
        processor.block_transactions.clear()
        node.peers.unban(peer_ip)

        # Recalculate difficulty and trigger hook
        diff = difficulty(node, db_handler)
        node.difficulty = diff
        node.plugin_manager.execute_action_hook('diff', diff[0])

    return node.last_block_hash


def log_difficulty_info(node, diff: tuple) -> None:
    """Log difficulty-related information."""
    node.logger.app_log.warning(f"Time to generate block {node.last_block + 1}: {'%.2f' % diff[2]}")
    node.logger.app_log.warning(f"Current difficulty: {diff[3]}")
    node.logger.app_log.warning(f"Current blocktime: {diff[4]}")
    node.logger.app_log.warning(f"Current hashrate: {diff[5]}")
    node.logger.app_log.warning(f"Difficulty adjustment: {diff[6]}")
    node.logger.app_log.warning(f"Difficulty: {diff[0]} {diff[1]}")


def block_already_exists(db_handler, block_hash: str, peer_ip: str) -> bool:
    """Check if a block with the given hash already exists."""
    db_handler.execute_param(
        db_handler.h,
        "SELECT block_height FROM transactions WHERE block_hash = ?",
        (block_hash,)
    )
    existing = db_handler.h.fetchone()

    if existing:
        raise ValueError(
            f"Skipping digestion of block {block_hash[:10]} from {peer_ip}, "
            f"already have it on block_height {existing[0]}"
        )
    return False


def calculate_mirror_hash(db_handler) -> str:
    """Calculate the mirror hash for the latest block."""
    db_handler.execute(
        db_handler.c,
        "SELECT * FROM transactions WHERE block_height = (SELECT max(block_height) FROM transactions)"
    )
    tx_list_to_hash = db_handler.c.fetchall()
    return hashlib.blake2b(str(tx_list_to_hash).encode(), digest_size=20).hexdigest()


def execute_block_hooks(node, block_instance, miner_tx, diff_save, peer_ip, block_transactions):
    """Execute plugin hooks for the processed block."""
    node.plugin_manager.execute_action_hook('block', {
        'height': block_instance.block_height_new,
        'diff': diff_save,
        'hash': block_instance.block_hash,
        'timestamp': float(miner_tx.q_block_timestamp),
        'miner': miner_tx.miner_address,
        'ip': peer_ip
    })

    node.plugin_manager.execute_action_hook('fullblock', {
        'height': block_instance.block_height_new,
        'diff': diff_save,
        'hash': block_instance.block_hash,
        'timestamp': float(miner_tx.q_block_timestamp),
        'miner': miner_tx.miner_address,
        'ip': peer_ip,
        'transactions': block_transactions
    })

    # Modern class-based plugins (doc/27): drive their per-block derived-state maintenance. The
    # tokens_aliases plugin projects token:/alias= ops into its own LMDB store here — so post-fork the
    # node core itself carries no token/alias indexing logic.
    node.plugin_manager.dispatch_block(block_instance.block_height_new, block_transactions)


def handle_processing_error(node, db_handler, sdef, peer_ip, error):
    """Handle errors during block processing."""
    # Pinpoint the REAL failure site (deepest traceback frame) and keep the cause. The previous bare
    # `print(exc_type, file, lineno)` went to stdout (no timestamp/level), dropped the message, and
    # always reported digest.py's process_block_data call site instead of where the block actually
    # failed validation — making rejected-block diagnosis during sync impossible.
    _exc_type, _exc, exc_tb = sys.exc_info()
    frames = traceback.extract_tb(exc_tb)
    where = f"{os.path.basename(frames[-1].filename)}:{frames[-1].lineno}" if frames else "?"
    node.logger.app_log.warning(
        f"Chain: digestion from {peer_ip} failed at {where} - {type(error).__name__}: {error}")

    # Restore actual data from database (roll our view back to what is committed)
    node.last_block = db_handler.block_max_ram()['block_height']
    node.last_block_hash = db_handler.last_block_hash()
    node.logger.app_log.warning(
        f"Chain: fell back to block {node.last_block} after rejecting a block from {peer_ip}")

    # Ban peer if necessary
    if node.peers.warning(sdef, peer_ip, "Rejected block", 2):
        raise ValueError(f"{peer_ip} banned")

    raise ValueError("Chain: digestion aborted")


def cleanup_after_processing(node, db_handler, peer_ip):
    """Clean up after block processing."""
    db_handler.db_to_drive(node)
    node.db_lock.release()
    node.logger.app_log.warning("Database lock released")

    # Execute cleanup hook
    block_instance = Block(node)  # Create temporary instance for timing
    delta_t = time.time() - float(block_instance.start_time_block)

    node.plugin_manager.execute_action_hook('digestblock', {
        'failed': '',
        'ip': peer_ip,
        'deltat': delta_t,
        'blocks': 0,
        'txs': 0
    })