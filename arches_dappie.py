import sqlite3
import keys

_, _, _, _, address = keys.read()

ledger = sqlite3.connect('static/ledger.db')
ledger.text_factory = str
cursor = None
try:
    cursor = ledger.cursor()
    ledger.execute('SELECT * FROM transactions WHERE recipient = ? AND openfield LIKE ? ORDER BY block_height DESC, timestamp DESC LIMIT 100;', (address,) + ('%' + "Archies:" + '%',))  # should work, needs testing
    result_payouts = ledger.fetchall()
    print(result_payouts)
except Exception as e:
    print(type(e), e)
finally:
    if cursor is not None:
        cursor.close()
    ledger.close()
