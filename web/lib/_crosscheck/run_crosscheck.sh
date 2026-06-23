#!/usr/bin/env bash
# run_crosscheck.sh — full headless cross-check of the Stage-3 foundation.
#   1. python3 dump_vectors.py        -> vectors.json + fn_selectors.json + sign_ref.json (poker_deal/table refs)
#   2. node crosscheck.mjs            -> assert bismuth-deal.js reproduces them + ABI calldata builders
#   3. python3 gen_test_wallet.py     -> throwaway RSA wallet
#   4. node signer_roundtrip.mjs      -> JS-sign a vm:call with bismuth-tx.js
#   5. python3 verify_roundtrip.py    -> the NODE's own verifier accepts the JS signature
#   6. node deal_session_sim.mjs      -> headless N-player DISTRIBUTED deal: correctness + PRIVACY assertions
# No ledger, no node service, no network.
set -e
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "== 1. dump poker_deal.py / poker_table.py vectors =="
python3 "$HERE/dump_vectors.py"
echo
echo "== 2. node cross-check: bismuth-deal.js vs vectors + ABI calldata =="
node "$HERE/crosscheck.mjs"
echo
echo "== 3. throwaway RSA wallet =="
python3 "$HERE/gen_test_wallet.py"
echo
echo "== 4. JS sign a vm:call (bismuth-tx.js) =="
node "$HERE/signer_roundtrip.mjs"
echo
echo "== 5. NODE verifier accepts the JS-signed tx =="
python3 "$HERE/verify_roundtrip.py"
echo
echo "== 6. headless N-player DISTRIBUTED deal privacy simulation (bismuth-deal-session.js) =="
node "$HERE/deal_session_sim.mjs"
echo
echo "ALL CROSS-CHECKS PASSED"
