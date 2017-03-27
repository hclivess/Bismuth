from Crypto.PublicKey import RSA
from Crypto.Signature import PKCS1_v1_5
from Crypto.Hash import SHA
from simplecrypt import decrypt

import base64, hashlib, socket, pyping, os, getpass, time

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

openfield = ((socket.gethostname()), r.ret_code, r.destination, r.max_rtt, r.avg_rtt, r.min_rtt, r.destination_ip)
print openfield

transaction = (timestamp, address, address, '%.8f' % 0, 0, openfield)  # this is signed
print transaction

h = SHA.new(str(transaction))
signer = PKCS1_v1_5.new(key)
signature = signer.sign(h)
signature_enc = base64.b64encode(signature)
app_log.info("Client: Encoded Signature: " + str(signature_enc))