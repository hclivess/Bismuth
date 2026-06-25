"""hf2 Stage-4: difficulty / cumulative-work record codec (doc/40 — difficulty domain, D.2/D.6).

Closes the one consensus-relevant SQLite gap the LMDB rearchitecture never filled: the ``misc`` table
(block_height, difficulty TEXT), which PoW-verify, the LWMA retarget, the API, and rollback all read. This
module is the pure codec for a ``diff_store`` projection record; the env + node wiring (the gated
``INSERT INTO misc`` mirror, the LWMA/prev-difficulty read flip) are staged behind the doc/40 validation
gates and not switched on here.

A record is a clean fixed 28-byte little-endian struct — no legacy-TEXT tail. (hf2 is a breaking fork:
the post-fork canonical difficulty IS ``difficulty_e10``; there is no obligation to reproduce the old
non-canonical ``misc.difficulty`` strings like ``'16.0'`` vs ``'16'``. Pre-fork rows are not migrated —
they stay in the existing SQLite ``misc`` read, untouched.)

```
off size field           type     meaning
 0   8   difficulty_e10  u64 LE   difficulty * 10**10 (the quantize_ten 10-dp grid; lossless integer)
 8   4   solvetime       u32 LE   ts(h) - ts(h-1) in seconds, clamped >= 0 (genesis = 0)
12  16   cumulative_work u128 LE  running Σ work_units over [genesis..h], two u64 LE limbs (lo, hi)
                                  --- 28 bytes ---
```

The head drives LWMA + fork-choice (numeric, lossless). Any API surfacing of difficulty is generated from
``difficulty_e10`` (the API already does ``int(float(diff_text))``, so an integer source is strictly safe).
"""
import struct
from decimal import Decimal

import diff_work

__version__ = "0.0.1"

_E10 = 10 ** 10
_HEAD = struct.Struct("<QIQQ")      # difficulty_e10, solvetime, cum_lo, cum_hi  (28 bytes)
HEAD_LEN = _HEAD.size               # 28
_U128_MAX = (1 << 128) - 1


def diff_to_e10(difficulty):
    """A difficulty (Decimal/str/float/int on the 10-dp quantize_ten grid) -> integer * 10**10 (lossless)."""
    return int((Decimal(str(difficulty)).quantize(Decimal("0.0000000001")) * _E10).to_integral_value())


def e10_to_diff(difficulty_e10):
    """Inverse of diff_to_e10: the exact Decimal grid value (10-dp, bit-for-bit)."""
    return Decimal(int(difficulty_e10)) / Decimal(_E10)


def pack_record(difficulty_e10, solvetime, cumulative_work):
    """Pack a fixed 28-byte difficulty record (clean post-fork form, no legacy tail)."""
    difficulty_e10 = int(difficulty_e10)
    solvetime = max(0, int(solvetime))
    cw = int(cumulative_work)
    if not (0 <= difficulty_e10 <= 0xFFFFFFFFFFFFFFFF):
        raise ValueError("difficulty_e10 out of u64 range: %d" % difficulty_e10)
    if not (0 <= cw <= _U128_MAX):
        raise ValueError("cumulative_work out of u128 range: %d" % cw)
    if solvetime > 0xFFFFFFFF:
        solvetime = 0xFFFFFFFF
    return _HEAD.pack(difficulty_e10, solvetime, cw & 0xFFFFFFFFFFFFFFFF, cw >> 64)


def unpack_record(blob):
    """Return a dict {difficulty_e10, solvetime, cumulative_work} from a packed 28-byte record."""
    difficulty_e10, solvetime, lo, hi = _HEAD.unpack(bytes(blob)[:HEAD_LEN])
    return {"difficulty_e10": difficulty_e10, "solvetime": solvetime,
            "cumulative_work": lo | (hi << 64)}


def difficulty_text(blob):
    """The post-fork canonical difficulty string for API surfacing — fixed 10-dp, rendered from the integer
    grid via exact integer formatting (no float, no Decimal trailing-zero stripping)."""
    e10 = unpack_record(blob)["difficulty_e10"]
    return "{}.{:010d}".format(e10 // _E10, e10 % _E10)


def make_record(difficulty, prev_ts, this_ts, prev_cumulative_work):
    """Build a record from chain inputs: difficulty (Decimal/str), the previous and current block timestamps,
    and the previous cumulative work. Returns (blob, cumulative_work)."""
    e10 = diff_to_e10(difficulty)
    solvetime = 0 if prev_ts is None else max(0, int(float(this_ts) - float(prev_ts)))
    cw = int(prev_cumulative_work) + diff_work.work_units(e10)
    return pack_record(e10, solvetime, cw), cw
