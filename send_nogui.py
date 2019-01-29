# this file takes optional arguments, arg1 = amount to spend, arg2 = recipient address, arg3 = keep forever (0/1), arg4=OpenField data
# args3+4 are not prompted if ran without args

from Cryptodome.Signature import PKCS1_v1_5
from Cryptodome.Hash import SHA
from essentials import fee_calculate

import base64
import time
import sqlite3
import essentials
import sys
import options
import re
import socks
import connections

config = options.Get()
config.read()
full_ledger = config.full_ledger_conf
ledger_path = config.ledger_path_conf
hyper_path = config.hyper_path_conf


key, public_key_readable, private_key_readable, encrypted, unlocked, public_key_hashed, address, keyfile = essentials.keys_load("privkey.der", "pubkey.der")

if encrypted:
    key, private_key_readable = essentials.keys_unlock(private_key_readable)

print('Number of arguments: %d arguments.' % len(sys.argv))
print('Argument List: %s' % ', '.join(sys.argv))

# get balance

# include mempool fees
mempool = sqlite3.connect('mempool.db')
mempool.text_factory = str
m = mempool.cursor()
m.execute("SELECT count(amount), sum(amount) FROM transactions WHERE address = ?;", (address,))
result = m.fetchall()[0]
if result[1] != None:
    debit_mempool = float('%.8f' % (float(result[1]) + float(result[1]) * 0.001 + int(result[0]) * 0.01))
else:
    debit_mempool = 0
# include mempool fees

if full_ledger == 1:
    conn = sqlite3.connect(ledger_path)
else:
    conn = sqlite3.connect(hyper_path)
conn.text_factory = str
c = conn.cursor()

s = socks.socksocket()
s.settimeout(10)
s.connect(("127.0.0.1", 5658))
#s.connect(("127.0.0.1", 3030))

connections.send (s, "balanceget", 10)
connections.send (s, address, 10)  # change address here to view other people's transactions
stats_account = connections.receive (s, 10)
balance = stats_account[0]
#credit = stats_account[1]
#debit = stats_account[2]
#fees = stats_account[3]
#rewards = stats_account[4]


print("Transction address: %s" % address)
print("Transction address balance: %s" % balance)

# get balance
def address_validate(address):
    if re.match ('[abcdef0123456789]{56}', address):
        return True
    else:
        return False

try:
    amount_input = sys.argv[1]
except IndexError:
    amount_input = input("Amount: ")

try:
    recipient_input = sys.argv[2]
except IndexError:
    recipient_input = input("Recipient: ")

if not address_validate(recipient_input):
    print("Wrong address format")
    exit(1)

try:
    operation_input = sys.argv[3]
except IndexError:
    operation_input = 0

try:
    openfield_input = sys.argv[4]
except IndexError:
    openfield_input = input("Enter openfield data (message): ")


# hardfork fee display
fee = fee_calculate(openfield_input)
print("Fee: %s" % fee)

confirm = input("Confirm (y/n): ")

if confirm != 'y':
    print("Transaction cancelled, user confirmation failed")
    exit(1)

# hardfork fee display
try:
    float(amount_input)
    is_float = 1
except ValueError:
    is_float = 0
    exit(1)

if len(str(recipient_input)) != 56:
    print("Wrong address length")
else:
    timestamp = '%.2f' % time.time()
    transaction = (str(timestamp), str(address), str(recipient_input), '%.8f' % float(amount_input), str(operation_input), str(openfield_input))  # this is signed
    # print transaction

    h = SHA.new(str(transaction).encode("utf-8"))
    signer = PKCS1_v1_5.new(key)
    signature = signer.sign(h)
    signature_enc = base64.b64encode(signature)
    txid = signature_enc[:56]

    print("Encoded Signature: %s" % signature_enc.decode("utf-8"))
    print("Transaction ID: %s" % txid.decode("utf-8"))

    verifier = PKCS1_v1_5.new(key)

    if verifier.verify(h, signature):
        if float(amount_input) < 0:
            print("Signature OK, but cannot use negative amounts")

        elif float(amount_input) + float(fee) > float(balance):
            print("Mempool: Sending more than owned")

        else:
            tx_submit = (str (timestamp), str (address), str (recipient_input), '%.8f' % float (amount_input), str (signature_enc.decode ("utf-8")), str (public_key_hashed.decode("utf-8")), str (operation_input), str (openfield_input))
            while True:
                connections.send (s, "mpinsert", 10)
                connections.send (s, tx_submit, 10)
                reply = connections.receive (s, 10)
                print ("Client: {}".format (reply))
                break
    else:
        print("Invalid signature")
        # enter transaction end

s.close()
