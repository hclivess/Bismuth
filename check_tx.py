"""
check_tx.py

A Demo script that takes a transaction id as input, and sends back a json with it's status
- unknown
- in mempool (shows timestamp, from, to, amount, first 50 chars of openfield)
- in ledger (shows timestamp, from, to, amount, first 50 chars of openfield as well as number of confirmations)

This script is supposed to be run in the node directory, it needs access to both ledger.db and mempool.db
"""

import sqlite3, sys, json
import options

# Default ledger path
ledger_path = "static/ledger.db"

# Default mempool path
mempool_path = "mempool.db"


def list_to_tx(result):
	"""
	Converts the query result into a dict
	"""
	result[4] = result[4][:50]
	keys = ["Timestamp", "Address", "Recipient", "Amount", "Openfield", "Block"]
	return dict(zip(keys,result))


def is_in_mempool(txid):
	"""
	If txid is in mempool, sends back details of the tx
	"""
	mempool = sqlite3.connect(mempool_path)
	mempool.text_factory = str
	m = mempool.cursor()
	m.execute("SELECT timestamp, address, recipient, amount, openfield FROM transactions WHERE signature like ?;", (txid+"%",))
	result = m.fetchone()
	if result:
		return (True, list_to_tx(list(result)))
	else:
		return (False, None)


def is_in_ledger(txid):
	"""
	If txid is in ledger, sends back details of the tx and number of confirmations
	"""
	ledger = sqlite3.connect(ledger_path)
	ledger.text_factory = str
	m = ledger.cursor()
	m.execute("SELECT timestamp, address, recipient, amount, openfield, block_height FROM transactions WHERE signature like ?;", (txid+"%",))
	result = m.fetchone()
	if result:
		m.execute("SELECT block_height FROM transactions ORDER BY block_height desc LIMIT 1")
		last = m.fetchone()
		return (True, list_to_tx(list(result)), last[0])
	else:
		return (False, None, None)


if __name__ == "__main__":
	if len(sys.argv) != 2:
		txid = input("No argument detected, please insert command manually\n")
	else:
		txid = sys.argv[1]

	res = {"TxId":txid, "Status":"Unknown"}
	
	isit, details = is_in_mempool(txid)
	if isit:
		res["Status"] = "Mempool"
		res.update(details)
	
	isit, details, lastblock = is_in_ledger(txid)
	if isit:
		res["Status"] = "Confirmed"
		res.update(details)
		res["Confirmations"] = lastblock - res["Block"]
	
	print(json.dumps(res))
