"""
Block & transaction data model for ``digest`` — the ``Transaction`` / ``MinerTransaction`` /
``Block`` value objects plus the consensus quantizers (``quantize_two`` / ``quantize_eight``).
Extracted from ``digest.py``; the ``BlockProcessor`` engine and the ``digest_block`` orchestration
stay there. Consensus boundary: ``Transaction`` routes through the frozen
``bismuth_serialize.signature_buffer`` and reconstructs the legacy ``'%.8f'`` / ``'%.2f'`` strings,
so these bodies are kept byte-identical (HARDFORK tags preserved). NOTE: these quantizers differ
from ``quantizer.py`` (no falsy short-circuit) and must not be swapped for those.
"""
import time
from decimal import Decimal
from typing import Any

import bismuth_serialize
import essentials
from essentials import address_is_rsa, MAX_TX_SIGNATURE_LEN, MAX_TX_PUBKEY_LEN
from polysign.signerfactory import SignerFactory


def quantize_two(value: float) -> Decimal:
    """Quantize to 2 decimal places."""
    return Decimal(value).quantize(Decimal('0.01'))


def quantize_eight(value: Any) -> Decimal:
    """Quantize to 8 decimal places."""
    return Decimal(value).quantize(Decimal('0.00000001'))


class Transaction:
    """Represents a single transaction within a block."""

    def __init__(self):
        self.start_time_tx = 0
        self.q_received_timestamp = 0
        self.received_timestamp = "0.00"
        self.received_address = None
        self.received_recipient = None
        self.received_amount = 0
        self.received_signature_enc = None
        self.received_public_key_b64encoded = None
        self.received_operation = None
        self.received_openfield = None

    def from_raw_transaction(self, transaction: tuple, index: int, tx_count: int,
                             block_height: int = None, fork_height=None) -> 'MinerTransaction':
        """Parse raw transaction data and populate fields."""
        self.start_time_tx = quantize_two(time.time())
        self.q_received_timestamp = quantize_two(transaction[0])
        self.received_timestamp = '%.2f' % self.q_received_timestamp
        self.received_address = str(transaction[1])[:56]
        self.received_recipient = str(transaction[2])[:56]
        # HARDFORK (doc/16): consensus boundary — the signature/hash are computed over the legacy
        # '%.8f' decimal-string amount (and the '%.2f' timestamp above), so even integer storage must
        # reconstruct these strings here. transaction[3] is the received wire amount (always decimal),
        # so this stays until the hard fork signs/hashes native integer units and these conversions go.
        self.received_amount = '%.8f' % (quantize_eight(transaction[3]))
        self.received_signature_enc = str(transaction[4])[:MAX_TX_SIGNATURE_LEN]
        self.received_public_key_b64encoded = str(transaction[5])[:MAX_TX_PUBKEY_LEN]
        # hf2: operation/openfield are free-form and uncapped POST-FORK (doc/41, generalized). Pre-fork the
        # legacy 30 / 100000 truncation is part of FROZEN consensus: it defines the canonical signing-buffer
        # and legacy-block-hash input even for an over-length field that a peer-relayed block could carry
        # (such a block bypasses the mempool caps), so it MUST be preserved below the fork. Gate on the
        # DESTINATION height — below the fork: truncate (byte-identical to legacy); at/after: full value.
        _post_fork = (fork_height is not None and block_height is not None and block_height >= fork_height)
        self.received_operation = str(transaction[6]) if _post_fork else str(transaction[6])[:30]
        self.received_openfield = str(transaction[7]) if _post_fork else str(transaction[7])[:100000]

        # Check if this is the mining transaction (last in block)
        if index == tx_count - 1:
            if float(self.received_amount) != 0:
                raise ValueError("Coinbase (Mining) transaction must have zero amount")
            # hf2 §2.C: the coinbase is PoW-authorized (no signature), so ANY address scheme can mine. The
            # legacy RSA-only restriction is enforced PRE-FORK only, in validate() (which knows the fork
            # height); removing it here lets ecdsa / ED25519 / ML-DSA / multisig miners win blocks post-fork.

            # Return miner transaction data
            miner_tx = MinerTransaction()
            miner_tx.q_block_timestamp = self.q_received_timestamp
            # PoW nonce source is fork-dependent and resolved at PoW-check time (this method has no
            # fork height): pre-fork the nonce rides in the openfield; post-fork (doc/41) it rides in the
            # freed signature slot, leaving the openfield as free miner data.
            miner_tx.nonce = self.received_openfield[:128]
            miner_tx.nonce_v2 = (self.received_signature_enc or "")[:128]
            miner_tx.miner_address = self.received_address
            return miner_tx

        return None

    def validate(self, node, last_block_timestamp: float, block_height: int = None,
                 verify_signature: bool = True, is_coinbase: bool = False) -> None:
        """Validate transaction elements. Raises ValueError on invalid transaction.

        ``block_height`` is the height this tx is being validated INTO; it fork-gates the signature scheme
        (post-hf2 single-sig secp256k1 verifies by ecrecover over the content txid). When None (legacy
        callers), verification falls back to the pre-fork scheme.

        ``verify_signature`` False skips ONLY the expensive cryptographic signature check (the field/amount/
        timestamp/address checks still run). Used by the from-genesis ``assume_valid_height`` fast-path
        (doc/30) for history already buried under millions of PoW blocks. Default True == verify every sig."""
        # Timestamp checks (cheap operations first)
        if self.start_time_tx < self.q_received_timestamp:
            minutes_future = quantize_two((self.q_received_timestamp - self.start_time_tx) / 60)
            raise ValueError(f"Future transaction not allowed, timestamp {minutes_future} minutes in the future")

        if last_block_timestamp - 86400 > self.q_received_timestamp:
            raise ValueError("Transaction older than 24h not allowed.")

        # Amount validation
        if float(self.received_amount) < 0:
            raise ValueError("Negative balance spend attempt")

        # Address validation
        if not essentials.address_validate(self.received_address):
            raise ValueError("Not a valid sender address")
        if not essentials.address_validate(self.received_recipient):
            raise ValueError("Not a valid recipient address")

        # Signature verification (expensive operation last). Fork-gated on the single hf2 fork_height:
        # at/after it an ordinary single-sig secp256k1 tx is verified by ecrecover over the content txid
        # (Ethereum-shape, public key dropped); pre-fork txs and post-fork RSA/ED25519/multisig keep the
        # legacy buffer+explicit-pubkey verification.
        if not verify_signature:
            return   # assume-valid fast-path (doc/30): trusted history, signature re-check skipped
        fork_height = getattr(node, "fork_height", None)
        post_fork = fork_height is not None and block_height is not None and block_height >= fork_height
        # hf2 §2.C: PRE-FORK the coinbase must be a legacy RSA address (frozen rule — a non-upgraded node
        # rejects a non-RSA coinbase, so an upgraded node must too, for chain-split safety). POST-FORK any
        # valid scheme may mine (already address_validate'd above; PoW + reward formula authorize it).
        if is_coinbase and not post_fork and not address_is_rsa(self.received_address):
            raise ValueError("pre-fork coinbase (mining) transaction must be a legacy RSA address")
        SignerFactory.verify_tx_signature(
            post_fork,
            self.received_timestamp,
            self.received_address,
            self.received_recipient,
            self.received_amount,
            self.received_operation,
            self.received_openfield,
            self.received_signature_enc,
            self.received_public_key_b64encoded,
            registry=getattr(node, "pk_registry", None),   # doc/40 pubkey-by-reference resolution
            is_coinbase=is_coinbase,                        # doc/29 §2.C coinbase compaction (sig-less)
        )

    def to_tuple(self) -> tuple:
        """Convert transaction to tuple format for storage."""
        return (
            self.received_timestamp,
            self.received_address,
            self.received_recipient,
            self.received_amount,
            self.received_signature_enc,
            self.received_public_key_b64encoded,
            self.received_operation,
            self.received_openfield
        )


class MinerTransaction:
    """Represents the mining transaction (coinbase) of a block."""

    def __init__(self):
        self.q_block_timestamp = 0
        self.nonce = None        # pre-fork PoW nonce source: the coinbase openfield
        self.nonce_v2 = None     # doc/41 post-fork PoW nonce source: the coinbase signature slot
        self.miner_address = None


class Block:
    """Represents a block being processed."""

    def __init__(self, node):
        self.tx_count = 0
        self.block_height_new = node.last_block + 1
        self.block_hash = 'N/A'
        self.failed_cause = ''
        self.block_count = 0
        self.transaction_list_converted = []
        self.mining_reward = None
        self.mirror_hash = None
        self.start_time_block = quantize_two(time.time())
        self.tokens_operation_present = False
