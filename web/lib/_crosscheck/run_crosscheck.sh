#!/usr/bin/env bash
# run_crosscheck.sh — headless cross-check of the JS transaction signer against the node's own verifier.
#   1. python3 gen_test_wallet.py     -> throwaway RSA wallet
#   2. node signer_roundtrip.mjs      -> JS-sign a tx with bismuth-tx.js
#   3. python3 verify_roundtrip.py    -> the NODE's own verifier accepts the JS signature
# No ledger, no node service, no network.
set -e
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "== 1. throwaway RSA wallet =="
python3 "$HERE/gen_test_wallet.py"
echo
echo "== 2. JS sign a tx (bismuth-tx.js) =="
node "$HERE/signer_roundtrip.mjs"
echo
echo "== 3. NODE verifier accepts the JS-signed tx =="
python3 "$HERE/verify_roundtrip.py"
echo
echo "ALL CROSS-CHECKS PASSED"
