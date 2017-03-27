from Crypto.PublicKey import RSA
from Crypto.Signature import PKCS1_v1_5
from Crypto.Hash import SHA

import base64, hashlib, socket, pyping, os


# import keys
if not os.path.exists('privkey_encrypted.der'):
    key = RSA.importKey(open('privkey.der').read())
    private_key_readable = str(key.exportKey())
    #public_key = key.publickey()
    encrypted = 0
    unlocked = 1
else:
    encrypted = 1
    unlocked = 0

#public_key_readable = str(key.publickey().exportKey())
public_key_readable = open('pubkey.der').read()
public_key_hashed = base64.b64encode(public_key_readable)
address = hashlib.sha224(public_key_readable).hexdigest()
#import keys

r = pyping.ping('google.com')                # Need to be root or
r = pyping.ping('google.com', udp = False)    # But it's udp, not real icmp
r.ret_code

openfield = ((socket.gethostname()), r.destination, r.max_rtt, r.avg_rtt, r.min_rtt, r.destination_ip)
print openfield

timestamp = str(time.time())


transaction = (timestamp, address, recipient_input, '%.8f' % float(amount_input), keep_input, openfield_input)  # this is signed
# print transaction

h = SHA.new(str(transaction))
signer = PKCS1_v1_5.new(key)
signature = signer.sign(h)
signature_enc = base64.b64encode(signature)
app_log.info("Client: Encoded Signature: " + str(signature_enc))