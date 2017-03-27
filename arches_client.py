import base64, hashlib, socket, pyping, os, getpass, time, psutil, log, sqlite3, keys
from Crypto.PublicKey import RSA
from Crypto.Signature import PKCS1_v1_5
from Crypto.Hash import SHA
from simplecrypt import decrypt

app_log = log.log("arches.log")

(key, private_key_readable, public_key_readable, public_key_hashed, address) = keys.read()

timestamp = str(time.time())

r = pyping.ping('google.com')                # Need to be root or

openfield = "Arches:" + socket.gethostname(), psutil.virtual_memory(), psutil.cpu_times(), r.ret_code, r.destination, r.max_rtt, r.avg_rtt, r.min_rtt, r.destination_ip
print openfield

transaction = (timestamp, address, address, '%.8f' % 0, 0, str(openfield))  # this is signed
print transaction

h = SHA.new(str(transaction))
signer = PKCS1_v1_5.new(key)
signature = signer.sign(h)
signature_enc = base64.b64encode(signature)
#app_log.info("Client: Encoded Signature: " + str(signature_enc))

mempool = sqlite3.connect('mempool.db')
mempool.text_factory = str
m = mempool.cursor()

m.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?)", (timestamp, address, address, '%.8f' % 0, signature_enc, public_key_hashed, "0", str(openfield)))
mempool.commit()  # Save (commit) the changes
mempool.close()

app_log.info("Arches: Mempool updated with a transaction")