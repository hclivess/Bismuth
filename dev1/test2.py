import sqlite3
received_signature_enc = "BZSmAVHsnjGZfADLSqALt2fahp1RRxOnTiPfX6e6oHtvSpoc8Nrn+mTyG6I7Py3a+igB+VKS37ExKcc1z6QHqqjZmYHBTXJ5KsDx6HvG9bQSqLYGVvaCakpZVuueCKTF/Jr5th3snTFQUYISlBMR9Y8ZYFL8vm3ufdphWTwd74c="
conn = sqlite3.connect('ledger.db')
c = conn.cursor()

print "verifying duplicity"
c.execute("SELECT signature FROM transactions WHERE signature = '"+received_signature_enc+"'")
try:
    c.fetchone()[0]
    duplicate = 1
    print "Duplicate transaciton"
except:
    duplicate = 0
    print "Not a duplicate"
