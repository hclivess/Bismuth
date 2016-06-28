Bismuth Whitepaper
=======
![GitHub Logo](/graphics/logo.gif)

*Expanded with technical specifications*
No limits, easy integration, endless possibilities

###Introduction:
Welcome to Bismuth, a digital distributed self-regulating database system whose primary application is currency, 
and its first application is mining. Bismuth is not based on code of BTC or any of it's derivates, it is only inspired
by some ideas laid down by Andreas M. Antonopoulos, Satoshi Nakamoto (BitCoin), Sunny King (Peercoin), NXT and
ETH developers.

Bismuth does not draw any code from other repositories, instead it reformulates the cryptocurrency code in its own terms to be easily 
readable, compatible across all platforms, integrated into business solutions with utmost ease and most importantly open for
development to wide public through it's simplicity, while minimizing the security risk for custom code implementations.

###Specifications
Bismuth vastly differs from the mentioned systems in many ways and some concepts are very different, in pursuit of improved
performance and removal of deliberate, non-technical limitations like the block size. The main difference is that Bismuth does not
differ between a block and a transactions and regards them as synonymous. This means that coins can be spend instantly, because the 
transaction time is equal to zero combined with physical limitations of the network infrastructure.

The risk of network overload resulting from this feature is eliminated by flexible fees. With the growing network usage, the fees
also rise. However when the network is not used extensively, fees are almost non-existent. The fees are currently burned, to stimulate
energy expenditure in real-life systems and eliminate issues associated with POS mechanisms (hoarding). The risk of double spending is
mitigated through the longest chain rule, and every transaction is checked against balance. Unlike the sophisticated input/output 
system, Bismuth uses a simple balance addition, substraction and comparison.

One of the improvements is derived from Bitcoin's issues with the memory pool overload, which is capable of crashing the client.
Bismuth uses a database (mempool.db) for this purpose, instead of storing incoming transactions in RAM. This enables
Bismuth to run on low-end devices, since price of storage is significantly lower than that of RAM and does not need to impact system
performance directly. When a fork occurs on the network and a different node is selected based on the longest chain rule, backup.db is
used to backup local transactions in order to reinsert them later.

There are currently three database files, ledger.db, mempool.db, and backup.db, they differ in processing priority in descending order
to maintain efficiency of the network. System chosen for databases is the sqlite3, default for every Python installation. It is ready
on-the-fly and easily accessible through third party tools or the blockchain explorer, which is included with Bismuth. Also, sqlite3
will make sidechain, dapp and megablock implementation very easy in the future.

There is a central transaction processing core in every node, which handles transaction verification and synchronization and makes
this system robust and reliable. However, there are multiple independent connectors in the peer to peer system, based on whether 
connections are incoming or outgoing and whether the node is active or passive. Active nodes are able to receive and send requests, 
while passive nodes are only capable of sending.

Another great feature for third party integration are the socket functions. This makes up for both Bitcoin's RPC and custom APIs
through unified and generic features on the fly in any programming language capable of handling sockets. The format is easily readable
to humans.

Author of this paper believes that the main success of Bitcoin is due to mining, but due to social implications and not the technical 
ones. Mining or Proof of Work corresponds is Bitcoin's first "killer app", because it basically represents a decentralized lottery.
Electrical power is used as input, exchanged for a chance of reward. For this reason, mining is included in Bismuth, based on the
following rules: There is a chance of one reward to be mined every certain number of blocks (i.e. 50). This can be achieved by 
matching a part of the transaction hash with client's own address, similar to Bitcoin. Since the transaction hash includes a signature
in it and changing variables like timestamp or amount are used, hash must be different on each minimum time unit. Iterating hashes
until a match is found leads to successful mining. A miner is included with Bismuth (miner.py/miner.exe)

###Future Development
*Listed by simplest to implement, top to bottom*

1. Message signing. The GUI is in place, but not working as of yet.

2. Bismuth's implementation of decentralized data and applications will be handled through technology which goes under the name
OpenField. For this purpose, an extra column will be created in the database with arbitrary user data. Then it will be decided whether
an external framework will be developed for handling this data or if it happens to be implemented to the core.

3. Compression mechanisms developed for Bismuth will make it even more efficient. Technology coined Extreme Blockchain Compression 
(EBC), which uses database references of repeated data, is capable of reducing the database size by more than 60%.

4. Sidechains and megablocks will increase scalability thousandfolds. Sidechains will be held in separate databases, allowing users to pick which one they would like to work with. This also adds an opportunity to create private blockchains. 

5. Megablocks can be understood as decentralized checkpoints. Based on predefined rules, all nodes will sum up balances once a certain
block height occurs. Most entries in the blockchain preceeding the checkpoint will become irrelevant and removable.

###Technical details

* System: Proof of transaction
* Hashing algorithm: SHA224
* Signing algorithm: PKCS1_v1_5, base64
* Block size: ~650 bytes
* Compressed block size: ~250 bytes
* Mining reward: 25 units/50 blocks
* Confirmations before respending: 0 (user decides)
* Default P2P port:
    * Client: Random
    * Node: 2829
* Plaintext peerlist file

###License
This program is free software; you can redistribute it and/or
modify it under the terms of the GNU General Public License
as published by the Free Software Foundation; either version 2
of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program; if not, write to the Free Software
Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.
