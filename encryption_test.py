import sqlite3,base64,keys,ast
from Crypto.PublicKey import RSA
(key, private_key_readable, public_key_readable, public_key_hashed, address) = keys.read() #import keys

conn = sqlite3.connect('static/ledger.db')
conn.text_factory = str
c = conn.cursor()

c.execute("SELECT public_key FROM transactions WHERE address = ? and reward = 0", (address,))

target_public_key_hashed = c.fetchone()[0]
target_public_key = RSA.importKey(base64.b64decode(target_public_key_hashed).decode("utf-8"))

string = "test"
string = str(target_public_key.encrypt(string.encode("utf-8"), 32))
print(string)

print (key.decrypt(ast.literal_eval(string)))