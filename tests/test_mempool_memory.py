"""
The in-memory mempool (mempool.Mempool) against a real LMDB ledger — no node process.

Covers the reads, maintenance and peer bookkeeping, and every merge path: admission of a correctly signed,
funded tx, and each rejection (format, frozen peer, locked ledger, bad fields, age, signature, balance,
already-in-ledger, already-pooled).

Run with: python3 -m pytest tests/test_mempool_memory.py -v
"""
import base64
import hashlib
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from Cryptodome.PublicKey import RSA  # noqa: E402

import essentials  # noqa: E402
import mempool as mp_mod  # noqa: E402
from block_store import BlockStore  # noqa: E402

SMALL = 1 << 26
OTHER = "b" * 56


class Cfg:
    version = "regnet"
    debug = False
    mandatory_message = {}


class Log:
    def __init__(self):
        self.msgs = []

    def warning(self, m, *a):
        self.msgs.append(str(m))
    info = error = warning


@pytest.fixture(scope="module")
def ident():
    key = RSA.generate(1024)
    pub_pem = key.publickey().exportKey().decode("utf-8")
    pub_b64 = base64.b64encode(pub_pem.encode("utf-8"))
    return key, pub_b64, hashlib.sha224(pub_pem.encode("utf-8")).hexdigest()


def _signed(ident, amount, ts=None, recipient=OTHER, op="", of=""):
    key, pub_b64, addr = ident
    ts = "%.2f" % (time.time() - 5 if ts is None else ts)
    return list(essentials.sign_rsa(ts, addr, recipient, amount, op, of, key, pub_b64))


@pytest.fixture
def ledger(tmp_path, ident):
    s = BlockStore(str(tmp_path / "ledger"), map_size=SMALL)
    addr = ident[2]
    s.commit_block(1, "%056x" % 1, [("1", "1000", "genesis", addr, "0", "g", "pk", "%056x" % 1, "0", "1", "1",
                                     "genesis")], difficulty=16)
    s.commit_block(2, "%056x" % 2, [("2", "1060", addr, addr, "0", "cb2", "pk", "%056x" % 2, "0", "10", "0",
                                     "n")], difficulty=16)
    yield s                                                     # addr holds 11 BIS
    s.close()


@pytest.fixture
def pool():
    return mp_mod.Mempool(Log(), Cfg(), threading.Lock())


def _put(pool, sig, sender="a" * 56, recip=OTHER, amount="1.00000000", ts=None, of="", merged=None):
    ts = "%.2f" % (time.time() if ts is None else ts)
    pool._txs[sig] = (ts, sender, recip, amount, sig, "pk", "", of, int(time.time()) if merged is None else merged)


# ------------------------------------------------------------------------------------------- reads/maintenance
def test_reads(pool):
    _put(pool, "s1", amount="2.00000000", of="hello")
    _put(pool, "s2", sender=OTHER, recip="c" * 56, amount="3.00000000")
    assert [t[4] for t in pool.transactions()] == ["s2", "s1"]
    assert [t[4] for t in pool.wire_transactions()] == ["s1", "s2"]
    assert all(len(t) == 8 for t in pool.wire_transactions())
    assert pool.get("s1")[3] == "2.00000000" and pool.get("nope") is None
    assert pool.find_openfield("hello") == pool.get("s1")[0] and pool.find_openfield("x") is None
    assert {t[4] for t in pool.by_address(OTHER)} == {"s1", "s2"}
    assert pool.mp_get("a" * 56) == [("2.00000000", "hello", "")]
    assert pool.count() == 2 and pool.sig_check("s2")
    assert pool.status() == (2, 5, 2, 2)
    assert pool.size() > 0


def test_expired_txs_are_invisible_then_purged(pool):
    old = time.time() - mp_mod.REFUSE_OLDER_THAN - 10
    _put(pool, "old", ts=old)
    _put(pool, "new")
    assert [t[4] for t in pool.by_address(OTHER)] == ["new"]
    assert len(pool.by_address(OTHER, valid_only=False)) == 2
    assert pool.mp_get("a" * 56) == [("1.00000000", "", "")]
    pool.purge()
    assert not pool.sig_check("old") and pool.sig_check("new")
    pool.clear()
    assert pool.count() == 0 and pool.status()[0] == 0


