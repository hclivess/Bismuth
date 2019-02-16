"""
Modularize the mining algo check

Bismuth Heavy3

From bismuth node
"""

import mmap
import os
import struct
import sys
from hashlib import sha224
from hmac_drbg import DRBG
from quantizer import *

import regnet

__version__ = '0.1.3'


print("Mining_Heavy3 v{}".format(__version__))


POW_FORK = 854660
FORK_DIFF = 108.9


RND_LEN = 0

MMAP = None
F = None

is_regnet = False


def read_int_from_map(map, index):
    return struct.unpack('I', map[4 * index:4 * index + 4])[0]


def anneal3(mmap, n):
    """
    Converts 224 bits number into annealed version, hexstring

    :param n: a 224 = 7x32 bits
    :return:  56 char in hex encoding.
    """
    h7 = n & 0xffffffff
    n = n >> 32
    index = ((h7 & ~0x7) % RND_LEN) * 4
    f1 = struct.unpack('I', mmap[index:index + 4])[0]
    value = h7 ^ struct.unpack('I', mmap[index:index + 4])[0]
    res = "{:08x}".format(value)
    for i in range(6):
        index += 4
        h = n & 0xffffffff
        n = n >> 32
        value = h ^ struct.unpack('I', mmap[index:index + 4])[0]
        res = "{:08x}".format(value) + res
    return res


def bin_convert(string):
    return ''.join(format(ord(x), '8b').replace(' ', '0') for x in string)


def diffme_heavy3(pool_address, nonce, db_block_hash):
    # minimum possible diff
    diff = 1
    diff_result = 0
    hash = sha224((pool_address + nonce + db_block_hash).encode("utf-8")).digest()
    hash = int.from_bytes(hash, 'big')
    annealed_sha = anneal3(MMAP, hash)
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
    if block_height_new == POW_FORK - 1 :
        diff0 = FORK_DIFF
    if block_height_new == POW_FORK:
        diff0 = FORK_DIFF
    if is_regnet:
        diff0 = regnet.REGNET_DIFF - 8

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
        diff_dropped = quantize_ten(diff0) + quantize_ten(1) - quantize_ten(time_difference / diff_drop_time)
        # Emergency diff drop
        if Decimal(received_timestamp) > q_db_timestamp_last + Decimal(2 * diff_drop_time):
            factor = 10
            diff_dropped = quantize_ten(diff0) - quantize_ten(1) - quantize_ten(factor * (time_difference-2*diff_drop_time) / diff_drop_time)

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


def create_heavy3a(file_name):
    print("Creating Junction Noise file, this usually takes a few minutes...")
    gen = DRBG(b"Bismuth is a chemical element with symbol Bi and atomic number 83. It is a pentavalent post-transition metal and one of the pnictogens with chemical properties resembling its lighter homologs arsenic and antimony.")
    # Size in Gb - No more than 4Gb from a single seed
    GB = 1
    # Do not change chunk size, it would change the file content.
    CHUNK_SIZE = 1024*4  # time 3m20.990s
    COUNT = GB * 1024 * 1024 * 1024 // CHUNK_SIZE
    with open(file_name, 'wb') as f:
        for chunks in range(COUNT):
            f.write(gen.generate(CHUNK_SIZE))


def mining_open():
    """
    Opens the Junction MMapped file
    """
    global F
    global MMAP
    global RND_LEN
    map = './heavy3a.bin' if os.path.isfile('./heavy3a.bin') else '../CSPRNG/rnd.bin'
    if not os.path.isfile(map):
        create_heavy3a('./heavy3a.bin')
        map = './heavy3a.bin'
    try:
        F = open(map, "rb+")
        # memory-map the file, size 0 means whole file
        MMAP = mmap.mmap(F.fileno(), 0)
        RND_LEN = os.path.getsize(map) // 4
        if read_int_from_map(MMAP, 0) != 3786993664:
            raise ValueError("Wrong file: {}".format(map))
        if read_int_from_map(MMAP, 1024) != 1742706086:
            raise ValueError("Wrong file: {}".format(map))
    except Exception as e:
        print("Error while loading Junction file: {}".format(e))
        sys.exit()


def mining_close():
    """
    Close the MMAP access, HAS to be called at end of program.
    """
    global F
    global MMAP

    if MMAP:
        MMAP.close()
    if F:
        F.close()
