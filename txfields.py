"""hf2 Stage-4 TRUE-BYTES codec for the numeric tx fields (doc/40 — tx-fields domain).

Covers the non-crypto scalar fields of a stored tx row: ``timestamp`` and the three money fields
``amount`` / ``fee`` / ``reward``. (address/recipient -> addrbytes; signature -> sigbytes; public_key ->
pk dedup; block_hash -> block-header domain; operation/openfield left as-is for now.)

DESIGN NOTE — why this is simple (probed against the live consensus path, doc/40):
The hf2 consensus serialization ALREADY normalizes the amount string form: ``block_hash_v2`` and
``tx_id_v2_s`` funnel every amount through ``_v2_units`` (integer atomic units), so ``'0'`` and
``'0.00000000'`` produce the IDENTICAL block hash and txid (verified). Likewise the post-fork signature
signs the content txid (amount-form-independent). So the storage layer does NOT need to preserve the
legacy SQLite byte-form of a money field — it only needs CONSENSUS EQUIVALENCE. We therefore store the
integer units (varint) and reconstruct the canonical string on read. ``'0'`` reconstructs as
``'0.00000000'`` (decimal mode) — a deliberate, consensus-safe normalization, NOT a bug. This is the
"resolved for hf2" form, free of the per-field BARE/FIXED8/RAWTEXT render-mode tag the byte-for-byte-SQLite
approach would have needed.

Storage mode (``amounts.LEDGER_INTEGER``) is read LIVE and is the ONLY thing the reconstruction depends on
(decimal -> ``from_units``; integer -> ``str(units)``); either way ``to_units(reconstructed) == units``,
so the re-digested block hash / txid is byte-identical regardless. Gated by the CALLER on
``node.fork_height`` by destination height; pre-fork rows keep the legacy ``str`` untouched.
"""
import amounts

__version__ = "0.0.1"


# --- unsigned LEB128 varint (minimal-length; the decoder rejects non-minimal encodings) ---
def uvarint_encode(n):
    n = int(n)
    if n < 0:
        raise ValueError("varint cannot encode negative %d" % n)
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def uvarint_decode(buf, i=0):
    """Return (value, next_index). Rejects a non-minimal (overlong) encoding so two byte strings can never
    decode to the same value."""
    shift = 0
    result = 0
    start = i
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    if i - start > 1 and buf[i - 1] == 0:
        raise ValueError("non-minimal varint encoding")
    return result, i


# --- timestamp: '%.2f' string <-> integer centiseconds varint (exact, no float) ---
def pack_timestamp(ts):
    """A canonical '%.2f' timestamp string -> minimal varint of integer centiseconds (matches the consensus
    ``_v2_ts_cs`` quantization)."""
    from decimal import Decimal
    cs = int((Decimal(str(ts)).quantize(Decimal("0.01")) * 100).to_integral_value())
    return uvarint_encode(cs)


def unpack_timestamp(blob):
    """Varint centiseconds -> the exact '%.2f' string via integer formatting (no float)."""
    cs, _ = uvarint_decode(bytes(blob), 0)
    return "{}.{:02d}".format(cs // 100, cs % 100)


# --- amount / fee / reward: numeric string <-> integer-units varint (storage-mode aware) ---
def pack_num(value):
    """A stored money field -> minimal varint of integer atomic units.

    Decimal mode (LEDGER_INTEGER=False): the field is a legacy '%.8f'/'0' string -> ``to_units``.
    Integer mode (LEDGER_INTEGER=True): the field is ALREADY a bare integer-units string -> ``int``.
    (This is the residual fix: in integer mode the string IS units; ``to_units`` would wrongly multiply.)"""
    if amounts.LEDGER_INTEGER:
        units = int(str(value))
    else:
        units = amounts.to_units(value)
    return uvarint_encode(units)


def unpack_num(blob):
    """Varint units -> the canonical money string for the CURRENT storage mode. Decimal -> ``from_units``
    (the '%.8f' form; '0' normalizes to '0.00000000', consensus-safe); integer -> ``str(units)``.
    ``to_units(result) == units`` either way, so any re-digested block hash / txid is byte-identical."""
    units, _ = uvarint_decode(bytes(blob), 0)
    return str(units) if amounts.LEDGER_INTEGER else amounts.from_units(units)