def test_peer_send_bookkeeping(pool):
    _put(pool, "early", merged=100)
    _put(pool, "late", merged=int(time.time()) + 100)
    assert pool.sendable("1.2.3.4")
    assert {t[4] for t in pool.tx_to_send("1.2.3.4")} == {"early", "late"}
    pool.sent("1.2.3.4")
    assert not pool.sendable("1.2.3.4")
    assert [t[4] for t in pool.tx_to_send("1.2.3.4")] == ["late"]           # only what is new to the peer
    assert pool.tx_to_send("1.2.3.4", peer_txs=[pool.get("late")[:8]]) == []  # minus what it sent us
    pool.peers_sent["1.2.3.4"] = time.time() - 31
    assert pool.sendable("1.2.3.4")


def test_space_left_tightens_with_size():
    tx = ["0", "a", "b", "0", "s", "p", "", ""]
    big = ["0", "a", "b", "0", "s", "p", "", "x" * 200000]
    assert mp_mod.Mempool.space_left_for_tx(None, tx, 0.1)
    assert not mp_mod.Mempool.space_left_for_tx(None, tx, 0.35)              # base fee only
    assert mp_mod.Mempool.space_left_for_tx(None, big, 0.45)
    assert not mp_mod.Mempool.space_left_for_tx(None, big, 0.7)


# ------------------------------------------------------------------------------------------- merge
def test_merge_admits_a_signed_funded_tx(pool, ledger, ident):
    tx = _signed(ident, 1.5)
    res = pool.merge(tx, "127.0.0.1", ledger=ledger)
    assert res[-1] == "Success", res
    assert pool.count() == 1
    pooled = pool.get(tx[4])
    assert pooled[1] == ident[2] and pooled[3] == "1.50000000"
    # the same tx again
    assert "That transaction is already in our mempool" in pool.merge(tx, "127.0.0.1", ledger=ledger)


def test_merge_accepts_a_list_and_a_db_handler(pool, ledger, ident):
    from dbhandler import DbHandler
    txs = [_signed(ident, 1), _signed(ident, 2)]
    res = pool.merge(txs, "127.0.0.1", ledger=DbHandler(ledger))
    assert res.count("Success") == 2 and pool.count() == 2


def test_merge_reads_the_node_ledger_when_none_is_given(ledger, ident):
    class Node:
        pass
    node = Node()
    node.ledger = ledger
    pool = mp_mod.Mempool(Log(), Cfg(), threading.Lock(), node=node)
    assert pool.merge(_signed(ident, 1), "127.0.0.1")[-1] == "Success"


def test_merge_counts_pending_sends_against_the_balance(pool, ledger, ident):
    assert pool.merge(_signed(ident, 6), "127.0.0.1", ledger=ledger)[-1] == "Success"
    res = pool.merge(_signed(ident, 5), "127.0.0.1", ledger=ledger)        # 11 - 6 - fees < 5
    assert "Mempool: Cannot afford to pay fees" in res
    assert "Mempool: Sending more than owned" in pool.merge(_signed(ident, 12), "127.0.0.1", ledger=ledger)


def test_merge_rejects_a_tx_already_in_the_ledger(pool, ledger, ident):
    tx = _signed(ident, 1, ts=time.time() - 3600)
    ledger.commit_block(3, "%056x" % 3, [("3", tx[0], tx[1], tx[2], tx[3], tx[4], "pk", "%056x" % 3, "0.01", "0",
                                          "", ""),
                                         ("3", "1120", ident[2], ident[2], "0", "cb3", "pk", "%056x" % 3, "0", "1",
                                          "0", "")])
    res = pool.merge(tx, "9.9.9.9", ledger=ledger)
    assert "That transaction is already in our ledger" in res
    assert pool.peers_sent["9.9.9.9"] > time.time()         # an old already-mined tx freezes a syncing peer
    assert "Mempool ignoring merge from frozen 9.9.9.9" in pool.merge(tx, "9.9.9.9", ledger=ledger)
    # a pooled copy of a now-mined tx is dropped
    pool._txs[tx[4]] = tuple(tx) + (0,)
    assert "Mempool: Transaction deleted from our mempool" in pool.merge(tx, "127.0.0.1", ledger=ledger)
    assert not pool.sig_check(tx[4])


