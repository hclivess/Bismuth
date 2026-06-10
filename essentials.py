"""
Common helpers for Bismuth - Optimized Version
"""
import base64
import math
import re
from collections import Counter
from functools import lru_cache

import requests
from quantizer import quantize_eight
from decimal import Decimal
from polysign.signerfactory import SignerFactory

import amounts
import db_helpers
import wallet_helpers

# Wallet & key management was lifted into wallet_helpers; re-bind here so the historical
# `essentials.sign_rsa` / `essentials.keys_load` / `from essentials import ...` call sites keep working.
sign_rsa = wallet_helpers.sign_rsa
keys_check = wallet_helpers.keys_check
keys_save = wallet_helpers.keys_save
keys_load = wallet_helpers.keys_load
keys_unlock = wallet_helpers.keys_unlock
keys_load_new = wallet_helpers.keys_load_new

__version__ = "0.0.7"

"""
0.0.7 : decrease checkpoint limit to 30 blocks at 1450000 (meaning max 59 blocks rollback)
"""

# Constants to avoid recreating Decimal objects
DECIMAL_ZERO = Decimal(0)
DECIMAL_ONE = Decimal(1)
DECIMAL_TEN = Decimal(10)
DECIMAL_HUNDRED_THOUSAND = Decimal(100000)
BASE_FEE = Decimal("0.01")

# Regex cache for replace_regex function
_regex_cache = {}

"""
For temp. code compatibility, dup code moved to polysign module
"""


@lru_cache(maxsize=1024)
def address_validate(address: str) -> bool:
    return SignerFactory.address_is_valid(address)


@lru_cache(maxsize=1024)
def address_is_rsa(address: str) -> bool:
    return SignerFactory.address_is_rsa(address)


"""
End compatibility
"""


def format_raw_tx(raw: list) -> dict:
    # Pre-size dictionary with all keys for better performance
    transaction = {
        'block_height': raw[0],
        'timestamp': raw[1],
        'address': raw[2],
        'recipient': raw[3],
        # display-edge (doc/16 phase 2): display_amount returns a FLOAT in integer-storage mode,
        # matching the legacy NUMERIC-coerced float output. This is load-bearing: blocktojsondiffs
        # classifies txs with `reward == 0`, so a string '0.00000000' (what from_units returns) would
        # be `!= 0` and misclassify every normal tx as a mining tx.
        # HARDFORK (doc/16): once consensus signs/hashes native integers, amounts are integer
        # end-to-end and this reconstruction disappears.
        'amount': amounts.display_amount(raw[4]),
        'signature': raw[5],
        # HARDFORK (doc/16): txid is an ad-hoc signature[:56] slice; adopt nado's fixed-length
        # content-hash txid (blake2b of tx content; the signature signs the txid).
        'txid': raw[5][:56],
        'block_hash': raw[7],
        'fee': amounts.display_amount(raw[8]),
        'reward': amounts.display_amount(raw[9]),
        'operation': raw[10],
        'openfield': raw[11]
    }

    # Only try base64 decode if it looks like base64
    if isinstance(raw[6], str) and len(raw[6]) > 0:
        try:
            transaction['pubkey'] = base64.b64decode(raw[6]).decode('utf-8')
        except:
            transaction['pubkey'] = raw[6]  # support new pubkey schemes
    else:
        transaction['pubkey'] = raw[6]

    return transaction


def percentage(percent, whole):
    return Decimal(percent) * Decimal(whole) / 100


def replace_regex(string: str, replace: str) -> str:
    # Cache compiled regex patterns for performance
    if replace not in _regex_cache:
        _regex_cache[replace] = re.compile(r'^{}'.format(re.escape(replace)))
    return _regex_cache[replace].sub("", string)


def download_file(url: str, filename: str) -> None:
    """Download a file from URL to filename

    :param url: URL to download file from
    :param filename: Filename to save downloaded data as

    returns `filename`
    """
    try:
        r = requests.get(url, stream=True)
        total_size = int(r.headers.get('content-length', 0)) / 1024

        with open(filename, 'wb') as fp:
            downloaded = 0
            last_percent = 0

            # Use larger chunks for better I/O performance
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    fp.write(chunk)
                    downloaded += len(chunk) / 1024

                    if total_size > 0:
                        percent = int(100 * (downloaded / total_size))
                        if percent > last_percent and percent % 10 == 0:
                            print(f"Downloaded {percent} %")
                            last_percent = percent

            print("Downloaded 100 %")
    except:
        raise


def most_common(lst: list):
    """Used by consensus - optimized with Counter"""
    if not lst:
        return None
    return Counter(lst).most_common(1)[0][0]


def most_common_dict(a_dict: dict):
    """Returns the most common value from a dict. Used by consensus"""
    return max(a_dict.values())


def percentage_in(individual, whole):
    return (float(list(whole).count(individual) / float(len(whole)))) * 100


def round_down(number, order):
    return int(math.floor(number / order)) * order


def checkpoint_set(node):
    # Max rollback depth. Configurable (config.txt: rollback_depth, default 30) so an operator whose
    # node is stuck on a minority fork deeper than the checkpoint can deepen it to rejoin the longest
    # chain. Default 30 preserves historical behavior (max ~59-block rollback). See doc/14.
    limit = getattr(node, "rollback_depth", 30)
    if node.last_block < 1450000:
        limit = 1000
    checkpoint = round_down(node.last_block, limit) - limit
    if checkpoint != node.checkpoint:
        node.checkpoint = checkpoint
        node.logger.app_log.warning(f"Checkpoint set to {node.checkpoint}")


