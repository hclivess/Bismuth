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




