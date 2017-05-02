#this file takes optional arguments, arg1 = amount to spend, arg2 = recipient address, arg3 = keep forever (0/1), arg4=OpenField data
#args3+4 are not prompted if ran without args

from Crypto.Signature import PKCS1_v1_5
from Crypto.Hash import SHA
import base64, time, sqlite3, keys, sys

(key, private_key_readable, public_key_readable, public_key_hashed, address) = keys.read()

print 'Number of arguments:', len(sys.argv), 'arguments.'
print 'Argument List:', str(sys.argv)

#get balance
mempool = sqlite3.connect('mempool.db')
mempool.text_factory = str
m = mempool.cursor()
m.execute("SELECT sum(amount) FROM transactions WHERE address = ?;", (address,))
debit_mempool = m.fetchone()[0]
mempool.close()
if debit_mempool == None:
    debit_mempool = 0

conn = sqlite3.connect('static/ledger.db')
conn.text_factory = str
c = conn.cursor()
c.execute("SELECT sum(amount) FROM transactions WHERE recipient = ?;", (address,))
credit = c.fetchone()[0]
c.execute("SELECT sum(amount) FROM transactions WHERE address = ?;", (address,))
debit = c.fetchone()[0]
c.execute("SELECT sum(fee) FROM transactions WHERE address = ?;", (address,))
fees = c.fetchone()[0]
c.execute("SELECT sum(reward) FROM transactions WHERE address = ?;", (address,))
rewards = c.fetchone()[0]
c.execute("SELECT MAX(block_height) FROM transactions")
bl_height = c.fetchone()[0]

if debit == None:
    debit = 0
if fees == None:
    fees = 0
if rewards == None:
    rewards = 0
if credit == None:
    credit = 0
balance = credit - debit - fees + rewards - debit_mempool
print("Transction address balance: {}".format(balance))
#get balance

try:
    amount_input = sys.argv[1]
except:
    amount_input = raw_input("Amount: ")

try:
    recipient_input = sys.argv[2]
except:
    recipient_input = raw_input("Recipient: ")


try:
    float(amount_input)
    is_float = 1
except:
    is_float = 0
    pass

if is_float != 1:
    print("Invalid amount")

elif len(str(recipient_input)) != 56:
    print("Wrong address length")

else:

    try:
        keep_input = sys.argv[3]
    except:
        keep_input = 0
    try:
        openfield_input = sys.argv[4]
    except:
        openfield_input = ""

    timestamp = '%.2f' % time.time()
    transaction = (timestamp, address, recipient_input, '%.8f' % float(amount_input), keep_input, openfield_input) #this is signed
    #print transaction

    h = SHA.new(str(transaction))
    signer = PKCS1_v1_5.new(key)
    signature = signer.sign(h)
    signature_enc = base64.b64encode(signature)
    print("Encoded Signature: {}".format(signature_enc))

    verifier = PKCS1_v1_5.new(key)
    if verifier.verify(h, signature) == True:
        if float(amount_input) < 0:
            print("Signature OK, but cannot use negative amounts")

        elif (float(amount_input) > float(balance)):
            print("Mempool: Sending more than owned")

        else:
            print("The signature is valid, proceeding to save transaction to mempool")

            mempool = sqlite3.connect('mempool.db')
            mempool.text_factory = str
            m = mempool.cursor()

            m.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?)",(timestamp, address, recipient_input, '%.8f' % float(amount_input),signature_enc, public_key_hashed, keep_input, openfield_input))
            mempool.commit()  # Save (commit) the changes
            mempool.close()
            print("Mempool updated with a received transaction")
            #refresh() experimentally disabled
    else:
        print("Invalid signature")
    #enter transaction end
