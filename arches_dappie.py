import sqlite3, base64, getpass, hashlib, os
from simplecrypt import decrypt
from Crypto.PublicKey import RSA

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

conn = sqlite3.connect('static/ledger.db')
conn.text_factory = str
c = conn.cursor()

c.execute('SELECT * FROM transactions WHERE recipient = ? AND openfield LIKE ? ORDER BY block_height DESC, timestamp DESC LIMIT 100;', (address,) + ('%' + "Archies:" + '%',))  # should work, needs testing
result_payouts = c.fetchall()

print result_payouts