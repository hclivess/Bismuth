#!/usr/bin/env bash

# Optimize / Compact the DB.
# To be used before tar gz the bootstrap archive.

echo "Optimizing Index..."
echo "VACUUM;"|sqlite3 index.db
echo "Optimizing Hyper..."
echo "VACUUM;"|sqlite3 hyper.db
echo "Optimizing Ledger..."
echo "VACUUM;"|sqlite3 ledger.db
echo "Done!"
