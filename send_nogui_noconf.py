"""
Send a transaction from console, with no password nor confirmation asked.
To be used for unattended, automated processes.

This file takes optional arguments,

arg1: amount to send
arg2: recipient address
arg3: operation
arg4: OpenField data
arg5: wallet file
arg6: request confirmation for every transaction

args3,4,6 are not prompted if ran without args
"""

import base64
import sys
import time

import socks
from Cryptodome.Hash import SHA
from Cryptodome.Signature import PKCS1_v1_5

from bismuthclient import rpcconnections
from bisbasic import essentials, options
from bisbasic.essentials import fee_calculate
from polysign.signerfactory import SignerFactory


def connect():
    if 'regnet' in config.version:
        port = 3030
    elif 'testnet' in config.version:
        port = 2829
    else:
        port = 5658

    return rpcconnections.Connection(("127.0.0.1", int(port)))

if __name__ == "__main__":
    config = options.Get()
    config.read()

    try:
        wallet_file = sys.argv[5]
    except:
        wallet_file = input("Path to wallet: ")

    try:
        request_confirmation = sys.argv[6]
    except:
        request_confirmation = False

    key, public_key_readable, private_key_readable, encrypted, unlocked, public_key_b64encoded, address, keyfile = essentials.keys_load_new(wallet_file)

    if encrypted:
        key, private_key_readable = essentials.keys_unlock(private_key_readable)

    print(f'Number of arguments: {len(sys.argv)} arguments.')
    print(f'Argument list: {"".join(sys.argv)}')
    print(f'Using address: {address}')

    # get balance

    s = connect()
    s._send ("balanceget")
    s._send (address)  # change address here to view other people's transactions
    stats_account = s._receive()
    balance = stats_account[0]

    print("Transaction address: %s" % address)
    print("Transaction address balance: %s" % balance)

    try:
        amount_input = sys.argv[1]
    except IndexError:
        amount_input = input("Amount: ")

    try:
        recipient_input = sys.argv[2]
    except IndexError:
        recipient_input = input("Recipient: ")

    if not SignerFactory.address_is_valid(recipient_input):
        print("Wrong address format")
        sys.exit(1)

    try:
        operation_input = sys.argv[3]
    except IndexError:
        operation_input = ""

    try:
        openfield_input = sys.argv[4]
    except IndexError:
        openfield_input = ""

    fee = fee_calculate(openfield_input)
    print("Fee: %s" % fee)

    if request_confirmation:
        confirm = input("Confirm (y/n): ")

        if confirm != 'y':
            print("Transaction cancelled, user confirmation failed")
            exit(1)

    try:
        float(amount_input)
        is_float = 1
    except ValueError:
        is_float = 0
        sys.exit(1)

    timestamp = '%.2f' % (time.time() - 5) #remote proofing
    # TODO: use transaction object, no dup code for buffer assembling
    transaction = (str(timestamp), str(address), str(recipient_input), '%.8f' % float(amount_input), str(operation_input), str(openfield_input))  # this is signed
    # TODO: use polysign here
    h = SHA.new(str(transaction).encode("utf-8"))
    signer = PKCS1_v1_5.new(key)
    signature = signer.sign(h)
    signature_enc = base64.b64encode(signature)
    txid = signature_enc[:56]
    # doc/29 §6 bug 2: post-hf2 the canonical id is the content-hash txid (blake2b of the 6-field
    # pre-image), not the signature prefix. RSA signing itself is unchanged post-fork, so print BOTH ids
    # labelled by era (no node round-trip needed): use the content txid against an hf2-active node.
    import bismuth_serialize        # Stage 2 (doc/29): post-fork canonical id is the binary-pre-image txid
    content_txid = bismuth_serialize.tx_id_v2_s(str(timestamp), str(address), str(recipient_input),
                                                '%.8f' % float(amount_input), str(operation_input),
                                                str(openfield_input))

    print(f"Encoded Signature: {signature_enc.decode('utf-8')}")
    print(f"Transaction ID (pre-hf2, signature prefix): {txid.decode('utf-8')}")
    print(f"Transaction ID (post-hf2, content hash): {content_txid}")

    verifier = PKCS1_v1_5.new(key)

    if verifier.verify(h, signature):
        if float(amount_input) < 0:
            print("Signature OK, but cannot use negative amounts")

        elif float(amount_input) + float(fee) > float(balance):
            print("Mempool: Sending more than owned")

        else:
            tx_submit = (str (timestamp), str (address), str (recipient_input), '%.8f' % float (amount_input), str (signature_enc.decode ("utf-8")), str (public_key_b64encoded.decode("utf-8")), str (operation_input), str (openfield_input))
            while True:
                try:
                    s._send("mpinsert")
                    s._send (tx_submit)
                    reply = s._receive()
                    print ("Client: {}".format (reply))
                    if reply != "*":  # response can be empty due to different timeout setting
                        break
                    else:
                        print("Connection cut, retrying")

                except Exception as e:
                    print(f"A problem occurred: {e}, retrying")
                    s = connect()
                    pass
    else:
        print("Invalid signature")

    s.close()
