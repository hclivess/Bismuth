"""Resolve the per-user Bismuth application directory (vestigial).

Computes the user's home directory and a ``<home>\\Bismuth`` data path. The
path separator is hardcoded for Windows and the module prints the result at
import time, which makes it unsuitable for use as a library.

NOTE: this module appears to be unused — no other module imports it (verified
including dynamic / string-based imports). It is a candidate for removal but is
left in place pending a human decision. The module-level ``print`` is its only
behaviour and is deliberately left untouched to keep this change byte-for-byte
behaviour-preserving.
"""

from os.path import expanduser
home = expanduser("~")
home_bismuth = f"{home}\\Bismuth"

print(home,home_bismuth)