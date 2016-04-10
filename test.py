from Crypto.Signature import PKCS1_v1_5
from Crypto.Hash import SHA
from Crypto.PublicKey import RSA
import base64

message = 'To be signed'
key = RSA.importKey(open('privkey.der').read())
h = SHA.new(message)
signer = PKCS1_v1_5.new(key)
signature = signer.sign(h)
signature_enc = str(base64.b64encode(signature))
#print signature_enc


signature_dec = str(base64.b64decode (signature_enc))
#print sugnature_dec
key = RSA.importKey(open('pubkey.der').read())
h = SHA.new(message)
verifier = PKCS1_v1_5.new(key)
if verifier.verify(h, signature_dec):
   print "The signature is authentic."
else:
   print "The signature is not authentic."
