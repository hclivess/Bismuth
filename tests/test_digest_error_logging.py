"""
digest.handle_processing_error logging test.

When a block is rejected during sync, the handler must log the CAUSE and the REAL failure location
(deepest traceback frame) through the logger — not a bare `print` of the call site with no message,
which is what real mainnet logs showed ("<class 'ValueError'> digest.py 327"). Pure unit test: no
node subprocess, no DB, no network.

Run with: python3 -m pytest tests/test_digest_error_logging.py -v
"""
import types

import pytest

import digest


class _Log:
    def __init__(self):
        self.lines = []

    def warning(self, m):
        self.lines.append(str(m))

    def info(self, m):
        pass


def _raise_deep():
    # an extra frame so the deepest traceback frame is distinct from the call site
    def inner():
        raise ValueError("bad signature in block 4733000")
    inner()


def test_handle_processing_error_logs_cause_and_real_location():
    log = _Log()
    node = types.SimpleNamespace(
        logger=types.SimpleNamespace(app_log=log),
        last_block=None, last_block_hash=None,
        peers=types.SimpleNamespace(warning=lambda *a, **k: False))
    db = types.SimpleNamespace(
        block_max_ram=lambda: {"block_height": 42},
        last_block_hash=lambda: "deadbeef")

    try:
        _raise_deep()
    except ValueError as e:
        # the handler always re-raises a generic abort after logging
        with pytest.raises(ValueError, match="digestion aborted"):
            digest.handle_processing_error(node, db, None, "1.2.3.4", e)

    blob = " ".join(log.lines)
    # the actual cause is logged (not swallowed)
    assert "bad signature in block 4733000" in blob
    assert "ValueError" in blob
    # the REAL failure site (this test file, the `inner` frame) is reported, not a fixed call site
    assert "test_digest_error_logging.py" in blob
    # peer + fallback height are in the log for diagnosis
    assert "1.2.3.4" in blob
    assert "fell back to block 42" in blob
    # our view was rolled back to the committed height
    assert node.last_block == 42
