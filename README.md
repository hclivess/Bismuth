# thincoin
looking for alternatives to blockchain technology

WARNING: This project is in the Proof of Concept stage!

Dependencies:

-pycrypto

-python 2.7 or newer

ThinCoin (codenames "stepchain", "bling") is a step transaction monetary vehicle, which implies serial transactions, one transaction per one blockchain entry, unlike Bitcoin's parallel txs. There is no mining. All tokens originate from the genesis wallet. This increases simplicity of the code, for now, but also removes the transaction confirmation process. I estimate this to have a negative impact on scalability because one block(step) can only contain one transaction, but no testing has been done yet. The chain is secured by incremental transaction numbers for synchronization, public key sharing, cryptographic signatures, signature verifications. 

-Addresses are public keys hashed into sha256 (for now there is a duplicate entry in the db basically).  

-The database is a simple sqlite3 implementation and you can actually view it yourself using a db explorer. 

Work in progress:
You may attempt to change the chain, but the nodes perform verification based on the genesis address, verifying every outgoing transactions and checking for your current balance. The security issues arising from this are solved by the stepchain implementation. You cannot make transactions from addresses which are not yours, because a private key is required for signature.

Potential issues:
ThinCoin uses eval_literal, which is more secure than eval (which is directly broken), but security testing needs to be carried out.

Components:
client.py - client application, which tries to connect to the server defined in the peer file, used to generate an address (address.txt)
server.py - active listener, which waits for messages (transactions) from client and confirms them, also handles the blockchain
