from Crypto.Signature import PKCS1_v1_5
from Crypto.Hash import SHA
from Crypto.PublicKey import RSA

message = 'To be signed'

key = RSA.importKey(open('privkey.der').read())
h = SHA.new(message)
print message
signer = PKCS1_v1_5.new(key)
print signer
signature = signer.sign(h)
print signature


key = RSA.importKey(open('pubkey.der').read())
h = SHA.new(message)
verifier = PKCS1_v1_5.new(key)
if verifier.verify(h, signature):
   print "The signature is authentic."
else:
   print "The signature is not authentic."
