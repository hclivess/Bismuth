# check_tx.py

A Demo script that takes a transaction id as input, and sends back a json with it's status
- Unknown
- In mempool  
  (shows timestamp, from, to, amount, first 50 chars of openfield)
- In ledger  
  (shows timestamp, from, to, amount, first 50 chars of openfield as well as number of confirmations)

## How to run

This script is supposed to be run in the node directory, it needs access to both `ledger.db` and `mempool.db`.  
It takes the ledger path from the default config.txt file.

# Output examples

Here are a few sample outputs.  
(Imaginary data)

## Unknown Txid

`python3 check_tx.py bZ8/XjQhKX4GmlVmettD8H1+Tdh+FG8zXfghfdgh`


```
{"Status": "Unknown", "TxId": "bZ8/XjQhKX4GmlVmettD8H1+Tdh+FG8zXfghfdgh"}
```

## Tx in Mempool

`python3 check_tx.py anotheroneinthepool`

```
{"Amount": 0, "Timestamp": 1517594747.07, "Address": "371a2a76a527d0a45aac441fc3170a9e609e59abd134aa4bca726211", "Recipient": "371a2a76a527d0a45aac441fc3170a9e609e59abd134aa4bca726211", "Block": 498059, "Openfield": "a9443a88b04834e8001ddc3569491d31a2b9c61765f99d1c62", "TxId": "bZ8/XjQhKX4GmlVmettD8H1+Tdh+FG8zX", "Status": "Mempool"}
```

## Confirmed Tx, in ledger


`python3 check_tx.py bZ8/XjQhKX4GmlVmettD8H1+Tdh+FG8zX`

```
{"Amount": 0, "Confirmations": 2, "Timestamp": 1517594747.07, "Address": "371a2a76a527d0a45aac441fc3170a9e609e59abd134aa4bca726211", "Recipient": "371a2a76a527d0a45aac441fc3170a9e609e59abd134aa4bca726211", "Block": 498059, "Openfield": "a9443a88b04834e8001ddc3569491d31a2b9c61765f99d1c62", "TxId": "bZ8/XjQhKX4GmlVmettD8H1+Tdh+FG8zX", "Status": "Confirmed"}
```
