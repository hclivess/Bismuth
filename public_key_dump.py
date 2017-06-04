from Crypto.PublicKey import RSA
import hashlib, base64

key = RSA.importKey(open('privkey.der').read())
public_key = key.publickey()
private_key_readable = str(key.exportKey())
public_key_readable = str(key.publickey().exportKey())
address = hashlib.sha224(base64.b64encode(public_key_readable)).hexdigest()

print private_key_readable
print public_key_readable
print address






