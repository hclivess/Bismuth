# Bismuth: Most useful files

See also full list of files: https://github.com/EggPool/Bismuth/blob/patch-51/_FILES_DESCRIPTION.md

# Main scripts

## node.py
The Bismuth node itself.

## wallet.py
GUI wallet, cross O.S.  
Connects to a local node if there is one, or to an online wallet server.

# No Gui scripts

Console scripts to run from the command line, or use as a base for automation

## balance_nogui.py
Get balance from the command line.
Outdated, still uses privkey.der/pubkey.der instead or modern wallet.der

## check_tx.py
A Demo script that takes a transaction id as input, and sends back a json with is status.  
Comes with a doc https://github.com/hclivess/Bismuth/blob/master/check_tx.md

## commands.py
Command line interface to node commands. Undocumented.  
Rather refer to the dedicated API repo.

## exchange.py (script)
Old helper script for use by exchanges, to be used with a local node only.  
**sends privkey to the node.**

## send_nogui.py
Command line script to send a transaction.  
Can be used as base for automated payment scripts.  
Does **not** send anything sensitive to the node.

# App & Dapp samples

## twitterizer.py
App: reward users who register their bismuth tweets.

## zircodice_dappie.py  (*script*)
Dapp part of the zircodice casino.

## zircodice_web.py  (*script*)
Web part of the zircodice casino.

# See also

## Bismuth Core API

https://github.com/EggPool/BismuthAPI

## Bismuth plugins
https://github.com/bismuthfoundation/BismuthPlugins

