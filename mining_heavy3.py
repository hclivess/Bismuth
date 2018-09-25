"""
Modularize the mining algo check

From bismuth node
"""

import annealing
from hashlib import sha224
from quantizer import *


__version__ = '0.0.2'

print("Mining_Heavy3 v{}".format(__version__))



def bin_convert(string):
    return ''.join(format(ord(x), '8b').replace(' ', '0') for x in string)


def diffme_heavy3(pool_address, nonce, db_block_hash):
    # minimum possible diff
    diff = 1
    diff_result = 0
    hash = sha224((pool_address + nonce + db_block_hash).encode("utf-8")).digest()
    hash = int.from_bytes(hash, 'big')
    annealed_sha = annealing.anneal3(annealing.MMAP, hash)
    bin_annealed_sha = bin_convert(annealed_sha)
    mining_condition = bin_convert(db_block_hash)
    while mining_condition[:diff] in bin_annealed_sha:
        diff_result = diff
        diff += 1
    return diff_result



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
    if block_height_new == 610100:
        diff0 = 100
    if block_height_new == 610101:
        diff0 = 100
    real_diff = diffme_heavy3(miner_address, nonce, db_block_hash)
    diff_drop_time = Decimal(180)
    mining_condition = bin_convert(db_block_hash)[0:int(diff0)]
    # simplified comparison, no backwards mining
    if real_diff >= int(diff0):
        if app_log:
            app_log.info("Difficulty requirement satisfied for block {} from {}. {} >= {}"
                         .format(block_height_new, peer_ip, real_diff, int(diff0)))
        diff_save = diff0

    elif Decimal(received_timestamp) > q_db_timestamp_last + Decimal(diff_drop_time):
        # uses block timestamp, don't merge with diff() for security reasons
        factor = 1
        time_difference = q_received_timestamp - q_db_timestamp_last
        diff_dropped = quantize_ten(diff0) - quantize_ten(time_difference / diff_drop_time)
        # Emergency diff drop
        if Decimal(received_timestamp) > q_db_timestamp_last + Decimal(2 * diff_drop_time):
            factor = 10
            diff_dropped = quantize_ten(diff0) - quantize_ten(2) - quantize_ten(factor * (time_difference-2*diff_drop_time) / diff_drop_time)
        if diff_dropped < 50:
            diff_dropped = 50
        if real_diff >= int(diff_dropped):
            if app_log:
                app_log.info ("Readjusted difficulty requirement satisfied for block {} from {}, {} >= {} (factor {})"
                              .format(block_height_new, peer_ip, real_diff, int(diff_dropped), factor))
            diff_save = diff0
            # lie about what diff was matched not to mess up the diff algo
        else:
            raise ValueError ("Readjusted difficulty too low for block {} from {}, {} should be at least {}"
                              .format(block_height_new, peer_ip, real_diff, diff_dropped))
    else:
        raise ValueError ("Difficulty {} too low for block {} from {}, should be at least {}"
                          .format(real_diff, block_height_new, peer_ip, diff0))
    return diff_save


def mining_open():
    annealing.junction_open()


def mining_close():
    annealing.junction_close()

