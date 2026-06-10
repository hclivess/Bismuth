"""bip39.py — BIP39 mnemonic codec for Bismuth HD wallets.

The human-friendly 12/24-word backup phrase that layers on top of :mod:`hd_wallet`. BIP39 has two halves
and this implements both, to the standard so a phrase is portable to/from any BIP39 tool:

  entropy  <->  mnemonic   : ENT bits of entropy + (ENT/32)-bit SHA256 checksum, split into 11-bit groups
                             indexing the fixed 2048-word English list (``bip39_english.txt``).
  mnemonic  ->  seed       : PBKDF2-HMAC-SHA512(NFKD(mnemonic), "mnemonic"+NFKD(passphrase), 2048, 64B).

That 64-byte seed is what ``hd_wallet.HDWallet`` consumes (``HDWallet.from_mnemonic``). NOTE: the phrase
<-> seed mapping is the interoperable BIP39 standard, but Bismuth's BIP32 step keys the master with the
domain tag ``b"Bismuth seed"`` (see hd_wallet), a deliberately distinct keyspace — so the SAME phrase
yields portable BACKUP but Bismuth-specific ADDRESSES (it never collides with a Bitcoin wallet's keys).

The wordlist is the canonical English list (sha256 2f5eed53…b24dbda); ``verify_wordlist`` re-checks it.
"""
import hashlib
import os
import unicodedata
from hashlib import sha256

__version__ = "0.1.0"

_WORDLIST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bip39_english.txt")
_WORDLIST_SHA256 = "2f5eed53a4727b4bf8880d8f3f199efc90e58503646d9ff8eff3a2ed3b24dbda"

# valid entropy strengths (bits) -> word counts: 128->12, 160->15, 192->18, 224->21, 256->24
_STRENGTHS = (128, 160, 192, 224, 256)
_WORD_COUNTS = (12, 15, 18, 21, 24)


def _load_wordlist():
    with open(_WORDLIST_FILE, "r", encoding="utf-8") as f:
        words = [w.strip() for w in f if w.strip()]
    if len(words) != 2048:
        raise ValueError("BIP39 wordlist must have exactly 2048 words, got %d" % len(words))
    return words


WORDLIST = _load_wordlist()
_WORD_INDEX = {w: i for i, w in enumerate(WORDLIST)}


def verify_wordlist() -> bool:
    """True iff the on-disk wordlist is the canonical English BIP39 list (exact bytes)."""
    with open(_WORDLIST_FILE, "rb") as f:
        return sha256(f.read()).hexdigest() == _WORDLIST_SHA256


# --- entropy <-> mnemonic -------------------------------------------------------------------------
def _entropy_to_mnemonic(entropy: bytes) -> str:
    if len(entropy) not in (16, 20, 24, 28, 32):
        raise ValueError("entropy must be 16/20/24/28/32 bytes (128..256 bits)")
    ent_bits = len(entropy) * 8
    cs_bits = ent_bits // 32
    checksum = sha256(entropy).digest()
    bits = (bin(int.from_bytes(entropy, "big"))[2:].zfill(ent_bits)
            + bin(int.from_bytes(checksum, "big"))[2:].zfill(256)[:cs_bits])
    return " ".join(WORDLIST[int(bits[i:i + 11], 2)] for i in range(0, len(bits), 11))


def generate(strength: int = 128, entropy: bytes = None) -> str:
    """A fresh mnemonic. ``strength`` is the entropy size in bits (128 -> 12 words, 256 -> 24).
    Pass ``entropy`` to derive deterministically from caller-supplied bytes (e.g. for tests/vectors)."""
    if strength not in _STRENGTHS:
        raise ValueError("strength must be one of %s bits" % (_STRENGTHS,))
    if entropy is None:
        entropy = os.urandom(strength // 8)
    elif len(entropy) != strength // 8:
        raise ValueError("entropy length %d does not match strength %d" % (len(entropy), strength))
    return _entropy_to_mnemonic(entropy)


def _normalize(words):
    return [w for w in unicodedata.normalize("NFKD", words.strip()).split(" ") if w] \
        if isinstance(words, str) else list(words)


def to_entropy(mnemonic: str) -> bytes:
    """Reverse a mnemonic to its entropy bytes; raises ValueError on an unknown word, a bad length, or a
    failed checksum (so this doubles as full validation)."""
    words = _normalize(mnemonic)
    if len(words) not in _WORD_COUNTS:
        raise ValueError("mnemonic must be 12/15/18/21/24 words, got %d" % len(words))
    try:
        indices = [_WORD_INDEX[w] for w in words]
    except KeyError as e:
        raise ValueError("word not in BIP39 wordlist: %s" % e)
    bits = "".join(bin(i)[2:].zfill(11) for i in indices)
    total = len(words) * 11
    ent_bits = total * 32 // 33
    cs_bits = total - ent_bits
    entropy = int(bits[:ent_bits], 2).to_bytes(ent_bits // 8, "big")
    expected = bin(int.from_bytes(sha256(entropy).digest(), "big"))[2:].zfill(256)[:cs_bits]
    if bits[ent_bits:] != expected:
        raise ValueError("invalid mnemonic checksum")
    return entropy


def check(mnemonic: str) -> bool:
    """True iff ``mnemonic`` is a well-formed BIP39 phrase (known words + valid checksum)."""
    try:
        to_entropy(mnemonic)
        return True
    except ValueError:
        return False


# --- mnemonic -> seed -----------------------------------------------------------------------------
def to_seed(mnemonic: str, passphrase: str = "") -> bytes:
    """The 64-byte BIP39 seed: PBKDF2-HMAC-SHA512 over NFKD(mnemonic) with salt "mnemonic"+passphrase.
    This does NOT validate the checksum (BIP39 allows seeding from any phrase) — call ``check`` first if
    you want to reject typos."""
    m = unicodedata.normalize("NFKD", mnemonic).encode("utf-8")
    salt = unicodedata.normalize("NFKD", "mnemonic" + passphrase).encode("utf-8")
    return hashlib.pbkdf2_hmac("sha512", m, salt, 2048, dklen=64)
