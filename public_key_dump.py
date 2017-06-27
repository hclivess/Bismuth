from Crypto.PublicKey import RSA
import hashlib

key = RSA.importKey(open('privkey.der'.encode("utf-8")).read())

public_key = key.publickey()
private_key_readable = key.exportKey().decode("utf-8")
public_key_readable = key.publickey().exportKey().decode("utf-8")
address = hashlib.sha224(public_key_readable.encode("utf-8")).hexdigest()  # hashed public key

print (private_key_readable)
print (public_key_readable)
print (address)






