"""
Modularize the mining algo check

Bismuth Heavy3

From bismuth node
"""

import mmap
import os
import struct
import sys
from hashlib import sha224, blake2b
from hmac_drbg import DRBG
from quantizer import quantize_two, quantize_eight, quantize_ten
from decimal import Decimal

import regnet

from fork import Fork
fork = Fork()

__version__ = '0.1.4'


print("Mining_Heavy3 v{}".format(__version__))

RND_LEN = 0

MMAP = None
F = None

is_regnet = False
heavy = True


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
    # f1 = struct.unpack('I', mmap[index:index + 4])[0]
    value = h7 ^ struct.unpack('I', mmap[index:index + 4])[0]
    res = "{:08x}".format(value)
    for i in range(6):
        index += 4
        h = n & 0xffffffff
        n = n >> 32
        value = h ^ struct.unpack('I', mmap[index:index + 4])[0]
        res = "{:08x}".format(value) + res
    return res


def anneal3_regnet(mmap, n):
    """
    Converts 224 bits number into annealed version, hexstring
    This uses a fake anneal for easier regtests

    :param n: a 224 = 7x32 bits
    :return:  56 char in hex encoding.
    """
    return "{:056x}".format(n)


def bin_convert(string):
    return ''.join(format(ord(x), '8b').replace(' ', '0') for x in string)


def diffme_heavy3(pool_address, nonce, db_block_hash, new_pow=False):
    # minimum possible diff
    diff = 1
    diff_result = 0
    _data = (pool_address + nonce + db_block_hash).encode("utf-8")
    # Dual PoW (doc/18-D, bundled into the single hf2 fork): post-fork the inner hash modernises
    # sha224 -> blake2b (28-byte digest, same 224-bit width). The 1 GB memory-hard anneal and the
    # substring-prefix difficulty are UNCHANGED, so a node/miner switches algos purely on `new_pow`
    # (= block_height >= the signalled hf2 fork_height).
    raw = blake2b(_data, digest_size=28).digest() if new_pow else sha224(_data).digest()
    hash224 = int.from_bytes(raw, 'big')
    if heavy or (not is_regnet):
        annealed_sha = anneal3(MMAP, hash224)
    else:
        annealed_sha = anneal3_regnet(MMAP, hash224)
    bin_annealed_sha = bin_convert(annealed_sha)
    mining_condition = bin_convert(db_block_hash)
    while mining_condition[:diff] in bin_annealed_sha:
        diff_result = diff
        diff += 1
    return diff_result


def check_block(block_height_new, miner_address, nonce, db_block_hash, diff0, received_timestamp, q_received_timestamp,
                q_db_timestamp_last, peer_ip='N/A', app_log=None, new_pow=False):
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
    try:
        if is_regnet:
            diff0 = regnet.REGNET_DIFF - 8

        real_diff = diffme_heavy3(miner_address, nonce, db_block_hash, new_pow=new_pow)
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
    except Exception as e:
            # Left for edge cases debug
            print("MH3 check block", e)
            exc_type, exc_obj, exc_tb = sys.exc_info()
            fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
            print(exc_type, fname, exc_tb.tb_lineno)
            raise


def create_heavy3a(file_name="heavy3a.bin"):
    if (not heavy) and is_regnet:
        print("Regnet, no heavy file")
        return
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


def mining_open(file_name="heavy3a.bin"):
    """
    Opens the Junction MMapped file
    """
    if (not heavy) and is_regnet:
        print("Regnet, no heavy file to open")
        return
    global F
    global MMAP
    global RND_LEN
    if os.path.isfile(file_name):
        size = os.path.getsize(file_name)
        if size != 1073741824:
            print("Invalid size of heavy file {}.".format(file_name))
            try:
                os.remove(file_name)
                print("Deleted, Will be re-created")
            except Exception as e:
                print(e)
                sys.exit()
    if not os.path.isfile(file_name):
        create_heavy3a(file_name)
    try:
        F = open(file_name, "rb+")
        # memory-map the file, size 0 means whole file
        MMAP = mmap.mmap(F.fileno(), 0)
        RND_LEN = os.path.getsize(file_name) // 4
        if read_int_from_map(MMAP, 0) != 3786993664:
            raise ValueError("Wrong file: {}".format(file_name))
        if read_int_from_map(MMAP, 1024) != 1742706086:
            raise ValueError("Wrong file: {}".format(file_name))
    except Exception as e:
        print("Error while loading Junction file: {}".format(e))
        sys.exit()


def mining_close():
    """
    Close the MMAP access, HAS to be called at end of program.
    """
    if (not heavy) and is_regnet:
        print("Regnet, no heavy file to close")
        return
    global F
    global MMAP
    try:
        assert MMAP
        MMAP.close()
    except:
        pass

    try:
        assert F
        F.close()
    except:
        pass
