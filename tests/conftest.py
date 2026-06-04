"""
Pytest fixtures: launch a fresh regnet node as a subprocess and hand tests a connected
dependency-light client. No external bismuthclient/bismuthcore needed.

Run from the repo root:   python3 -m pytest -v
"""
import os
import shutil
import socket
import subprocess
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.dirname(__file__) not in sys.path:
    sys.path.insert(0, os.path.dirname(__file__))

REGNET_PARAM = "regnet2"
PORT = 3030


def _port_open():
    s = socket.socket()
    s.settimeout(0.3)
    try:
        return s.connect_ex(("127.0.0.1", PORT)) == 0
    finally:
        s.close()


@pytest.fixture(scope="session")
def node_proc():
    # Start each session from a clean regnet chain.
    for name in ("static/regmode.db", "static/index_reg.db",
                 "static/regmode.db-shm", "static/regmode.db-wal", "node.log"):
        try:
            os.remove(os.path.join(ROOT, name))
        except OSError:
            pass
    shutil.copy(os.path.join(ROOT, "tests/config_custom.txt"),
                os.path.join(ROOT, "config_custom.txt"))

    log = open(os.path.join(ROOT, "node_test.log"), "w")
    proc = subprocess.Popen([sys.executable, "node.py", REGNET_PARAM],
                            cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    # Poll for readiness instead of a blind sleep.
    deadline = time.time() + 90
    while time.time() < deadline:
        if _port_open():
            break
        if proc.poll() is not None:
            raise RuntimeError("node exited during startup; see node_test.log")
        time.sleep(0.5)
    else:
        proc.terminate()
        raise RuntimeError("regnet node did not open port 3030 in time")

    yield proc

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()
    log.close()


@pytest.fixture(scope="session")
def client(node_proc):
    from _lite_client import LiteClient
    wallet = os.path.join(ROOT, "wallet.der")
    c = LiteClient(wallet, port=PORT)
    # Make sure there are some funds to play with.
    c.mine(2)
    return c
