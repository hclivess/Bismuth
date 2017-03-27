from Crypto.PublicKey import RSA
from Crypto.Signature import PKCS1_v1_5
from Crypto.Hash import SHA

import base64, socket, pyping, keys

private_key_readable = keys.read()[0]
encrypted = keys.read()[1]
unlocked = keys.read()[2]
address = keys.read()[3]
public_key_readable = keys.read()[4]
public_key_hashed = keys.read()[5]

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