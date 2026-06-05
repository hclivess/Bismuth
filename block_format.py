"""
Block → JSON formatters for the legacy API responses, extracted from ``apihandler.ApiHandler``.

These are pure functions (they never used ``self``) that turn raw ``transactions`` rows into the exact
legacy JSON shapes the socket `*json` commands and the REST block endpoints return. Amount/fee/reward
are reconstructed through ``essentials.format_raw_tx`` (which honors integer-unit storage), so the
output is byte-for-byte the historical shape. Kept here so ``apihandler`` and ``rest_api`` share one
implementation instead of a 900-line module owning formatting too.
"""
from essentials import format_raw_tx


def blockstojson(raw_blocks: list):
    """Group raw transaction rows into ``{height: {block_height, block_hash, transactions[]}}``."""
    tx_list = []
    block = {}
    blocks = {}

    old = None
    for transaction_raw in raw_blocks:
        transaction = format_raw_tx(transaction_raw)
        height = transaction['block_height']
        hash = transaction['block_hash']

        del transaction['block_height']
        del transaction['block_hash']

        if old != height:  # if same block
            del tx_list[:]
            block.clear()

        tx_list.append(transaction)

        block['block_height'] = height
        block['block_hash'] = hash
        block['transactions'] = list(tx_list)
        blocks[height] = dict(block)

        old = height  # update

    return blocks


def blocktojsondiffs(list_of_txs: list, list_of_diffs: list):
    """Split each block into its mining tx (``reward != 0``) and normal ``transactions`` (``reward == 0``),
    attaching difficulty to the mining tx. Used by ``api_getblockrange``."""
    i = 0
    blocks_dict = {}
    block_dict = {}
    normal_transactions = []

    old = None
    for transaction in list_of_txs:
        transaction_formatted = format_raw_tx(transaction)
        height = transaction_formatted["block_height"]

        del transaction_formatted["block_height"]

        #  del transaction_formatted["signature"]  # optional
        #  del transaction_formatted["pubkey"]  # optional

        if old != height:
            block_dict.clear()
            del normal_transactions[:]

        if transaction_formatted["reward"] == 0:  # if normal tx
            del transaction_formatted["block_hash"]
            del transaction_formatted["reward"]
            normal_transactions.append(transaction_formatted)

        else:
            del transaction_formatted["address"]
            del transaction_formatted["amount"]
            transaction_formatted['difficulty'] = list_of_diffs[i][0]
            block_dict['mining_tx'] = transaction_formatted

            block_dict['transactions'] = list(normal_transactions)

            blocks_dict[height] = dict(block_dict)
            i += 1
        old = height

    return blocks_dict
