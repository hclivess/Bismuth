#!/usr/bin/env python3
"""dump_vectors.py — emit poker_deal.py reference vectors to JSON for the node cross-check.

Dumps:
  * the SRA prime P (decimal + hex),
  * the card map (card -> field value) and the canonical cardmap_bytes / cardmap_digest,
  * enc/dec of ALL 52 cards under a fixed key (round-trip witness),
  * for N = 2..6: a FULL deterministic deal's stage decks (every shuffle stage + the locked deck) as
    decimal-string arrays, the deal_digests, each seat's hole, and the board.

The deterministic RNG is the SAME sha256 stream tests/test_poker_deal.mkrng uses, with the SAME seeds, so
the JS port (driven by the identical stream) must reproduce these arrays EXACTLY.

NEVER touches the live ledger; pure in-process arithmetic.
"""
import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
for p in (ROOT, os.path.join(ROOT, "contracts"), os.path.join(ROOT, "tests")):
    if p not in sys.path:
        sys.path.insert(0, p)

import poker_deal as d
import poker_table as pt
import bismuth_serialize


def mkrng(seed):
    """EXACT copy of tests/test_poker_deal.mkrng — a deterministic sha256 byte stream."""
    st = [seed if isinstance(seed, bytes) else str(seed).encode()]

    def rng(n):
        out = b""
        while len(out) < n:
            st[0] = hashlib.sha256(st[0]).digest()
            out += st[0]
        return out[:n]

    return rng


def pt_calldata_hex():
    """A sample FN_CHECK calldata (be4(selector) ++ no args) for the signing-buffer reference."""
    return (pt.FN_CHECK & 0xFFFFFFFF).to_bytes(4, "big").hex()


def run_deal(n, seed):
    deal = d.Deal(n, rng=mkrng(seed))
    deal.run_shuffle()
    deal.run_lock()
    holes = {r: deal.deal_hole(r) for r in range(n)}
    board = deal.reveal_board()
    return deal, holes, board


def main():
    out = {}
    out["P_dec"] = str(d.P)
    out["P_hex"] = hex(d.P)

    # card map
    out["card_map"] = [[c, str(d.card_val(c))] for c in range(52)]
    out["cardmap_bytes_hex"] = d.cardmap_bytes().hex()
    out["cardmap_digest_hex"] = d.cardmap_digest().hex()

    # enc/dec of all 52 cards under one fixed key (deterministic)
    key = d.gen_key(mkrng(b"fixed-key-witness"))
    out["fixed_key"] = [str(key[0]), str(key[1])]
    enc_all = []
    for c in range(52):
        v = d.card_val(c)
        e = d.enc(v, key)
        back = d.dec(e, key)
        assert back == v, "enc/dec roundtrip failed for card %d" % c
        enc_all.append([c, str(e)])
    out["enc_all_fixed_key"] = enc_all

    # full deals N=2..6 with the SAME seeds the test uses for correctness, plus a deterministic per-N seed
    deals = {}
    for n in range(2, 7):
        seed = b"crosscheck-%d" % n
        deal, holes, board = run_deal(n, seed)
        deals[str(n)] = {
            "seed": seed.decode(),
            "stage_decks": [[str(v) for v in stage] for stage in deal.stage_decks],
            "deal_digests_hex": [h.hex() for h in deal.deal_digests()],
            "holes": {str(r): holes[r] for r in range(n)},
            "board": board,
            "shuffled_deck": [str(v) for v in deal.shuffled_deck],
            "locked_deck": [str(v) for v in deal.locked_deck],
        }
    out["deals"] = deals

    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "vectors.json")
    with open(path, "w") as f:
        json.dump(out, f, indent=1)

    # ---- FN_* selectors straight from poker_table.py (so the JS ABI table is checked at the source) ----
    fn = {
        "FN_SIT": pt.FN_SIT, "FN_LEAVE": pt.FN_LEAVE, "FN_DEAL": pt.FN_DEAL, "FN_COMMIT": pt.FN_COMMIT,
        "FN_DECK_DIGEST": pt.FN_DECK_DIGEST, "FN_CHECK": pt.FN_CHECK, "FN_CALL": pt.FN_CALL,
        "FN_BET": pt.FN_BET, "FN_FOLD": pt.FN_FOLD, "FN_REVEAL": pt.FN_REVEAL, "FN_TIMEOUT": pt.FN_TIMEOUT,
        "FN_SETTLE": pt.FN_SETTLE, "IDX_CARDMAP": pt.IDX_CARDMAP, "DECKH_MAXIDX": pt.DECKH_MAXIDX,
    }
    with open(os.path.join(here, "fn_selectors.json"), "w") as f:
        json.dump(fn, f, indent=1)

    # ---- a reference signing buffer for a known vm:call (the EXACT bytes the node signs/verifies) ----
    ts = "1700000000.00"
    addr = "ed5e9c2f3d4b1a6c7e8f0a1b2c3d4e5f60718293a4b5c6d7e8f90123"   # 56 hex (RSA address shape)
    contract = "00112233445566778899aabbccddeeff00112233445566778899aabb"     # 56 hex
    calldata_hex = pt_calldata_hex()
    openfield = "%s:%s" % (contract, calldata_hex)
    amount = "%.8f" % 0.0
    buf = bismuth_serialize.signature_buffer(ts, addr, contract, amount, "vm:call", openfield)
    sign_ref = {
        "timestamp": ts, "address": addr, "recipient": contract, "amount": amount,
        "operation": "vm:call", "openfield": openfield, "buffer_hex": buf.hex(),
    }
    with open(os.path.join(here, "sign_ref.json"), "w") as f:
        json.dump(sign_ref, f, indent=1)

    print("wrote %s" % path)
    print("  P_hex = %s" % out["P_hex"])
    print("  cardmap_digest = %s" % out["cardmap_digest_hex"])
    for n in range(2, 7):
        print("  N=%d stages=%d digests[0]=%s board=%s" % (
            n, len(deals[str(n)]["stage_decks"]),
            deals[str(n)]["deal_digests_hex"][0][:16], deals[str(n)]["board"]))


if __name__ == "__main__":
    main()
