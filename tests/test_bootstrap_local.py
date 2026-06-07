"""
Bootstrap source-resolution tests (chain_ops.bootstrap).

Guards the fix for the dead hardcoded download host: a locally-provided ledger archive must be used
WITHOUT any network access and extracted into the ledger's own directory; a missing source must fail
LOUDLY (re-raise) instead of being swallowed by a bare except. No node subprocess, no network.

Run with: python3 -m pytest tests/test_bootstrap_local.py -v
"""
import os
import tarfile
import types

import pytest

import chain_ops


class _Log:
    def __init__(self):
        self.lines = []

    def warning(self, m):
        self.lines.append(str(m))

    def info(self, m):
        pass

    def error(self, m):
        self.lines.append(str(m))


def _make_node(tmp_path, bootstrap_file="", bootstrap_url="http://127.0.0.1:1/nope.tar.gz"):
    node = types.SimpleNamespace()
    node.ledger_path = os.path.join(str(tmp_path), "ledger.db")
    node.bootstrap_file = bootstrap_file
    node.bootstrap_url = bootstrap_url  # deliberately unreachable so any download attempt fails fast
    node.logger = types.SimpleNamespace(app_log=_Log())
    return node


def _make_archive(path, marker_name, marker_text="hello"):
    src = os.path.join(os.path.dirname(path), marker_name)
    with open(src, "w") as f:
        f.write(marker_text)
    with tarfile.open(path, "w:gz") as tar:
        tar.add(src, arcname=marker_name)
    os.remove(src)


def test_bootstrap_uses_local_file_without_network(tmp_path):
    # archive pointed to explicitly by bootstrap_file is used directly (no download)
    archive = os.path.join(str(tmp_path), "boot.tar.gz")
    _make_archive(archive, "BOOTSTRAP_MARKER.txt")
    node = _make_node(tmp_path, bootstrap_file=archive)

    chain_ops.bootstrap(node)

    extracted = os.path.join(str(tmp_path), "BOOTSTRAP_MARKER.txt")
    assert os.path.exists(extracted), "local bootstrap archive was not extracted into the ledger dir"
    assert open(extracted).read() == "hello"


def test_bootstrap_uses_existing_default_archive(tmp_path):
    # no bootstrap_file, but an archive already sits at <ledger_path>.tar.gz -> used without download
    node = _make_node(tmp_path, bootstrap_file="")
    _make_archive(node.ledger_path + ".tar.gz", "DEFAULT_ARCHIVE_MARKER.txt")

    chain_ops.bootstrap(node)

    assert os.path.exists(os.path.join(str(tmp_path), "DEFAULT_ARCHIVE_MARKER.txt"))


def test_bootstrap_failure_is_loud(tmp_path):
    # no local archive anywhere -> it tries the unreachable url and must RAISE (not swallow)
    node = _make_node(tmp_path, bootstrap_file="")
    with pytest.raises(Exception):
        chain_ops.bootstrap(node)
    assert any("Bootstrapping failed" in line for line in node.logger.app_log.lines)
