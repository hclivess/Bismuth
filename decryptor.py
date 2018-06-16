from simplecrypt import decrypt
import base64
import hashlib
from Crypto.PublicKey import RSA

password = input("Password: ")
encrypted_privkey = input("Encrypted private key: ")
decrypted_privkey = decrypt (password, base64.b64decode (encrypted_privkey)).decode()


key = RSA.importKey(decrypted_privkey)
public_key_readable = key.publickey ().exportKey ().decode ("utf-8")
address = hashlib.sha224 (public_key_readable.encode ("utf-8")).hexdigest ()

print("decrypted_privkey: \n",decrypted_privkey)
print("public_key_readable: \n",public_key_readable)
print("address: \n",address)

input("Press any key to continue...")