def rollback_allowed(node, target_height) -> bool:
    """
    Decide whether the node may roll its chain tip back to `target_height`.

    - Shallow rollbacks (target at or above node.checkpoint) are always allowed: the normal small
      reorgs, bounded by the usual ~rollback_depth window.
    - Deeper rollbacks (below the checkpoint) AUTO-RECOVER (default on): instead of the old rigid refusal
      that stranded an honestly-forked node and needed a manual re-bootstrap, the node rolls back as deep
      as needed to rejoin the chain that PROVEN peers agree on. Safe because it requires a supermajority
      of enough peers INCLUDING reputable ones (positive reputation = have delivered valid PoW blocks), so
      a fresh sybil flood can't force a deep reorg — and the deep chain's blocks are PoW-validated on
      ingest regardless, so an attacker still needs real 51% work. See doc/14.
    """
    if target_height >= node.checkpoint:
        return True
    if not getattr(node, "rollback_consensus", True):   # auto-recovery ON by default
        return False
    peers = getattr(node, "peers", None)
    if peers is None:
        return False
    enough_peers = peers.consensus_size >= getattr(node, "rollback_consensus_min_peers", 3)
    strong_majority = (peers.consensus_percentage or 0) >= getattr(node, "rollback_consensus_threshold", 75)
    # require PROVEN peers too, so a fresh sybil flood (no valid blocks delivered) can't force a deep reorg
    enough_reputable = getattr(peers, "reputable_count", 0) >= getattr(node, "rollback_consensus_min_reputable", 1)
    return enough_peers and strong_majority and enough_reputable


def ledger_balance3(address, cache, db_handler):
    # Many heavy blocks are pool payouts, same address.
    # Cache pre_balance instead of recalc for every tx
    if address in cache:
        return cache[address]

    # Optimized: Single query instead of two separate queries
    db_handler.execute_param(db_handler.c,
        """SELECT 
           SUM(CASE WHEN recipient = ? THEN amount + reward ELSE 0 END) as credit,
           SUM(CASE WHEN address = ? THEN amount + fee ELSE 0 END) as debit
           FROM transactions 
           WHERE recipient = ? OR address = ?""",
        (address, address, address, address))

    result = db_handler.c.fetchone()
    if amounts.LEDGER_INTEGER:
        credit = amounts.to_decimal(result[0])
        debit = amounts.to_decimal(result[1])
    else:
        credit = Decimal(result[0] or 0)
        debit = Decimal(result[1] or 0)

    cache[address] = quantize_eight(credit - debit)
    return cache[address]


def ledger_balance3_original(address, cache, db_handler):
    """Keep original implementation as fallback if needed"""
    if address in cache:
        return cache[address]
    credit_ledger = Decimal(0)

    db_handler.execute_param(db_handler.c, "SELECT amount, reward FROM transactions WHERE recipient = ?;", (address,))
    entries = db_handler.c.fetchall()

    for entry in entries:
        credit_ledger += quantize_eight(entry[0]) + quantize_eight(entry[1])

    debit_ledger = Decimal(0)
    db_handler.execute_param(db_handler.c, "SELECT amount, fee FROM transactions WHERE address = ?;", (address,))
    entries = db_handler.c.fetchall()

    for entry in entries:
        debit_ledger += quantize_eight(entry[0]) + quantize_eight(entry[1])

    cache[address] = quantize_eight(credit_ledger - debit_ledger)
    return cache[address]




def fee_calculate(openfield: str, operation: str = '', block: int = 0,
                  base_fee=None, vm_surcharge: bool = False) -> Decimal:
    # base_fee: the post-fork DYNAMIC base fee (fee_dynamics); None -> the static BASE_FEE (pre-fork, and
    # any caller that doesn't supply it). vm_surcharge: post-fork, charge vm: txs for execution (gas).
    base = BASE_FEE if base_fee is None else Decimal(base_fee)
    fee = base + (Decimal(len(openfield)) / DECIMAL_HUNDRED_THOUSAND)

    # Check operation first (faster than string.startswith)
    if operation == "token:issue":
        fee += DECIMAL_TEN
    elif openfield.startswith("alias="):  # Only check if needed
        fee += DECIMAL_ONE
    if vm_surcharge and operation.startswith("vm:"):
        import fee_dynamics
        fee += fee_dynamics.VM_SURCHARGE
    # shielded txs (doc/22) are larger and costlier to validate (EC ops); a flat post-fork surcharge
    # discourages spamming the pool. Same gate as the vm surcharge (post-fork only).
    if vm_surcharge and operation.startswith("shield:"):
        fee += DECIMAL_ONE

    return quantize_eight(fee)


# The post-fork dynamic-fee congestion signal (per-block WEIGHT) is read from the BLOCK STORE
# (block_store.BlockStore.recent_block_weights), NOT SQLite — there is no SQLite on any post-fork path.
# The earlier SQLite recent_tx_counts / recent_block_weights helpers were removed for that reason.


def execute_param_c(cursor, query, param, app_log):
    """Secure execute w/ param for slow nodes (aborts on UnicodeEncodeError, retries otherwise)."""
    db_helpers.retry_db(lambda: cursor.execute(query, param), abort_on=(UnicodeEncodeError,),
                        delay=0.1, log=app_log, describe=str(query)[:100])
    return cursor


def is_sequence(arg) -> bool:
    # TODO: hard to read compound condition.
    return not hasattr(arg, "strip") and hasattr(arg, "__getitem__") or hasattr(arg, "__iter__")