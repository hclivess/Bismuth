# Bismuth REPO files description

This repo will likely go through some refactoring, but in the mean time here is an alphabetic list of all .py files of the main dir, so you know where to look.

Each file maybe a *module* (library of functions, to be included byt not run) or a *script* (to be run)

## aliases.py (*module*)
Handles the aliases index (ie: labels for bismuth addresses)

## ann.py (*module*)
Extract the latest announce and version from the chain.

## anon_dappie.py (*script*)
Non documented, probably outdated.

## apihandler.py (*module*)
Class to handle extra API commands, that are not part of the code nodes protocol.  
Used by Json-RPC serverand other third party interaction.  
Prefered entry point for adding new generic features.

## apidirs.py (*module*)
To be used by GUI wallet.  Finds local user storage location.

## arches_client.py (*script*)
Non documented arches client.

## async_client.py (*module*)
Async TCP Client class for use by asyncio Bismuth Wallet orother async clients.

## balance_nogui.py (*script*)
Get balance from the command line.  
Outdated, still uses privkey.der/pubkey.der instead or modern wallet.der

## bisecdsa.py (*script*)
Unused poc for ecdsa functions. Do not use.

## bisprocmon.py (*script*)
Simple process monitor with http interface.

## bisurl.py (*module*)
bisurl encoding and decoding functions.  
A bisurl is a single string that encodes recipient, amount, data and a checksum into a single url like string, to be used in the wallet.  
Exchanges could use this feature to give each user a bis url for deposit. The bis url would include the required custom data field, and the user would only have to adjust the required amount.

## block_time_calc.py  (*script*)
Outdated block time calculator. Do not use.

## check_tx.py  (*script*)
A Demo script that takes a transaction id as input, and sends back a json with it's status.  
Comes with a doc https://github.com/hclivess/Bismuth/blob/master/check_tx.md

## commands.py  (*script*)
Command line interface to node commands. Undocumented.  
Rather refer to the dedicated API repo. 

## commands_new_not_working.py  (*script*)
Undocumented WIP.

## connections.py (*module*)
Low level protocol functions, header + json over raw socket.  
Len of payload, decimal, on 10 chars, followed by json encoded data. See API repo.

## db.py (*module*)
Database helpers - sqlite.

## dbhandler.py (*module*)
Outdated WIP

## decryptor.py (*script*)
Decrypt an encrypted wallet.  
Only privkey is encrypted in an encrypted wallet. Address and pubkey stay in clear format.

## demo_txsend.py (*script*)
**!!Do not use unless you know what you do!!**. 
Assemble and sends a transaction to be signed and sent by a node.
**sends the privkey to the node**

## difficulty_calculator.py (*script*)
Undocumented 

## essentials.py (*module*)
Common helpers. Some DB related, mainly crypto helpers.

## exchange.py (*script*)
Old helper script for use by exchanges, to be used with a local node only.  
**sends privkey to the node**

## genesis.py (*script*)
Do not use.  
Helper script to create a new genesis block, from an empty chain.

## hmac_drbg.py (*module*)
HMAC_DRBG (sha512) helper class, used by the "heavy3" PoW algorithm.

## html_dappie.py (*script*)
Undocumented

## hyper_test.py (*script*)
Test. Recompress the ledger into hyperblocks.

## icons.py (*module*)
Undocumented

## keygen.py (*script*)
Sample script to generate a new address.

## keys.py (*module*)
Helpers to generate and read wallet.der (privkey, pubkey, address)

## ledger_explorer.py (*script*)
Early version of a block explorer. Not maintained.

## legacy_gui.py (*script*)
Early version of a wallet. Not maintained.

## log.py (*module*)
Logging - console, file - helpers.

## lwbench.py (*module*) (wallet)
Benchmark Helper for wallet.

## mempool.py (*module*)
Mempool module for Bismuth nodes

## miner.py (*script*)
Legacy miner. Not maintained, not "heavy3" compatible.

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

## poolware_dappie (*script*)
Unmaintained pool interface. Do not use.

## poolware_explorer (*script*)
Unmaintained pool interface. Do not use.

## probability_analyzer.py  (*script*)
One time script to analyse the outcome of the casino.

## process_search.py (*module*)
Helper to test for a process presence.

## quantizer.py (*module*)
Helpers. Workaround for float rounding.

## recovery.py (*module*)
Helper. Recovers pubkey and address from privkey alone.

## rewards_reindex.py  (*script*)
System script to reindex dev rewards. Do not use.

## rewards_test.py  (*script*)
Test. Do not use.

## rollback.py (*module*)
Helper. rollbacks indexs on block rollback.

## send_nogui.py  (*script*)
Command line script to send a transaction.  
Can be used as base for automated payment scripts.  
Does **not** send anything sensitive to the node.

## send_nogui_noconf.py  (*script*)
As send_nogui.py, without confirmation.

## simplecrypt.py (*module*)
Helper. Handles wallet encryption/decryption with password.

## staking.py (*module*)
Helper. Proof of concept for offline staking.

## tokensv2.py (*module*)
Tokens functions.

## twitterizer.py  (*script*)
App: reward users who register their bismuth tweets.

## vanity.py  (*script*)
Try to generate addresses with specific chars in it.  
Concept only, do not use for real world use without prior testing that the wallet works.

## wallet.py  (*script*)
Current GUI Wallet.

## wallet_async.py  (*script*)
Proof of concept asyncio GUI wallet.

## wallet_async_old.py  (*script*)
outdated

## wallet_old.py  (*script*)
outdated

## wallet_recovery_tool.py  (*script*)
Util. Tries to recover a wallet from privkey.

## zircodice_dappie.py  (*script*)
Dapp part of the zircodice casino.

## zircodice_web.py  (*script*)
Web part of the zircodice casino.
