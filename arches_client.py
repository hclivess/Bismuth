import base64, hashlib, socket, pyping, os, getpass, time, psutil, log, sqlite3
from Crypto.PublicKey import RSA
from Crypto.Signature import PKCS1_v1_5
from Crypto.Hash import SHA
from simplecrypt import decrypt

app_log = log.log("arches.log")

# import keys
if not os.path.exists('privkey_encrypted.der'):
    password = ""
    key = RSA.importKey(open('privkey.der').read())
    private_key_readable = str(key.exportKey())
    # public_key = key.publickey()
else:
    password = getpass.getpass()
    encrypted_privkey = open('privkey_encrypted.der').read()
    decrypted_privkey = decrypt(password, base64.b64decode(encrypted_privkey))
    key = RSA.importKey(decrypted_privkey)  # be able to sign
    private_key_readable = str(key.exportKey())

public_key_readable = open('pubkey.der').read()
public_key_hashed = base64.b64encode(public_key_readable)
address = hashlib.sha224(public_key_readable).hexdigest()
# import keys

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