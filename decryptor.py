from simplecrypt import decrypt
import base64
import hashlib
from Cryptodome.PublicKey import RSA

try:
    with open("secret.txt", "r") as encrypted_privkey_file:
        encrypted_privkey = encrypted_privkey_file.read()

    password = input("Password: ")
    print ("Your password is: {}".format(password))
    decrypted_privkey = decrypt (password, base64.b64decode (encrypted_privkey)).decode()
    print("decrypted_privkey: \n",decrypted_privkey)

    key = RSA.importKey(decrypted_privkey)

    public_key_readable = key.publickey ().exportKey ().decode ("utf-8")
    print("public_key_readable: \n",public_key_readable)

    address = hashlib.sha224 (public_key_readable.encode ("utf-8")).hexdigest ()
    print("address: \n",address)

except Exception as e:
    print(e)

input("Press any key to continue...")