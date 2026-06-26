"""hf2 Stage-4 TRUE-BYTES single-record tx codec (doc/40 — tx-fields model B, "txrec consolidation").

block_store stores each tx as an 11-field msgpack LIST
(timestamp, address, recipient, amount, signature, public_key_id, block_hash, fee, reward, operation,
openfield). Even with the Stage-4 per-field codecs, the msgpack array framing costs ~1 byte for the array
+ ~2 bytes (bin8 prefix) for every element — ~20 wasted bytes per tx. This module packs the WHOLE row
into ONE concatenated bytes blob (the msgpack list element becomes a single `bin`), removing that
per-element overhead — the largest single per-tx storage win left.

It reuses the Stage-4 field codecs (sigbytes/addrbytes/txfields), so the wire/store share one set of
reversible encodings. ``unpack_row`` reconstructs the EXACT 11-field row (public_key still as the dedup id;
block_store re-expands it), so get_block / cross_check / verify_against_sqlite see byte-identical rows.
Gated by the CALLER on node.fork_height (post-fork blocks store txrec; pre-fork stay legacy lists).
"""
import amounts
import sigbytes
import txfields
from txfields import uvarint_encode as _uv, uvarint_decode as _ud

__version__ = "0.0.1"


def _lp1(b):
    if len(b) > 255:
        raise ValueError("field exceeds u8 length: %d" % len(b))
    return bytes((len(b),)) + b


def _lp4(b):
    return len(b).to_bytes(4, "little") + b


def _rd1(buf, i):
    n = buf[i]
    return buf[i + 1:i + 1 + n], i + 1 + n


def _rd4(buf, i):
    n = int.from_bytes(buf[i:i + 4], "little")
    return buf[i + 4:i + 4 + n], i + 4 + n


def pack_row(t, addr_idx, recip_idx):
    """Pack an 11-field stored row (public_key already a dedup id) into one bytes blob. ``addr_idx`` /
    ``recip_idx`` are this tx's address / recipient positions in the BLOCK's per-block address dict (the
    envelope ``"a"`` list) — stored as varint indices instead of the inline ~30-byte address blob, so a
    repeated address (self-spend, change, a sender with several txs in the block) costs ~1 byte not ~30.

    Layout (all little-endian / self-delimiting):
      timestamp   varint cs           amount/fee/reward  varint units (txfields)
      addr_idx    varint              recip_idx          varint   (-> envelope "a" dict)
      signature   sigbytes blob (tag||u16len||raw, self-delimiting)
      pubkey_id   varint              (block_hash is NOT stored — hoisted to the block envelope)
      operation   u8 len + utf8       openfield          u32 len + raw bytes

    Note: the u8 length prefixes (operation; the "a"-dict address blobs) rely on the consensus-layer
    truncation upstream (address/recipient [:56], operation [:30]) staying under 255 bytes — an inherited
    invariant, not re-enforced here (_lp1 raises if ever handed > 255 bytes). t[1] (address string) is still
    used to derive the signature scheme tag.
    """
    out = bytearray()
    out += txfields.pack_timestamp(t[0])                       # timestamp
    out += _uv(int(addr_idx))                                  # address  -> index into envelope "a"
    out += _uv(int(recip_idx))                                 # recipient -> index into envelope "a"
    out += txfields.pack_num(t[3])                             # amount
    out += sigbytes.pack_from_wire(t[4], t[1])                 # signature (addr string -> scheme tag)
    out += _uv(int(t[5]))                                      # public_key dedup id
    # block_hash (t[6]) is NOT stored: it is identical for every tx in a block and equals the block-store
    # envelope hash, so block_store._expand fills it from there (one source of truth, ~33B/tx saved).
    out += txfields.pack_num(t[7])                             # fee
    out += txfields.pack_num(t[8])                             # reward
    out += _lp1(str(t[9]).encode("utf-8"))                     # operation (cap 30)
    of = t[10].encode("utf-8") if isinstance(t[10], str) else bytes(t[10])
    out += _lp4(of)                                            # openfield raw
    return bytes(out)


def unpack_row(blob):
    """Inverse of pack_row -> the 11-field row with address (index 1) and recipient (index 2) as INTEGER
    indices into the block's "a" dict; block_store._expand resolves them to address strings and re-expands
    the public_key dedup id. block_hash (index 6) is a None placeholder filled from the envelope hash."""
    buf = bytes(blob)
    i = 0
    cs, i = _ud(buf, i); timestamp = txfields.unpack_timestamp(_uv(cs))
    addr_idx, i = _ud(buf, i)
    recip_idx, i = _ud(buf, i)
    au, i = _ud(buf, i); amount = str(au) if amounts.LEDGER_INTEGER else amounts.from_units(au)
    slen = int.from_bytes(buf[i + 1:i + 3], "little")          # signature self-delimiting (tag+u16+raw)
    sig = sigbytes.to_wire(buf[i:i + 3 + slen]); i += 3 + slen
    pkid, i = _ud(buf, i)
    fu, i = _ud(buf, i); fee = str(fu) if amounts.LEDGER_INTEGER else amounts.from_units(fu)
    ru, i = _ud(buf, i); reward = str(ru) if amounts.LEDGER_INTEGER else amounts.from_units(ru)
    ob, i = _rd1(buf, i); operation = ob.decode("utf-8")
    fb, i = _rd4(buf, i); openfield = fb.decode("utf-8")
    return [timestamp, addr_idx, recip_idx, amount, sig, pkid, None, fee, reward, operation, openfield]


def openfield_of(blob):
    """The openfield string only (for the fee-weight read) — full unpack is cheap and avoids offset bugs."""
    return unpack_row(blob)[10]
