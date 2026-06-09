"""Startup config.txt self-heal (v4.5.0.x hardening).

config.txt is gitignored (it holds the node's live/local settings), so a git checkout or worktree
switch can delete it and crash-loop the node on a FileNotFoundError at `options.Get.read()`. Startup
now auto-restores it from the tracked `config.txt.example` template. These tests prove it restores a
missing config and never clobbers an existing one. Hermetic: a temp CWD, no live node.
"""
import os
import tempfile

import options


def _run_read_in(dirpath):
    cwd = os.getcwd()
    os.chdir(dirpath)
    try:
        g = options.Get()
        g.read()
        return g
    finally:
        os.chdir(cwd)


def test_read_restores_missing_config_from_example():
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "config.txt.example"), "w") as f:
            f.write("port=5658\nrest_api=True\nrest_api_port=5659\n")
        assert not os.path.exists(os.path.join(d, "config.txt"))
        g = _run_read_in(d)
        assert os.path.exists(os.path.join(d, "config.txt")), "config.txt should be restored from the example"
        # and the restored config was actually loaded
        assert g.rest_api is True
        assert g.rest_api_port == 5659
        assert g.port == "5658"


def test_read_does_not_clobber_existing_config():
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "config.txt.example"), "w") as f:
            f.write("port=9999\n")
        with open(os.path.join(d, "config.txt"), "w") as f:
            f.write("port=5658\n")
        g = _run_read_in(d)
        assert g.port == "5658", "an existing config.txt must NOT be overwritten by the example"


def test_read_without_example_is_unchanged_when_config_present():
    # no example at all: existing config.txt loads normally, nothing restored
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "config.txt"), "w") as f:
            f.write("port=5658\n")
        g = _run_read_in(d)
        assert g.port == "5658"
        assert not os.path.exists(os.path.join(d, "config.txt.example"))
