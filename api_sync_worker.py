"""
API-sync consume loop (the catch-up wiring api_sync.py left "deliberately separate").

When ``api_sync`` is enabled and an ``api_sync_source`` peer is configured, this runs a background daemon
that pulls the chain from that peer's REST API and feeds it to the digester — the modern replacement for
the legacy socket sync (doc/17 / the "API fork"): a node catches up entirely over HTTP, no socket
handshake. It is purely a TRANSPORT swap — the blocks api_sync returns are digested by the SAME
``digest_block`` the socket path uses, so consensus validation (PoW, signatures, balances, hash linkage,
fork gating) is byte-identical regardless of how the block arrived.

Default-OFF and capability-gated: api_sync.sync_segment fail-softs to None if the peer isn't REST-capable
or a segment doesn't validate, and the loop simply backs off and retries (a real node would fall back to
socket sync). It NEVER weakens validation — a bad block is rejected by the digester exactly as a
socket-delivered one would be.
"""
import threading
import time

import api_sync
import dbhandler
import rest_client
from digest import digest_block

BATCH = 50          # blocks per fetch+digest cycle (api_sync pulls headers then bodies in parallel)
IDLE_SLEEP = 2.0    # caught up: poll the peer tip this often
RETRY_SLEEP = 3.0   # transient (peer unreachable / segment didn't validate / digest error): back off


def start(node):
    """Spawn the consume loop iff enabled + a source is set. Returns the thread (or None)."""
    if not getattr(node, "api_sync_enabled", False):
        return None
    src = (getattr(node, "api_sync_source", "") or "").strip()
    if not src:
        node.logger.app_log.warning("Status: api_sync enabled but no api_sync_source set — not starting")
        return None
    try:
        host, rport = src.rsplit(":", 1)
        host, rport = host.strip(), int(rport)
    except Exception:
        node.logger.app_log.warning("Status: api_sync_source %r is not host:rest_port — not starting" % src)
        return None
    t = threading.Thread(target=_loop, args=(node, host, rport), name="api-sync", daemon=True)
    t.start()
    node.logger.app_log.warning("Status: API-sync consume loop started (source %s:%s)" % (host, rport))
    return t


def _tip(node):
    return int(getattr(node, "last_block", 0) or 0)


def _loop(node, host, rport):
    # Own DB handler for this thread; digest_block serializes on node.db_lock, so this never races the
    # node's own digestion.
    db = dbhandler.DbHandler(node.index_db, node.ledger_path, node.hyper_path, node.ram,
                             node.ledger_ram_file, node.logger, trace_db_calls=node.trace_db_calls)
    stall = 0
    try:
        while not getattr(node, "IS_STOPPING", False):
            try:
                peer_tip = rest_client.get_height(host, rport)
                my_tip = _tip(node)
                if peer_tip is None:
                    time.sleep(RETRY_SLEEP); continue
                if peer_tip <= my_tip:
                    stall = 0
                    time.sleep(IDLE_SLEEP); continue

                start_h, end_h = my_tip + 1, min(my_tip + BATCH, peer_tip)
                blocks = api_sync.sync_segment(host, rport, start_h, end_h)
                if not blocks:
                    node.logger.app_log.warning(
                        "api_sync: segment [%s-%s] from %s:%s not available/valid — backing off"
                        % (start_h, end_h, host, rport))
                    time.sleep(RETRY_SLEEP); continue

                digest_block(node, blocks, None, host, db)

                if _tip(node) > my_tip:
                    stall = 0
                    node.logger.app_log.warning(
                        "api_sync: ingested [%s-%s] over REST -> tip %s" % (start_h, end_h, _tip(node)))
                else:
                    # digester accepted nothing (e.g. blocks we already hold / a reorg in flight): don't
                    # hot-loop the same range.
                    stall += 1
                    time.sleep(RETRY_SLEEP * min(stall, 5))
            except Exception as e:
                node.logger.app_log.warning("api_sync loop error: %s" % e)
                time.sleep(RETRY_SLEEP)
    finally:
        try:
            db.close()
        except Exception:
            pass
