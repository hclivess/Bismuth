"""
Modularize the mining algo check
"""

import hashlib
from quantizer import *


__version__ = '0.0.1'


def bin_convert(string):
    return ''.join(format(ord(x), '8b').replace(' ', '0') for x in string)


def check_block(block_height_new, miner_address, nonce, db_block_hash, diff0, received_timestamp, q_received_timestamp,
                q_db_timestamp_last, peer_ip='N/A', app_log=None):
    """
    Checks that the given block matches the mining algo.

    :param block_height_new:
    :param miner_address:
    :param nonce:
    :param db_block_hash:
    :param diff0:
    :param received_timestamp:
    :param q_received_timestamp:
    :param q_db_timestamp_last:
    :param peer_ip:
    :param app_log:
    :return:
    """
    mining_hash = bin_convert(
        hashlib.sha224((miner_address + nonce + db_block_hash).encode("utf-8")).hexdigest())
    diff_drop_time = Decimal(180)
    mining_condition = bin_convert(db_block_hash)[0:int(diff0)]
    # simplified comparison, no backwards mining
    if mining_condition in mining_hash:
        if app_log:
            app_log.info("Difficulty requirement satisfied for block {} from {}".format (block_height_new, peer_ip))
        diff_save = diff0

    elif Decimal(received_timestamp) > q_db_timestamp_last + Decimal(diff_drop_time):
        # uses block timestamp, don't merge with diff() for security reasons
        time_difference = q_received_timestamp - q_db_timestamp_last
        diff_dropped = quantize_ten(diff0) - quantize_ten(time_difference / diff_drop_time)
        if diff_dropped < 50:
            diff_dropped = 50

        mining_condition = bin_convert(db_block_hash)[0:int(diff_dropped)]
        if mining_condition in mining_hash:  # simplified comparison, no backwards mining
            if app_log:
                app_log.info ("Readjusted difficulty requirement satisfied for block {} from {}"
                              .format(block_height_new, peer_ip))
            diff_save = diff0
            # lie about what diff was matched not to mess up the diff algo
        else:
            raise ValueError ("Readjusted difficulty too low for block {} from {}, should be at least {}".format(block_height_new, peer_ip, diff_dropped))
    else:
        raise ValueError ("Difficulty too low for block {} from {}, should be at least {}"
                          .format(block_height_new, peer_ip, diff0))
    return diff_save
