# thincoin
looking for alternatives to blockchain technology

thincoin is basically a working encrypted communication tool now, which works in a decentralized manner, but has no mashnet yet, communication is managed p2p between open IP addresses

For it to run successfully, you will need Python installed (developed in 2.7), all modules should be included by default, except for Crypto.Hash (pycrypto)

generate.py - generates key pairs if you don't have them yet and saves them to a file
client.py - client application, which tries to connect to the server defined in the peer file
server.py - active listener, which waits messages (transactions) from client and confirms them

Warning: I am not very skilled in object programming and would like to keep this as simple as possible