@pytest.mark.parametrize("mutate,expected", [
    (lambda t: t.__setitem__(0, "notatime"), "Mempool: Invalid timestamp"),
    (lambda t: t.__setitem__(1, "zz"), "Mempool: Invalid address"),
    (lambda t: t.__setitem__(2, "zz"), "Mempool: Invalid recipient"),
    (lambda t: t.__setitem__(3, "lots"), "Mempool: Invalid amount"),
    (lambda t: t.__setitem__(4, "S" * (essentials.MAX_TX_SIGNATURE_LEN + 1)), "Mempool: Invalid signature len"),
    (lambda t: t.__setitem__(6, "o" * 31), "Mempool: Invalid operation len"),
    (lambda t: t.__setitem__(0, "%.2f" % (time.time() + 600)), "Mempool: Future transaction rejected"),
    (lambda t: t.__setitem__(0, "%.2f" % (time.time() - mp_mod.REFUSE_OLDER_THAN - 60)), "Mempool: Too old"),
    (lambda t: t.__setitem__(3, "-1"), "Mempool: Negative balance spend attempt"),
    (lambda t: t.__setitem__(3, "1.00000001"), "Mempool: Signature did not match"),
])
def test_merge_rejections(pool, ledger, ident, mutate, expected):
    tx = _signed(ident, 1)
    mutate(tx)
    res = pool.merge(tx, "127.0.0.1", ledger=ledger)
    assert any(r.startswith(expected) for r in res), res
    assert pool.count() == 0


def test_merge_mandatory_message(pool, ledger, ident):
    Cfg.mandatory_message = {OTHER: "exchange needs a memo"}
    try:
        res = pool.merge(_signed(ident, 1), "127.0.0.1", ledger=ledger)
    finally:
        Cfg.mandatory_message = {}
    assert "Mempool: Missing message - exchange needs a memo" in res


def test_merge_format_guards(pool, ledger):
    assert pool.merge([], "1.1.1.1", ledger=ledger) == ["Mempool from 1.1.1.1 was empty"]
    with pytest.raises(ValueError):
        pool.merge("*", "1.1.1.1", ledger=ledger)
    assert pool.merge(42, "1.1.1.1", ledger=ledger) == ["Bad TX Format"]
    assert pool.peers_sent["1.1.1.1"] > time.time() + 9 * 60                  # frozen for 10 min
    assert pool.merge(42, "127.0.0.1", ledger=ledger) == ["Bad TX Format"]
    assert "127.0.0.1" not in pool.peers_sent                                 # never freeze ourselves


def test_merge_drops_while_a_block_is_digesting(ledger, ident):
    lock = threading.Lock()
    pool = mp_mod.Mempool(Log(), Cfg(), lock)
    tx = _signed(ident, 1)
    with lock:
        assert pool.merge(tx, "127.0.0.1", ledger=ledger) == ["Locked ledger, dropping txs"]
        # revert=True is the digester itself handing txs back: it holds the lock already
        assert pool.merge(tx, "127.0.0.1", ledger=ledger, revert=True)[-1] == "Success"


def test_merge_full_pool_skips_unless_bypassed(pool, ledger, ident, monkeypatch):
    monkeypatch.setattr(pool, "size", lambda: 0.9)
    tx = _signed(ident, 1)
    assert "Local mempool is already full for this tx type, skipping merging" in \
        pool.merge(tx, "127.0.0.1", ledger=ledger)
    assert pool.merge(tx, "127.0.0.1", ledger=ledger, size_bypass=True)[-1] == "Success"


def test_merge_is_thread_safe(pool, ledger, ident):
    txs = [_signed(ident, 0.1) for _ in range(20)]
    results = []
    threads = [threading.Thread(target=lambda t=t: results.append(pool.merge(t, "127.0.0.1", ledger=ledger)))
               for t in txs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert pool.count() == len({t[4] for t in txs})
    assert sum(r[-1] == "Success" for r in results) == pool.count()
