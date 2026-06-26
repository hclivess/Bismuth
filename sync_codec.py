"""Compact BINARY encoding of the REST sync payload (doc/40 — API transport compaction).

The REST fast-sync endpoint (`GET /api/blocks/range?format=sync`) returns, per block,
`{block_height, block_hash, transactions:[[ts, addr, recip, amount, sig, pubkey, op, openfield], ...]}`
as JSON with hex/base64/decimal-string fields. That is verbose: hashes are 2x (hex), signatures/pubkeys
~1.33x (base64), amounts ~10 chars. This module serializes the SAME logical payload to TRUE BYTES and
decodes it back to the **byte-identical** Python structure the digester consumes — so it is a pure
negotiated TRANSPORT swap (`?format=binary`), NOT a consensus/wire-form change: no fork gate, coexists
with the JSON path, and a binary-synced node derives byte-identical blocks to a JSON- or socket-synced one.

It reuses the hf2 Stage-4 storage codecs (sigbytes/addrbytes/txfields) so the wire and the store share one
set of reversible encodings. Works for pre- AND post-fork blocks (the codecs round-trip any input):
hashes are length-prefixed raw (28B sha224 / 32B blake2b), amounts are integer-units varints reconstructed
via amounts.from_units (the consensus_amount form), signatures keep their per-scheme tag, addresses keep
their family tag, pubkeys/operation/openfield are length-prefixed raw.
"""
import amounts
import addrbytes
import sigbytes
import txfields
from txfields import uvarint_encode as _uv, uvarint_decode as _ud

__version__ = "0.0.1"

MAGIC = b"BSY1"


def _lp1(b):                       # u8-length-prefixed bytes
    if len(b) > 255:
        raise ValueError("field exceeds u8 length: %d" % len(b))
    return bytes((len(b),)) + b


def _lp4(b):                       # u32 LE length-prefixed bytes
    return len(b).to_bytes(4, "little") + b


def _rd1(buf, i):
    n = buf[i]; i += 1
    return buf[i:i + n], i + n


def _rd4(buf, i):
    n = int.from_bytes(buf[i:i + 4], "little"); i += 4
    return buf[i:i + n], i + n


def _pack_pubkey(pk):
    """Pubkey wire string -> u8 flag || varint len || bytes. flag 0 = base64 (store raw), 1 = verbatim."""
    s = pk if isinstance(pk, str) else str(pk)
    try:
        import base64
        raw = base64.b64decode(s)
        if base64.b64encode(raw).decode("ascii") == s:
            return b"\x00" + _uv(len(raw)) + raw
    except Exception:
        pass
    b = s.encode("utf-8")
    return b"\x01" + _uv(len(b)) + b


def _unpack_pubkey(buf, i):
    flag = buf[i]; i += 1
    n, i = _ud(buf, i)
    body = buf[i:i + n]; i += n
    if flag == 0:
        import base64
        return base64.b64encode(body).decode("ascii"), i
    return body.decode("utf-8"), i


def _encode_tx(tx):
    """tx = [ts, addr, recip, amount, sig, pubkey, op, openfield] (consensus order)."""
    ts, addr, recip, amount, sig, pubkey, op, openfield = tx[0], tx[1], tx[2], tx[3], tx[4], tx[5], tx[6], tx[7]
    out = bytearray()
    out += txfields.pack_timestamp(ts)                                   # varint cs (self-delimiting)
    out += _lp1(addrbytes.pack_addr(addr))                               # u8 len + addr blob
    out += _lp1(addrbytes.pack_addr(recip))                              # u8 len + recipient blob
    out += _uv(amounts.to_units(amount))                                 # amount integer units (varint)
    out += sigbytes.pack_from_wire(sig, addr)                            # tag||u16len||raw (self-delimiting)
    out += _pack_pubkey(pubkey)                                          # flag||varint len||bytes
    out += _lp1(str(op).encode("utf-8"))                                 # u8 len + operation (cap 30)
    of = openfield.encode("utf-8") if isinstance(openfield, str) else bytes(openfield)
    out += _lp4(of)                                                      # u32 len + openfield raw
    return bytes(out)


def _decode_tx(buf, i):
    cs, i = _ud(buf, i)
    ts = txfields.unpack_timestamp(_uv(cs))                              # reconstruct '%.2f'
    a, i = _rd1(buf, i); addr = addrbytes.unpack_addr(a)
    r, i = _rd1(buf, i); recip = addrbytes.unpack_addr(r)
    units, i = _ud(buf, i); amount = amounts.from_units(units)           # consensus_amount form
    # signature: tag(1) + u16 len(2) + raw
    slen = int.from_bytes(buf[i + 1:i + 3], "little"); sblob = buf[i:i + 3 + slen]; i += 3 + slen
    sig = sigbytes.to_wire(sblob)
    pubkey, i = _unpack_pubkey(buf, i)
    op, i = _rd1(buf, i); operation = op.decode("utf-8")
    of, i = _rd4(buf, i); openfield = of.decode("utf-8")
    return [ts, addr, recip, amount, sig, pubkey, operation, openfield], i


def encode_blocks(blocks):
    """A list of sync-block dicts (block_height, block_hash, transactions:[8-field tuples]) -> bytes."""
    out = bytearray(MAGIC)
    out += _uv(len(blocks))
    for blk in blocks:
        out += _uv(int(blk["block_height"]))
        bh = blk["block_hash"]
        try:
            raw = bytes.fromhex(bh)
            out += b"\x00" + _lp1(raw)                                   # 0 = raw hex digest (28/32 B)
        except (ValueError, TypeError):
            out += b"\x01" + _lp1(str(bh).encode("utf-8"))               # 1 = verbatim (non-hex, rare)
        txs = blk["transactions"]
        out += _uv(len(txs))
        for tx in txs:
            out += _encode_tx(tx)
    return bytes(out)


def decode_blocks(buf):
    """Inverse of encode_blocks -> the byte-identical list of sync-block dicts the JSON path produces."""
    buf = bytes(buf)
    if buf[:4] != MAGIC:
        raise ValueError("not a binary sync payload")
    i = 4
    nblocks, i = _ud(buf, i)
    blocks = []
    for _ in range(nblocks):
        height, i = _ud(buf, i)
        hflag = buf[i]; i += 1
        hb, i = _rd1(buf, i)
        block_hash = hb.hex() if hflag == 0 else hb.decode("utf-8")
        ntx, i = _ud(buf, i)
        txs = []
        for _t in range(ntx):
            tx, i = _decode_tx(buf, i)
            txs.append(tx)
        blocks.append({"block_height": height, "block_hash": block_hash, "transactions": txs})
    return blocks
