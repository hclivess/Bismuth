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
import addrbytes
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


def pack_row(t):
    """Pack an 11-field stored row (public_key already replaced by its dedup id) into one bytes blob.

    Layout (all little-endian / self-delimiting):
      timestamp   varint cs           amount/fee/reward  varint units (txfields)
      address     u8 len + addr blob  recipient          u8 len + addr blob
      signature   sigbytes blob (tag||u16len||raw, self-delimiting)
      pubkey_id   varint              block_hash         u8 flag(0 hex/1 verbatim) + u8 len + bytes
      operation   u8 len + utf8       openfield          u32 len + raw bytes
    """
    out = bytearray()
    out += txfields.pack_timestamp(t[0])                       # timestamp
    out += _lp1(addrbytes.pack_addr(t[1]))                     # address
    out += _lp1(addrbytes.pack_addr(t[2]))                     # recipient
    out += txfields.pack_num(t[3])                             # amount
    out += sigbytes.pack_from_wire(t[4], t[1])                 # signature (addr -> scheme tag)
    out += _uv(int(t[5]))                                      # public_key dedup id
    bh = t[6]                                                  # block_hash: raw digest if hex, else verbatim
    try:
        raw = bytes.fromhex(bh) if isinstance(bh, str) else bytes(bh)
        out += b"\x00" + _lp1(raw)
    except (ValueError, TypeError):
        out += b"\x01" + _lp1(str(bh).encode("utf-8"))
    out += txfields.pack_num(t[7])                             # fee
    out += txfields.pack_num(t[8])                             # reward
    out += _lp1(str(t[9]).encode("utf-8"))                     # operation (cap 30)
    of = t[10].encode("utf-8") if isinstance(t[10], str) else bytes(t[10])
    out += _lp4(of)                                            # openfield raw
    return bytes(out)


def unpack_row(blob):
    """Inverse of pack_row -> the 11-field row (public_key as the dedup id; block_store re-expands it)."""
    buf = bytes(blob)
    i = 0
    cs, i = _ud(buf, i); timestamp = txfields.unpack_timestamp(_uv(cs))
    ab, i = _rd1(buf, i); address = addrbytes.unpack_addr(ab)
    rb, i = _rd1(buf, i); recipient = addrbytes.unpack_addr(rb)
    au, i = _ud(buf, i); amount = str(au) if amounts.LEDGER_INTEGER else amounts.from_units(au)
    slen = int.from_bytes(buf[i + 1:i + 3], "little")          # signature self-delimiting (tag+u16+raw)
    sig = sigbytes.to_wire(buf[i:i + 3 + slen]); i += 3 + slen
    pkid, i = _ud(buf, i)
    hflag = buf[i]; i += 1
    hb, i = _rd1(buf, i); block_hash = hb.hex() if hflag == 0 else hb.decode("utf-8")
    fu, i = _ud(buf, i); fee = str(fu) if amounts.LEDGER_INTEGER else amounts.from_units(fu)
    ru, i = _ud(buf, i); reward = str(ru) if amounts.LEDGER_INTEGER else amounts.from_units(ru)
    ob, i = _rd1(buf, i); operation = ob.decode("utf-8")
    fb, i = _rd4(buf, i); openfield = fb.decode("utf-8")
    return [timestamp, address, recipient, amount, sig, pkid, block_hash, fee, reward, operation, openfield]


def openfield_of(blob):
    """The openfield string only (for the fee-weight read) — full unpack is cheap and avoids offset bugs."""
    return unpack_row(blob)[10]
