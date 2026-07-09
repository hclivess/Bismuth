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
import validation_exceptions
from difficulty import difficulty
from essentials import checkpoint_set, ledger_balance3, immature_coinbase
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
        """Reject a block that replays a transaction already in the CONFIRMED chain."""
        # ROOT FIX for the "stuck, not syncing" wedge: bound the replay check to the canonical chain this
        # block extends — heights [0, block_height_new - 1]. The node keeps two non-atomically-committed
        # SQLite stores (full ledger.db + hyper.db rollup), so a crash or a rollback can momentarily leave
        # one store with transaction rows ABOVE the committed tip. An UNBOUNDED replay check trusts those
        # non-canonical orphan rows and then rejects the peer's (valid) re-sync of that very height
        # ("Transaction ... already in ledger") — wedging sync indefinitely. Replay protection only ever
        # needs the confirmed chain; rows above the tip are not consensus and must be ignored. This makes a
        # ledger/hyper split HARMLESS to sync no matter how it arose, and is NOT a consensus change: a
        # healthy node has no rows above its tip, so the bound is a no-op there — it only changes the
        # (previously fatal) behaviour when local orphans exist.
        confirmed_tip = block_instance.block_height_new - 1
        signature_list = []
        txid_list = []

        # doc/26 stage 4: shadow/primary the dup-replay read on the LMDB txid index. Engages POST-FORK only
        # (pre-fork txs have no content txid and stay signature-deduped) and only when the index exists.
        node = self.node
        post_fork = (getattr(node, "fork_height", None) is not None
                     and block_instance.block_height_new >= node.fork_height)
        txi = getattr(node, "txid_index", None)
        txi_mode = getattr(node, "txid_index_consensus", "off")
        shadow_txid = post_fork and txi_mode != "off" and txi is not None

        n_tx = block_instance.tx_count
        for idx, entry in enumerate(block):
            entry_signature = entry[4]
            # hf2 §2.C: the coinbase (last tx) is sig-less post-fork. It is authorized by PoW + the reward
            # formula, and PoW (over the parent hash) makes it per-block unique, so it cannot be replayed —
            # skip the empty-signature reject AND the signature-replay checks for it. (Keep its empty slot in
            # signature_list so the in-block tx-count check stays correct; only the coinbase may be empty,
            # because a NON-coinbase empty signature is still rejected below.)
            is_coinbase = post_fork and (idx == n_tx - 1)

            # doc/41: the post-fork coinbase's signature slot now carries the PoW NONCE (not a signature)
            # and its public_key slot the state-root commitment. It is authorized by PoW + the reward
            # formula, so it is NEVER signature-verified or replay-checked — whether that slot is empty
            # (pre-doc/41 compact form) or holds the nonce. Handle it first; a NON-coinbase empty signature
            # is still rejected below. (Keep the coinbase slot in signature_list for the tx-count check.)
            if is_coinbase:
                signature_list.append(entry_signature)
                if shadow_txid:
                    txid_list.append(bismuth_serialize.tx_id_at(
                        block_instance.block_height_new, node.fork_height,
                        str(entry[0]), str(entry[1]), str(entry[2]), str(entry[3]),
                        str(entry[6]), str(entry[7])))
                continue

            if not entry_signature:
                raise ValueError(f"Empty signature from {self.peer_ip}")

            signature_list.append(entry_signature)

            # Authoritative replay verdict (signature, confirmed chain only) — computed BEFORE raising so
            # the shadow can compare without changing the (byte-identical) signature behaviour below.
            sig_in_h = self._signature_exists_in_ledger(entry_signature, self.db_handler.h, confirmed_tip)
            sig_in_c = self._signature_exists_in_ledger(entry_signature, self.db_handler.c, confirmed_tip)

            if shadow_txid:
                # The canonical content txid of this entry (== txid_index.txid_of of the stored row: both
                # funnel the amount through the same atomic-unit normalization inside tx_id_v2_s).
                txid = bismuth_serialize.tx_id_at(
                    block_instance.block_height_new, node.fork_height,
                    str(entry[0]), str(entry[1]), str(entry[2]), str(entry[3]), str(entry[6]), str(entry[7]))
                txid_list.append(txid)
                idx_seen = txi.contains(txid, confirmed_tip)
                sig_seen = sig_in_h or sig_in_c
                if idx_seen != sig_seen:
                    msg = (f"txid-index dedup parity mismatch at {block_instance.block_height_new}: "
                           f"txid {txid[:12]} index={idx_seen} sig={sig_seen}")
                    if txi_mode == "primary":
                        # index authoritative: a disagreement means a replay the SQLite read would miss,
                        # or a stale index — HALT (a wrong dedup either inflates or wedges). Never bypass.
                        raise ValueError(msg)
                    if getattr(node, "parity_strict", False):
                        raise AssertionError(msg)
                    node.logger.app_log.warning(msg)

            # Check if signature exists in main ledger (confirmed chain only). doc/30: a curated DUPLICATE
            # waiver lets a known historical replay (manual rescue re-broadcast) through during sync.
            _dup_h = block_instance.block_height_new
            if sig_in_h:
                if not validation_exceptions.is_exempt(node, _dup_h, validation_exceptions.DUPLICATE,
                                                       entry_signature):
                    raise ValueError(f"Transaction {entry_signature[:10]} already in ledger")
                validation_exceptions.note(node, _dup_h, validation_exceptions.DUPLICATE,
                                           f"replay {entry_signature[:12]} (ledger)")

            # Check if signature exists in RAM ledger (confirmed chain only)
            if sig_in_c:
                if not validation_exceptions.is_exempt(node, _dup_h, validation_exceptions.DUPLICATE,
                                                       entry_signature):
                    raise ValueError(f"Transaction {entry_signature[:10]} already in RAM ledger")
                validation_exceptions.note(node, _dup_h, validation_exceptions.DUPLICATE,
                                           f"replay {entry_signature[:12]} (ram)")

        # Check for duplicates within the block
        if block_instance.tx_count != len(set(signature_list)):
            if not validation_exceptions.is_exempt(node, block_instance.block_height_new,
                                                   validation_exceptions.DUPLICATE):
                raise ValueError("There are duplicate transactions in this block, rejected")
            validation_exceptions.note(node, block_instance.block_height_new,
                                       validation_exceptions.DUPLICATE, "in-block duplicate signatures")

        # doc/26 stage 4: the same in-block dedup, keyed on the content txid. Under malleability two entries
        # can share a txid while carrying different signatures (audit M-3/M-4) — the txid key catches that,
        # the signature key does not. In shadow it only flags the divergence; in primary the txid is the
        # authoritative dedup key.
        if shadow_txid and len(set(txid_list)) != len(set(signature_list)):
            msg = (f"txid-index in-block dedup parity mismatch at {block_instance.block_height_new}: "
                   f"{len(set(txid_list))} unique txids vs {len(set(signature_list))} unique sigs")
            if txi_mode == "primary":
                raise ValueError("There are duplicate transactions in this block (txid), rejected")
            if getattr(node, "parity_strict", False):
                raise AssertionError(msg)
            node.logger.app_log.warning(msg)

    def _signature_exists_in_ledger(self, signature: str, cursor, confirmed_tip: int) -> bool:
        """Does `signature` exist in the CONFIRMED chain (block_height in [0, confirmed_tip])? Rows above
        confirmed_tip are non-canonical (an orphan from a ledger/hyper split) and are deliberately ignored
        so they cannot poison replay protection and wedge sync."""
        if self.node.old_sqlite:
            self.db_handler.execute_param(
                cursor,
                "SELECT block_height FROM transactions WHERE signature = ?1 "
                "AND block_height >= 0 AND block_height <= ?2;",
                (signature, confirmed_tip)
            )
        else:
            self.db_handler.execute_param(
                cursor,
                "SELECT block_height FROM transactions WHERE substr(signature,1,4) = substr(?1,1,4) "
                "and signature = ?1 AND block_height >= 0 AND block_height <= ?2;",
                (signature, confirmed_tip)
            )
        return cursor.fetchone() is not None

    def sort_and_validate_transactions(self, block: list, block_instance: Block) -> MinerTransaction:
        """Sort and validate all transactions in a block."""
        miner_tx = None

        for tx_index, raw_transaction in enumerate(block):
            tx = Transaction()
            potential_miner_tx = tx.from_raw_transaction(raw_transaction, tx_index, block_instance.tx_count,
                                                          block_instance.block_height_new,
                                                          getattr(self.node, "fork_height", None))

            if potential_miner_tx:
                miner_tx = potential_miner_tx

            # Check for token operations
            if tx.received_operation in ["token:issue", "token:transfer"]:
                block_instance.tokens_operation_present = True

            # Validate transaction (pass the height it's being validated into, for the hf2 signature gate).
            # doc/30 from-genesis sync: below assume_valid_height skip the expensive sig re-verify; and a
            # curated SIGNATURE waiver (manual coin-rescue / fork-edge tx) lets a known-irregular tx through.
            height = block_instance.block_height_new
            _verify_sig = not validation_exceptions.assume_valid_skip_signature(self.node, height)
            # hf2 §2.C: the coinbase (last tx) is sig-less post-fork — flag it so verify_tx_signature
            # authorizes it by PoW + reward formula instead of a signature.
            _is_coinbase = (tx_index == block_instance.tx_count - 1)
            try:
                tx.validate(self.node, self.node.last_block_timestamp, height, verify_signature=_verify_sig,
                            is_coinbase=_is_coinbase)
            except ValueError:
                if not validation_exceptions.is_exempt(self.node, height, validation_exceptions.SIGNATURE,
                                                       tx.received_signature_enc):
                    raise
                validation_exceptions.note(
                    self.node, height, validation_exceptions.SIGNATURE,
                    "tx %s %s->%s" % (str(tx.received_signature_enc)[:12],
                                      tx.received_address, tx.received_recipient))

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
            db_signature = str(transaction[4])[:essentials.MAX_TX_SIGNATURE_LEN]
            db_public_key_b64encoded = str(transaction[5])[:essentials.MAX_TX_PUBKEY_LEN]
            # hf2: operation/openfield are uncapped post-fork (doc/41, generalized). Pre-fork they stay within
            # the legacy 30 / 100000 bounds. db_* feed the fee calc and the stored row, not the post-fork
            # consensus hash (which is built from the raw tuple).
            _post_fork = (getattr(self.node, "fork_height", None) is not None
                          and block_instance.block_height_new >= self.node.fork_height)
            db_operation = str(transaction[6]) if _post_fork else str(transaction[6])[:30]
            db_openfield = str(transaction[7]) if _post_fork else str(transaction[7])[:100000]

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
                # hf2 §2.C anti-inflation invariant (documented guard, not the anchor): with the coinbase
                # signature dropped, the reward FORMULA is the sole amount authority. The credited reward is
                # RECOMPUTED above (never read from the tx) and the wire amount is forced to 0 — assert it as
                # defense-in-depth so a future refactor cannot let a wire value inflate supply.
                if float(transaction[3]) != 0:
                    raise ValueError("coinbase wire amount must be 0 (got %s)" % str(transaction[3]))
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
        """Validate that an address has sufficient balance.

        Balance-read migration seam (doc/26 stage 4). The overspend check is the LAST consensus read still on
        SQLite (ledger_balance3, a ledger scan). The maintained LMDB balance_index BIT-MATCHES it in
        integer-units mode (exact, order-independent integer addition through the same to_decimal; mirror
        reward rows included), so it can become the authoritative read. Gated by node.balance_index_consensus,
        and engaged only on an address's FIRST appearance this block (where the two views coincide:
        ledger_balance3 has no intra-block delta cached yet and the index reflects the last committed block),
        post-fork, integer mode:
          off (default / mainnet): ledger_balance3 authoritative, NO index read -> byte-identical.
          shadow: compute the index too + compare; warn (raise under parity_strict) on a mismatch; keep SQLite.
          primary: the index is authoritative; still compute SQLite and RAISE on a mismatch (halt > inflate)."""
        node = self.node
        first_seen = address not in balances
        balance_pre = ledger_balance3(address, balances, self.db_handler)   # SQLite — the safe authoritative read

        mode = getattr(node, "balance_index_consensus", "off")
        bi = getattr(node, "balance_index", None)
        fh = getattr(node, "fork_height", None)
        if (mode != "off" and bi is not None and fh is not None and first_seen
                and (getattr(node, "last_block", 0) or 0) >= fh and amounts.LEDGER_INTEGER):
            strict = (mode == "primary") or getattr(node, "parity_strict", False)
            try:
                indexed = quantize_eight(bi.get_balance(address))
            except Exception as e:
                if strict:
                    raise
                node.logger.app_log.warning(f"balance seam read failed for {address[:16]}: {e}")
                indexed = None
            if indexed is not None and indexed != quantize_eight(balance_pre):
                msg = (f"balance seam: index {indexed} != ledger_balance3 {balance_pre} for {address[:16]} "
                       f"at height {getattr(node, 'last_block', 0)}")
                if strict:
                    raise ValueError(msg)
                node.logger.app_log.warning(msg + " — investigate before trusting the LMDB balance read")
            elif indexed is not None and mode == "primary":
                balance_pre = indexed                   # the index is now the authoritative starting balance

        # doc/30: the block being validated is node.last_block + 1 (last_block is only advanced after this
        # whole block's balances are processed). A curated OVERSPEND waiver lets a historical manual coin
        # rescue / fork-edge balance edit replay from genesis instead of halting the sync here.
        _ovr_h = (getattr(node, "last_block", 0) or 0) + 1
        _ovr_trusted = validation_exceptions.in_trusted_prefix(node, _ovr_h)
        # doc/42: post-fork a coinbase reward is unspendable until COINBASE_MATURITY blocks deep — carve the
        # immature reward slice out of the spendable balance (account-model maturity). Pre-fork: no carve-out
        # (immature = 0), so this is byte-identical below the fork.
        _fh_mat = getattr(node, "fork_height", None)
        _immature = (immature_coinbase(address, _ovr_h, self.db_handler.c,
                                       essentials.coinbase_maturity(getattr(node, "is_regnet", False)),
                                       node.logger.app_log)
                     if (_fh_mat is not None and _ovr_h >= _fh_mat) else Decimal(0))
        _spendable = quantize_eight(balance_pre - _immature)
        # Spendable balance remaining after this address's cumulative block debit. BOTH the overspend check
        # (below) and the fee-affordability check must measure against _spendable, so immature coinbase reward
        # can be spent as neither a transfer NOR a fee before maturity (doc/42). This mirrors mempool
        # admission (mempool.py), which subtracts the immature slice from both balances. Pre-fork _immature
        # is 0, so `balance` == balance_pre - debit exactly as before — byte-identical below the fork.
        balance = quantize_eight(_spendable - debit)
        # Compare against the CUMULATIVE per-address block debit, not the single-tx `amount`. `debit`
        # (block_debit_address) is the address's total spend across the whole block and is identical for each
        # of its txs; `amount` is just this one tx. Using `amount` let a holder of S mature + I immature
        # coinbase split a spend into N txs each <= S — every tx individually passes a per-tx `amount` check
        # while the cumulative no-overspend check (line below) only caps total at mature+immature — draining
        # immature coinbase before maturity. Mirroring the cumulative `debit` here closes that split bypass
        # (consensus audit, doc/47).
        if _spendable < quantize_eight(debit) and not _ovr_trusted:
            if not validation_exceptions.is_exempt(node, _ovr_h, validation_exceptions.OVERSPEND):
                raise ValueError(f"{address} sending more than spendable: {debit}/{_spendable} "
                                 f"(balance {balance_pre}, immature coinbase {_immature})")
            validation_exceptions.note(node, _ovr_h, validation_exceptions.OVERSPEND,
                                       f"{address} {amount}/{_spendable} immature {_immature}")

        if quantize_eight(balance) - quantize_eight(fees) < 0 and not _ovr_trusted:
            if not validation_exceptions.is_exempt(node, _ovr_h, validation_exceptions.OVERSPEND):
                raise ValueError(f"{address} Cannot afford to pay fees (balance: {balance}, block fees: {fees})")
            validation_exceptions.note(node, _ovr_h, validation_exceptions.OVERSPEND,
                                       f"{address} fee shortfall (balance {balance}, fees {fees})")

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
        # doc/41: post-fork the PoW nonce lives in the coinbase signature slot (nonce_v2); pre-fork it
        # rides in the openfield (nonce). Same gate as new_pow, so they switch together.
        nonce = miner_tx.nonce_v2 if new_pow else miner_tx.nonce
        if self.node.is_mainnet or self.node.is_testnet:
            return mining_heavy3.check_block(
                block_instance.block_height_new,
                miner_tx.miner_address,
                nonce,
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
                nonce,
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
        # Keep the mempool's fork view in sync so it admits post-fork-signed txs (recoverable, no pubkey)
        # and rejects pre-fork-signed ones aimed at a post-fork block. The digester stays the authority.
        try:
            if mp.MEMPOOL is not None:
                mp.MEMPOOL.fork_height = node.fork_height
                mp.MEMPOOL.pk_registry = getattr(node, "pk_registry", None)   # doc/40 pubkey-by-reference
        except Exception:
            pass

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
            # SQLite post-fork. The base_fee is CONSENSUS (it sets the per-tx required fee and the coinbase
            # reward), so it MUST be identical on every node. FAIL CLOSED: the store is mandatory post-fork
            # (opened unconditionally in node.py) and the weight window must be fully present, else this node
            # HALTS instead of silently falling back to a divergent static fee / a short-window average — the
            # consensus split this guards against. A missing store or a window gap is an operator/env fault
            # (re-sync or enable the block store before hf2), never a silent alternate fee schedule.
            _bs = getattr(node, "block_store", None)
            if _bs is None:
                raise ValueError(
                    "post-fork dynamic fee requires the block store, but it is not open — it is mandatory "
                    "consensus since hf2 activation (fee window source). Fix/enable the block store and restart.")
            # strict=True: raise on any missing height in the window rather than skipping it (a skipped height
            # would make this node average over fewer blocks than its peers -> a different fee -> a fork).
            _weights = _bs.recent_block_weights(node.last_block, fee_dynamics.WINDOW, fee_dynamics.W_UNIT,
                                                strict=True)
            node.base_fee = fee_dynamics.base_fee(essentials.BASE_FEE, _weights,
                                                  target=fee_dynamics.TARGET_WEIGHT)
        else:
            node.base_fee = essentials.BASE_FEE

        # Mirror the consensus fee inputs to the mempool so ADMISSION computes the same fee this block's
        # apply will require (closes the validate/apply fee desync that otherwise wedges block production —
        # a tx admitted at the static fee but rejected at the dynamic fee stays stuck in the mempool). The
        # demand-scaled base_fee can lag admission by at most one block; the VM surcharge flag is exact.
        try:
            if mp.MEMPOOL is not None:
                mp.MEMPOOL.base_fee = node.base_fee
                mp.MEMPOOL.fee_post_fork = node.fee_post_fork
        except Exception:
            pass

        # Sort and validate transactions
        miner_tx = processor.sort_and_validate_transactions(block, block_instance)

        # Validate block timestamp. doc/30: skipped in the trusted prefix (early sub-0.01s granularity)
        # and waivable per-height; otherwise a block older than the previous one is rejected.
        _trusted = validation_exceptions.in_trusted_prefix(node, block_instance.block_height_new)
        if miner_tx.q_block_timestamp <= node.last_block_timestamp and not _trusted:
            if not validation_exceptions.is_exempt(node, block_instance.block_height_new,
                                                   validation_exceptions.TIMESTAMP):
                raise ValueError(
                    f"Block is older {miner_tx.q_block_timestamp} than the previous one "
                    f"{node.last_block_timestamp}, will be rejected"
                )
            validation_exceptions.note(node, block_instance.block_height_new, validation_exceptions.TIMESTAMP,
                                       f"block ts {miner_tx.q_block_timestamp} <= prev {node.last_block_timestamp}")

        # Check for duplicate signatures (skipped in the trusted prefix — the checkpoint anchors integrity)
        if not _trusted:
            processor.check_duplicate_signatures(block, block_instance)

        # Calculate difficulty
        diff = difficulty(node, db_handler)
        node.difficulty = diff
        log_difficulty_info(node, diff)

        # Calculate block hash — fork-aware (doc/29): at/after node.fork_height the binary/integer
        # block_hash_v2 (blake2b-256); below it the frozen legacy sha224. Gated by the destination height.
        block_instance.block_hash = bismuth_serialize.block_hash_at(
            block_instance.block_height_new, getattr(node, "fork_height", None),
            block_instance.transaction_list_converted, node.last_block_hash
        )

        # doc/30 from-genesis anchor: at a checkpoint height the COMPUTED block hash must reproduce the
        # hardcoded canonical value. Because the block hash chains the previous one, this pins the ENTIRE
        # prefix below it — making it safe to have skipped per-item validation in the trusted prefix. Always
        # on; inert for a synced node (never re-digests these historical heights), fires only on catch-up.
        validation_exceptions.verify_checkpoint(node, block_instance.block_height_new, block_instance.block_hash)

        # Check if we already have this block
        if block_already_exists(db_handler, block_instance.block_hash, peer_ip):
            continue

        # Verify proof of work
        # Get last transaction for PoW verification
        last_tx = Transaction()
        last_tx.from_raw_transaction(block[-1], len(block) - 1, len(block),
                                     block_instance.block_height_new, getattr(node, "fork_height", None))
        # doc/30: in the trusted prefix skip the memory-hard PoW re-verify entirely (the checkpoint anchors
        # integrity); record the computed required difficulty. Outside it, verify PoW — a curated POW waiver
        # still lets a specific historical manually-inserted block through. diff[0] is the required difficulty.
        if _trusted:
            diff_save = diff[0]
        else:
            try:
                diff_save = processor.verify_proof_of_work(block_instance, miner_tx, last_tx, diff)
            except ValueError:
                if not validation_exceptions.is_exempt(node, block_instance.block_height_new,
                                                       validation_exceptions.POW):
                    raise
                diff_save = diff[0]
                validation_exceptions.note(node, block_instance.block_height_new, validation_exceptions.POW,
                                           f"recorded diff {diff_save}")

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
        import fork as _fork
        if _fork.vm_active(node, block_instance.block_height_new):
            # The VM is mandatory consensus once ACTIVATED (fork.vm_active, gated on VM_ACTIVATION_HEIGHT —
            # DECOUPLED from hf2; disabled while VM_ACTIVATION_HEIGHT is None). If the store failed to open at
            # startup, FAIL CLOSED: halt here rather than skip the check and silently fork onto lenient rules
            # (the split this raise prevents was the whole reason vm_state is opened unconditionally in
            # node.py). digest_block catches this, rolls back, and the block is not committed.
            if _ev is None:
                raise ValueError(
                    f"block at {block_instance.block_height_new} requires the VM state store, but it is not "
                    f"open — the VM is mandatory consensus since its activation at {_fork.VM_ACTIVATION_HEIGHT}. "
                    f"Fix the VM store (see 'VM state store could not open' at startup) and restart.")
            import vm_engine
            _claimed = None
            for _t in processor.block_transactions:
                try:
                    if _t[9] and float(_t[9]) != 0:                  # coinbase = the reward tx
                        # doc/41: post-fork the state-root commitment rides in the public_key slot (_t[6]),
                        # not the openfield (this whole branch is already gated to post-fork heights).
                        _claimed = vm_engine.extract_state_root(_t[6])
                        break
                except (ValueError, TypeError):
                    continue
            _local = getattr(node, "vm_state_root", None)
            if _claimed is None:
                # MANDATORY once VM-active: a coinbase with no committed root would otherwise bypass the
                # check, letting a miner hide a divergent VM. Reject it (upgraded miners always embed the root).
                raise ValueError(
                    f"coinbase at {block_instance.block_height_new} commits no VM state root "
                    f"(required since VM activation at {_fork.VM_ACTIVATION_HEIGHT})")
            if _claimed != _local:
                raise ValueError(
                    f"VM state-root mismatch at {block_instance.block_height_new}: coinbase "
                    f"{_claimed[:16]} != local {str(_local)[:16]}")

        # post-fork-only SENDER types (native multisig doc/23 §2; ML-DSA / secp256r1 doc/20): base-layer
        # address types a pre-fork node does not understand. The per-tx signature itself is verified in
        # Transaction.validate (SignerFactory route); here we add the hf2 TIMING gate — before activation an
        # upgraded node MUST NOT accept a spend FROM one of these that a pre-fork node rejects (chain-split
        # safety), so reject any block carrying such a SENDER at/below the fork height. Receiving INTO one of
        # these addresses is always fine (just an address). _t[2] is the sender (12-field converted row).
        _mfh = getattr(node, "fork_height", None)
        # active at height >= fork_height, identical to the VM gate (which uses >= node.fork_height). Using '<'
        # (not '<=') so these activate at the SAME block as the VM gate, not one block later.
        if _mfh is None or block_instance.block_height_new < _mfh:
            from polysign.signerfactory import SignerFactory as _SF
            for _t in processor.block_transactions:
                if _SF.address_is_post_fork_sender_type(str(_t[2])):
                    raise ValueError(
                        f"post-fork-only sender type {str(_t[2])[:12]} at block "
                        f"{block_instance.block_height_new} requires hf2 activation (fork_height {_mfh})")

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

        # Decentralized-apps VM (doc/17): execute this block's vm: transactions, ONLY once the VM is ACTIVATED
        # (fork.vm_active — gated on VM_ACTIVATION_HEIGHT, DECOUPLED from hf2; disabled while it is None). Inert
        # until activation — it adds NO behaviour to the chain, `vm:` txs are stored as inert data. Failures are
        # isolated (a bad contract is a no-op), never breaking block digestion.
        _vms = getattr(node, "vm_state", None)
        if _vms is not None and _fork.vm_active(node, block_instance.block_height_new):
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
                # consensus-committable STATE ROOT (covers code + storage + custody). doc/45 Stage 2b: the
                # committed root is the MERKLE root (vm_merkle), over the SAME entries as the flat root, so a
                # light-client / zk verifier can prove a single locked entry against exactly what consensus
                # commits. Post-fork only (this branch is already hf2-gated) — rides the one fork, no new signal.
                node.vm_state_root = _vms.merkle_root()
            except Exception as e:
                # A VM apply failure post-fork is NOT safe to swallow: block N is already committed (to_db +
                # apply_rewards above) but node.vm_state_root would be left at the pre-state, so block N+1's
                # mandatory coinbase state-root check would mismatch and wedge the node forever
                # (handle_processing_error's ledger>working heal does not fire here — the working store is
                # legitimately ahead mid-batch). Individual contract faults are already isolated inside
                # apply_block_rows (a bad contract is a no-op); reaching here is an infrastructure failure. Roll
                # block N back so the ledger and vm_state unwind in lockstep (chain_ops.rollback rebuilds
                # vm_state from the ledger at N-1), then re-raise so digestion aborts and the block is
                # re-fetched. Fail-closed: a persistent failure keeps rejecting rather than silently diverging.
                node.logger.app_log.error(
                    f"vm execution failed at {block_instance.block_height_new}: {e} — rolling the block back")
                try:
                    import chain_ops
                    chain_ops.rollback(node, db_handler, block_instance.block_height_new)
                except Exception as _rb:
                    node.logger.app_log.error(
                        f"post-vm-failure rollback also failed at {block_instance.block_height_new}: {_rb} "
                        f"— startup reconcile will heal on restart")
                raise

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
                # doc/26 stage 4 (S1): once the index is consensus-critical, a silently-stale index is an
                # inflation risk — HALT rather than drift. Inert (warn-only) while balance_index_consensus=off.
                if getattr(node, "balance_index_consensus", "off") != "off":
                    raise
                node.logger.app_log.warning(
                    f"balance index maintain failed at {block_instance.block_height_new}: {e}")

        # Optional maintained txid -> height index (doc/26 stage 4): index this block's POST-FORK txs
        # (apply_rows skips pre-fork/negative-height rows itself) so the dup-sig replay read can move off
        # SQLite. Like the balance index, a silently-stale index becomes a dedup-bypass risk once it is
        # consensus-critical, so HALT rather than drift when txid_index_consensus != off.
        if getattr(node, "txid_index", None) is not None:
            try:
                node.txid_index.apply_rows(processor.block_transactions, node.fork_height)
            except Exception as e:
                if getattr(node, "txid_index_consensus", "off") != "off":
                    raise
                node.logger.app_log.warning(
                    f"txid index maintain failed at {block_instance.block_height_new}: {e}")

        # hf2 pubkey-by-reference (doc/40): register this block's first-appearance RSA/ML-DSA/multisig keys
        # AFTER the block is fully validated (prior-block-only invariant — a same-block reference cannot
        # resolve). Like the other consensus projections: HALT (not drift) when pk_registry_consensus != off.
        if getattr(node, "pk_registry", None) is not None:
            try:
                node.pk_registry.register_from_block(processor.block_transactions, node.fork_height)
            except Exception as e:
                if getattr(node, "pk_registry_consensus", "off") != "off":
                    raise
                node.logger.app_log.warning(
                    f"pubkey registry maintain failed at {block_instance.block_height_new}: {e}")

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

    # Restore actual data, and heal a GENUINE ledger.db-ahead-of-working split in-run. The node keeps two
    # non-atomically-committed stores (ledger.db full + hyper.db rollup); a crash or a tip reorg can leave
    # ledger.db with rows ABOVE the committed working tip. Those orphans then make an "already have this"
    # check reject the peer's valid re-sync forever — via the block-hash check (block_already_exists) or the
    # replay guard — wedging sync until a manual restart runs chain_ops.reconcile_ledger_hyper. Heal it
    # here, but SAFELY (consensus-safety audit findings):
    #   * act ONLY when ledger.db is AHEAD of the working store (ledger_tip > working_tip) — the real split
    #     direction. In a NORMAL multi-block batch the working store is legitimately ahead (db_to_drive only
    #     flushes at batch end), so trimming there would discard just-validated blocks.
    #   * read FRESH tips (clear_caches) so the 1-second read cache can't trim canonical blocks.
    #   * route through chain_ops.rollback, NOT bare rollback_under, so the DERIVED consensus stores
    #     (vm_state + state root, token/alias index, balance_index) roll back in
    #     lockstep — a bare ledger delete would strand them above the tip (post-fork inflation / wedge).
    # All best-effort: a failure here must never mask the original rejection; the startup reconcile is the
    # backstop. The bounded replay guard (check_duplicate_signatures) is the complementary defense layer.
    try:
        db_handler.clear_caches()
    except Exception:
        pass
    try:
        working_tip = db_handler.block_max_ram()['block_height']
    except Exception:
        working_tip = node.last_block
    try:
        ledger_tip = db_handler.block_height_max()
    except Exception:
        ledger_tip = working_tip
    if ledger_tip is None:
        ledger_tip = working_tip
    if ledger_tip > working_tip:
        node.logger.app_log.warning(
            f"Chain: ledger ahead of working store (ledger={ledger_tip} > working={working_tip}); "
            f"rolling the orphaned excess above {working_tip} back so it can't wedge sync")
        try:
            import chain_ops
            chain_ops.rollback(node, db_handler, working_tip + 1)   # full rollback incl. derived stores
        except Exception as _heal_err:
            node.logger.app_log.warning(
                f"Chain: orphan-heal rollback failed ({_heal_err}); startup reconcile will heal on restart")
    node.last_block = working_tip
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