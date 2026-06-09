"""HMAC-DRBG (deterministic random bit generator) over HMAC-SHA-512.

Vendored verbatim from https://github.com/davidlazar/python-drbg. Given a seed it
produces a reproducible byte stream, which Bismuth uses where deterministic
randomness is required. The algorithm (NIST SP 800-90A HMAC_DRBG) is left exactly
as upstream — do not alter the update/reseed/generate steps, as that would change
every value derived from a given seed.
"""

# From https://github.com/davidlazar/python-drbg

import hashlib
import hmac


class DRBG(object):
    """HMAC-SHA-512 deterministic RBG. Seed once, then call :meth:`generate`."""

    def __init__(self, seed):
        self.key = b'\x00' * 64
        self.val = b'\x01' * 64
        self.reseed(seed)

    def hmac(self, key, val) -> bytes:
        """Return ``HMAC-SHA-512(key, val)`` as raw bytes."""
        return hmac.new(key, val, hashlib.sha512).digest()

    def reseed(self, data: bytes = b'') -> None:
        """Mix ``data`` into the internal (key, val) state per HMAC_DRBG update."""
        self.key = self.hmac(self.key, self.val + b'\x00' + data)
        self.val = self.hmac(self.key, self.val)
        if data:
            self.key = self.hmac(self.key, self.val + b'\x01' + data)
            self.val = self.hmac(self.key, self.val)

    def generate(self, n: int) -> bytes:
        """Return ``n`` deterministic bytes, then reseed the internal state."""
        xs = b''
        while len(xs) < n:
            self.val = self.hmac(self.key, self.val)
            xs += self.val

        self.reseed()
        return xs[:n]
