"""Regression tests for the bug-fix wave (lower-risk standalone/helper files).

Each test proves a *real* bug (demonstrating the original failure mode) and then
asserts the applied fix. Pure Python -- no running node required.

Covered:
  * gpuminer/opencl_alt/miner.py  -- exception path called traceback.print_exc()
                                     without importing `traceback` (NameError).
  * balance_nogui.py              -- unpacked essentials.keys_load() into 7 names
                                     while it returns 8 (ValueError on every run).
"""
import ast
import hashlib
import json
import os
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _module_imported_names(path):
    """Return the set of top-level names made available by `import ...` statements."""
    with open(path, "r", encoding="utf-8") as fp:
        tree = ast.parse(fp.read(), filename=path)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
    return names


def _unpack_target_counts_for_call(path, func_name):
    """For every `... = <func_name>(...)` assignment, yield the number of unpack targets.

    Returns a list of ints (one per matching assignment). A bare single Name target
    (no tuple unpacking) yields 1.
    """
    with open(path, "r", encoding="utf-8") as fp:
        tree = ast.parse(fp.read(), filename=path)
    counts = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        call = node.value
        if not isinstance(call, ast.Call):
            continue
        fn = call.func
        # match both `keys_load(...)` and `essentials.keys_load(...)`
        matched = (
            (isinstance(fn, ast.Name) and fn.id == func_name)
            or (isinstance(fn, ast.Attribute) and fn.attr == func_name)
        )
        if not matched:
            continue
        target = node.targets[0]
        if isinstance(target, (ast.Tuple, ast.List)):
            counts.append(len(target.elts))
        else:
            counts.append(1)
    return counts


# ---------------------------------------------------------------------------
# Bug 1: gpuminer/opencl_alt/miner.py missing `import traceback`
# ---------------------------------------------------------------------------
MINER_PATH = os.path.join(ROOT, "gpuminer", "opencl_alt", "miner.py")


def test_miner_imports_traceback():
    """The fix: `traceback` must be importable in miner.py (its except path uses it)."""
    names = _module_imported_names(MINER_PATH)
    assert "traceback" in names, (
        "miner.py calls traceback.print_exc() in its top-level exception handler "
        "but does not import `traceback` -> NameError at the worst possible time."
    )


def test_miner_still_uses_traceback_print_exc():
    """Guard: confirm the symbol we just imported is actually the one that is used."""
    with open(MINER_PATH, "r", encoding="utf-8") as fp:
        src = fp.read()
    assert "traceback.print_exc" in src


def test_traceback_call_needs_the_import():
    """Demonstrate the original failure mode: the handler body NameErrors without the import."""
    handler_body = "traceback.print_exc(file=sys.stdout)"

    # Without `traceback` bound -> NameError (this is exactly what the old code did).
    ns_broken = {"sys": __import__("sys")}
    with pytest.raises(NameError):
        exec(handler_body, ns_broken)

    # With the import present (the fix) -> runs cleanly even outside an active exception.
    import sys as _sys
    import traceback as _tb
    exec(handler_body, {"sys": _sys, "traceback": _tb})  # must not raise


# ---------------------------------------------------------------------------
# Bug 2: balance_nogui.py unpacked keys_load() into the wrong number of names
# ---------------------------------------------------------------------------
BALANCE_NOGUI_PATH = os.path.join(ROOT, "balance_nogui.py")
NODE_INIT_PATH = os.path.join(ROOT, "node_init.py")


def _make_temp_wallet(directory):
    """Create a minimal valid wallet.der (271-char pubkey PEM) in `directory`."""
    from Cryptodome.PublicKey import RSA

    key = RSA.generate(1024)  # 1024-bit -> public PEM is exactly 271 chars (passes validation)
    priv = key.exportKey().decode("utf-8")
    pub = key.publickey().exportKey().decode("utf-8")
    addr = hashlib.sha224(pub.encode("utf-8")).hexdigest()
    with open(os.path.join(directory, "wallet.der"), "w") as fp:
        json.dump({"Private Key": priv, "Public Key": pub, "Address": addr}, fp)
    return addr


def test_keys_load_returns_eight_values():
    """Establish the ground truth that drives the bug: keys_load yields 8 values."""
    import essentials

    cwd = os.getcwd()
    tmp = tempfile.mkdtemp()
    try:
        os.chdir(tmp)
        _make_temp_wallet(tmp)
        result = essentials.keys_load("privkey.der", "pubkey.der")
    finally:
        os.chdir(cwd)
    assert len(result) == 8, (
        "essentials.keys_load() returns 8 values; any call site unpacking a "
        "different count crashes with ValueError."
    )


def test_balance_nogui_unpacks_eight_targets():
    """The fix: balance_nogui.py must unpack keys_load() into 8 names, not 7."""
    counts = _unpack_target_counts_for_call(BALANCE_NOGUI_PATH, "keys_load")
    assert counts == [8], (
        "balance_nogui.py must unpack essentials.keys_load() into 8 targets "
        "(it returns 8); found {}.".format(counts)
    )


def test_balance_nogui_matches_canonical_call_site():
    """The canonical node_init.py call site also unpacks 8 -- balance_nogui must agree."""
    canonical = _unpack_target_counts_for_call(NODE_INIT_PATH, "keys_load")
    assert 8 in canonical, "expected node_init.py to unpack keys_load() into 8 names"
    balance = _unpack_target_counts_for_call(BALANCE_NOGUI_PATH, "keys_load")
    assert balance == [8]


def test_seven_target_unpack_was_a_real_crash():
    """Demonstrate the original failure: unpacking 8 returned values into 7 names raises."""
    eight = tuple(range(8))

    # The OLD code: 7 names on the left, 8 values on the right -> ValueError.
    with pytest.raises(ValueError):
        a, b, c, d, e, f, g = eight  # noqa: F841

    # The FIXED code: 8 names -> succeeds.
    a, b, c, d, e, f, g, h = eight  # noqa: F841
    assert (a, h) == (0, 7)
