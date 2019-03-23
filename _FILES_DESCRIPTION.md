# Bismuth REPO files description

This repo will likely go through some refactoring, but in the mean time here is an alphabetic list of all .py files of the main dir, so you know where to look.

Each file maybe a *module* (library of functions, to be included but not run) or a *script* (to be run).

See also the most useful files: https://github.com/EggPool/Bismuth/blob/patch-51/_MOST_USEFUL_FILES.md
Some files have been moved to https://github.com/hclivess/BismuthProjects

## aliases.py (*module*)
Handles the aliases index (ie: labels for bismuth addresses)

## apihandler.py (*module*)
Class to handle extra API commands, that are not part of the core nodes protocol.  
Used by Json-RPC server and other third party interaction.  
Prefered entry point for adding new generic features.

## apidirs.py (*module*)
To be used by GUI wallet.  Finds local user storage location.

## balance_nogui.py (*script*)
Get balance from the command line.  
Outdated, still uses privkey.der/pubkey.der instead or modern wallet.der

## bisurl.py (*module*)
bisurl encoding and decoding functions.  
A bisurl is a single string that encodes recipient, amount, data and a checksum into a single url like string, to be used in the wallet.  
Exchanges could use this feature to give each user a bis url for deposit. The bis url would include the required custom data field, and the user would only have to adjust the required amount.

## classes.py (*module*)
Initialization of classes commonly used by node (Node, Client, Logger, Keys), which are then populated by the node code.

## commands.py  (*script*)
Command line interface to node commands. Undocumented.
Better refer to the dedicated API repo. 

## connections.py (*module*)
Low level protocol functions, header + json over raw socket.  
Len of payload, decimal, on 10 chars, followed by json encoded data. See API repo.

## connectionmanager.py (*module*)
Connection manager thread module. Reports status to output.

## compile_nuitka.cmd (*script*)
Compiles executables for Windows. Nuitka is cross-platform, however.

## dbhandler.py (*module*)
Standard database handler class for initiation and closing of database connections per thread, includes error handling.

## difficulty.py (*module*)
Difficulty calculation.

## digest.py (*module*)
Block processing.

## essentials.py (*module*)
Common helpers. Some DB related, mainly crypto helpers and common abstract operations.

## fork.py (*module*)
Rules for upcoming hardforks should be stored here.

## genesis.py (*script*)
Do not use.  
Helper script to create a new genesis block, from an empty chain. Not maintained.

## hmac_drbg.py (*module*)
HMAC_DRBG (sha512) helper class, used by the "heavy3" PoW algorithm.

## hyperlane_asyncio.py (*module*)
Module that handles hyperlane integration to node.

## keygen.py (*script*)
Sample script to generate a new address.

## keys.py (*module*)
Helpers to generate and read wallet.der (privkey, pubkey, address)

## ledger_explorer.py (*script*)
Early version of a block explorer. Not maintained.

## log.py (*module*)
Logging - console, file - helpers.

## lwbench.py (*module*) (wallet)
Benchmark Helper for wallet.

## mempool.py (*module*)
Mempool module for Bismuth nodes

## mining.py (*module*)
Legacy mining algo: block validity check.

## mining_heavy3.py (*module*)
Heavy3 mining algo: block validity check.

## node.py (*script*)
Main script to run a bismuth node.

## options.py (*module*)
Config helper. Beware: imported as "config" in the node.

## peershandler.py (*module*)
Peers handler class.

## plugins.py (*module*)
Plugin manager class for nodes. See the plugins repo for usage and sample plugins.

## process_search.py (*module*)
Helper to test for a process presence.

## quantizer.py (*module*)
Helpers. Workaround for float rounding.

## recovery.py (*module*)
Helper. Recovers pubkey and address from privkey alone.

## regnet.py (*module*)
Regnet mode for bismuth testing.

## rewards_reindex.py  (*script*)
System script to reindex dev rewards, manual block level input. Do not use.

## rewards_test.py  (*script*)
Tests if there is a dev reward every 10 blocks. Do not use.

## rollback.py (*module*)
Helper. rollbacks indexes on block rollback.

## send_nogui.py  (*script*)
Command line script to send a transaction.  
Can be used as base for automated payment scripts.  
Does **not** send anything sensitive to the node.

## send_nogui_noconf.py  (*script*)
As send_nogui.py, without confirmation.

## setup_nuitka.iss (*script*)
Windows installer generator for Inno Setup when compiled with nuitka.

## simplecrypt.py (*module*)
Helper. Handles wallet encryption/decryption with password.

## staking.py (*module*)
Helper. Proof of concept for offline staking.

## tokensv2.py (*module*)
Tokens functions.

## wallet.py  (*script*)
Bundled/Legacy GUI Wallet.

## wallet_async.py  (*script*)
Proof of concept asyncio GUI wallet.

## worker.py (*module*)
Outgoing traffic thread node module.


