# hopium
looking for alternatives to blockchain technology

development plan:

enable longest chain rule though regression/rollbacks to latest known hash (now that txhash is in place)
enable transaction fees to prevent spam
nodes (not only clients) should send out transactions to prevent forks and rollbacks
maybe convert node communication to one channel

WARNING: This project is in the Proof of Concept stage!

Dependencies:

-pycrypto

-python 2.7 or newer

-Addresses are public keys hashed into sha256. 

-The database is a simple sqlite3 implementation and you can actually view it yourself using a db explorer. 

Work in progress:
You may attempt to change the chain, but the nodes perform verification based on the genesis address, verifying every outgoing transactions and checking for your current balance. The security issues arising from this are solved by the stepchain implementation. You cannot make transactions from addresses which are not yours, because a private key is required for signature.

Potential issues:
ThinCoin uses eval_literal, which is more secure than eval (which is directly broken), but security testing needs to be carried out.

Components:
client.py - client application, which tries to connect to the server defined in the peer file, used to generate an address (address.txt)
server.py - active listener, which waits for messages (transactions) from client and confirms them, also handles the blockchain

Communication structure:
Node startup: Add self to peers if port is publicly accessible

Client -> Node: Hello, please send peers

Node -> Client: Here you go, my whole peerlist

Client -> Node: My latest block is xx, what is yours?

Node -> Client: My latest block is xx+n, sending the missing blocks


Client -> Node: Thanks, I'll need to verify that (verifies, saves)

Client -> Node: I want to send this amount of tokens to that address, this is my signature

Node -> Client: I'll need to verify balance and transaction signature before adding this to the blockchain (verifies, saves)

Client goes to the next node and repeats
