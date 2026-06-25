Known issue: Optipoolware will not stop execution with normal ctr-c in windows - use alt-f4 instead

Note: The reference miner software for the pool (optihash.py) is now contained in the folder called optihash

# optipoolware.py

This needs to be placed in the Bismuth folder.

It uses a new custom database that has the same name as the poolware_dappie.py database but has additional table information so don't confuse the two.

node.py (i.e. Bismuth node) needs to be running.

Port 8525 needs to open in the default setting or you can change the port by typing the port number as an arguement on startup
If you change the port please make sure you let your miners know so they can change their settings !!

The pool share difficulty is set in pool.txt and is static - no percentage etc.

Optipoolware.py records hash rate and worker name information

Autopayout runs every hour - this has not been fully tested. The minimum payout can be set in pool.txt

Pool fee can be set as a percentage in pool.txt e.g. for 1% fee just enter 1 in the appropriate line

Also a fee for an alternate address (dev, charity, your friend) can be set in pool.txt (alt_fee)

Alt_add is the alternate address suggested above - this can also be uses as the default share address so no share is wasted if a miner sends a bad address

pool.txt (also used for optiexplorer.py)

mine_diff= pool share difficulty

min_payout= minimum payout for autopayout function

pool_fee= pool fee percentage

alt_fee= alternative address fee

alt_add= alternative address to send the alt_fee to

worker_time= how often the pool checks diff and blockhash to be mined in seconds

m_timeout= if a miner does not send a share within this many minutes the hashrate will be reduced / set to zero

# optiexplorer.py

This is a reference pool web interface that displays stats and block information for miners. It uses Flask run over tornado as a microframework

# optihash.py

This is the stand alone to be used against optipoolware.py only.
It has no dependency on node.py and relies only on connections.py which have been copied as is from the Bismuth repository
miner.txt contains the information needed to mine against the pool

port=8525 or port presented by the pool

mining_ip= ip address of the pool

mining_threads= number of mining threads to be used by the miner

miner_address= miners bismuth address

nonce_time= time in seconds you wish optihash to mine between getting new work from the pool

max_diff= the maximum difficulty you wish you miner to work at

miner_name= Base name of each worker, this name will be appended with the thread number to give a name for each worker

hashcount= this is used to calculate the size of nonce array to be used in a single hashing cycle. A typical size is 20000 (to give an array of 200000 nonces)

# How it Works

The miner starts and connects to pool ip or hostname
It requests the block hash, diff and pool address to find from the pool
It then hashes as normal using the pool address for the mining hash
Once a nonce is found at the required difficulty it is timestamped and sent to the pool togther with the miners address for processing
The pool receives the nonce and if it meets the network difficulty, validates it and then processes the mempool, creates and signs the transaction block
The block is then transmitted to all nodes from the pool.

# Filelist

Place in and run from bismuth folder

optipoolware.py, optiexplorer.py and pool.txt

Place and run from any suitable folder on miner - node is not needed (but a bismuth address is!)

optihash.py, miner.txt, connections.py

Windows 64bit and Linux Ubunut 16.04 LTS executables are provided in releases.
