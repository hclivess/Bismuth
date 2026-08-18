"""
Chain-maintenance operations extracted from ``node.py``: single-block rollback, hyperblock
recompression, the ledger/hyper cross-integrity height check, and the ``blocknf`` ("block not found")
rollback handler.

These are the ledger-maintenance functions that already took ``node``/``db_handler`` explicitly, so
they lift out of the node monolith by dependency injection with no behaviour change — every free name
here is either a parameter or a module imported below (``sql_trace_callback`` comes from its canonical
home in ``dbhandler``, not a 5th copy). ``node.py`` re-exports ``blocknf`` so ``worker.py``'s
``from node import blocknf`` keeps working.
"""
import functools
import glob
import os
import shutil
import sqlite3
import tarfile
import threading
import time
from decimal import Decimal

import essentials
import mempool as mp
import tokensv2 as tokens
from dbhandler import sql_trace_callback
from essentials import download_file
from quantizer import quantize_eight


def _rollback_aux_stores(node, keep_height):
    """Roll back EVERY integrated height-keyed auxiliary store to ``keep_height`` (the new tip) in ONE
    place, so a chain rollback (reorg) can never leave a store out of sync with the ledger. Each is
    guarded + logged; stores that aren't enabled skip. The balance index / reward sidechain join this
    list when they are wired into the read path. (Depth is bounded upstream by the blocknf /
    rollback_depth / rollback_consensus checks — this only mirrors the already-bounded ledger rollback,
    so it adds no new deep-reorg attack surface.)"""
    for label, store in (("block store", getattr(node, "block_store", None)),
                         ("reward sidechain", getattr(node, "reward_chain", None))):
        if store is None:
            continue
        try:
            removed = store.rollback(keep_height)
            node.logger.app_log.warning(f"Status: {label} rolled back to {keep_height} ({removed} removed)")
        except Exception as e:
            node.logger.app_log.warning(f"{label} rollback failed: {e}")


def _rebuild_derived_state(node, db_handler, keep_height):
    """Rebuild every DERIVED consensus projection from the just-rolled-back ledger so a reorg can never
    leave a store AHEAD of the ledger. ``keep_height`` is the new tip (highest block KEPT). This MUST run
    on EVERY rollback path (rollback / blocknf / sequencing_check). The height-keyed DELETE stores
    (ledger / tokens / aliases) are rolled back at each call site with the site's own boundary; this
    rebuilds the RE-EXECUTABLE projections. All guarded + logged; disabled stores skip."""
    # auxiliary block stores (block-store / reward sidechain) keep <= keep_height
    _rollback_aux_stores(node, keep_height)
    # the balance index is a full-ledger projection (rewards baked into balances) — rebuild from the cursor
    if getattr(node, "balance_index", None) is not None:
        try:
            node.balance_index.rebuild_from_cursor(db_handler.c)
        except Exception as e:
            # doc/26 stage 4 (S1): consensus-critical once the overspend read trusts the index — a failed
            # reorg rebuild would leave it ahead of the ledger; HALT rather than drift. Inert while off.
            if getattr(node, "balance_index_consensus", "off") != "off":
                raise
            node.logger.app_log.warning(f"balance index rollback rebuild failed: {e}")
    # the txid index is a full-ledger post-fork projection (doc/26 stage 4) — rebuild from the cursor so the
    # dup-replay read can never trust txids confirmed above the new tip. HALT (not drift) when consensus-on.
    if getattr(node, "txid_index", None) is not None:
        try:
            node.txid_index.rebuild_from_cursor(db_handler.c, getattr(node, "fork_height", None))
        except Exception as e:
            if getattr(node, "txid_index_consensus", "off") != "off":
                raise
            node.logger.app_log.warning(f"txid index rollback rebuild failed: {e}")
    # hf2 pubkey-by-reference (doc/40): rebuild the address->pubkey registry from the FULL ledger cursor
    # (db_handler.h, NOT the pruned .c) so a post-reorg by-reference tx can never resolve a key registered
    # in a block above the new tip. Deterministic full reconstruction; HALT (not drift) when consensus-on.
    if getattr(node, "pk_registry", None) is not None:
        try:
            node.pk_registry.rebuild_from_cursor(db_handler.h, getattr(node, "fork_height", None))
        except Exception as e:
            if getattr(node, "pk_registry_consensus", "off") != "off":
                raise
            node.logger.app_log.warning(f"pubkey registry rollback rebuild failed: {e}")


def rollback(node, db_handler, block_height):
    node.logger.app_log.warning(f"Status: Rolling back below: {block_height}")

    db_handler.rollback_under(block_height)

    # rollback indices
    db_handler.tokens_rollback(node, block_height)
    db_handler.aliases_rollback(node, block_height)
    # rollback indices

    # rollback_under(block_height) drops heights >= block_height, so the derived stores keep <= height-1
    _rebuild_derived_state(node, db_handler, block_height - 1)

    node.logger.app_log.warning(f"Status: Chain rolled back below {block_height} and will be resynchronized")


def _rollup_balances(conn, depth_specific):
    """Collapsed {recipient_address: balance} for the hyperblock rollup of all blocks in the prune range
    (``block_height`` in the open interval that excludes the kept tail and the negative mirror rows).

    Replaces the old O(distinct-recipients x 2 index scans) per-address double loop — which on a deep
    rollup issues millions of indexed queries and runs for hours — with a SINGLE range scan that
    accumulates per-address credit/debit in memory. The arithmetic is byte-identical to the original:
    the same ``quantize_eight`` is applied per entry before summing (Decimal sums are exact and
    order-independent), and — exactly like the original, which iterates ``distinct(recipient)`` — only
    addresses that appear as a RECIPIENT in the range get a collapsed row (``credit - debit`` if > 0).
    The legacy ``except -> 0`` per-address reset is preserved (it is dead code on numeric decimal-mode
    data, but kept so behaviour is identical)."""
    credit, debit = {}, {}
    cur = conn.cursor()
    cur.execute("SELECT recipient, amount, reward, address, fee FROM transactions "
                "WHERE block_height < ? AND block_height > ?", (depth_specific, -depth_specific))
    for rcpt, amount, reward, addr, fee in cur:
        try:
            credit[rcpt] = quantize_eight(credit.get(rcpt, Decimal("0"))) + quantize_eight(amount) + quantize_eight(reward)
        except Exception:
            credit[rcpt] = 0
        try:
            debit[addr] = quantize_eight(debit.get(addr, Decimal("0"))) + quantize_eight(amount) + quantize_eight(fee)
        except Exception:
            debit[addr] = 0
    cur.close()
    out = {}
    for addr in credit:                      # recipients only — matches the original distinct(recipient)
        end_balance = quantize_eight(Decimal(credit[addr]) - Decimal(debit.get(addr, 0)))
        if end_balance > 0:
            out[addr] = end_balance
    return out


def recompress_ledger(node, rebuild=False, depth=15000):
    # TODO: Candidate for single user mode
    # HARDFORK / cleanup (doc/16): NOT integer-storage safe — this hyperblock rollup sums amount/reward
    # with bare quantize_eight() (which would read integer atomic units as whole BIS) and writes the
    # collapsed balance into a synthetic address='Hyperblock' mirror row as a decimal STRING. It also
    # swallows conversion errors with `except: credit = 0`. Regnet/tests don't exercise pruning, so it
    # is knowingly left legacy; it MUST be converted (amounts.ledger_value on reads, integer units on
    # write) and the mirror-row hack replaced (phase 5) before ledger_integer_amounts is set on mainnet.
    node.logger.app_log.warning(f"Status: Recompressing, please be patient")

    files_remove = [node.ledger_path + '.temp',node.ledger_path + '.temp-shm',node.ledger_path + '.temp-wal']
    for file in files_remove:
        if os.path.exists(file):
            os.remove(file)
            node.logger.app_log.warning(f"Removed old {file}")

    if rebuild:
        node.logger.app_log.warning(f"Status: Hyperblocks will be rebuilt")

    # Consistent snapshot of the source before copying. The source (hyper.db, or ledger.db when
    # rebuilding) is a WAL-mode DB, so committed rows can still live in its `-wal` sidecar that hasn't
    # been checkpointed into the main file yet. `shutil.copy` copies ONLY the main file, so without a
    # checkpoint first the copy silently drops those committed frames — an inconsistent snapshot that
    # is BEHIND the real tip. A non-clean exit (an impatient double Ctrl-C / hard kill — the graceful
    # stop flag is only honoured by the main loop, not during this startup step) is exactly when the WAL
    # is left un-checkpointed, which is why this only bites after an unclean stop. TRUNCATE folds the
    # frames into the main file and empties the WAL so the copy is complete.
    source = node.ledger_path if rebuild else node.hyper_path
    try:
        src_conn = sqlite3.connect(source, timeout=30)
        src_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        src_conn.close()
    except sqlite3.Error as e:
        node.logger.app_log.warning(f"Recompress: could not checkpoint {source} before copy ({e}); "
                                    f"proceeding with current main-file state")
    shutil.copy(source, node.ledger_path + '.temp')
    hyper = sqlite3.connect(node.ledger_path + '.temp', timeout=30)
    if node.trace_db_calls:
       hyper.set_trace_callback(functools.partial(sql_trace_callback,node.logger.app_log,"HYPER"))
    hyper.text_factory = str
    hyp = hyper.cursor()

    hyp.execute("UPDATE transactions SET address = 'Hypoblock' WHERE address = 'Hyperblock'")

    hyp.execute("SELECT max(block_height) FROM transactions")
    db_block_height = int(hyp.fetchone()[0])
    depth_specific = db_block_height - depth

    # OPTIMIZED: one range scan + in-memory accumulation (see _rollup_balances) instead of the old
    # per-recipient double index-scan loop that ran for hours on a deep rollup. Byte-identical output.
    rollup = _rollup_balances(hyper, depth_specific)
    timestamp = str(time.time())
    for addr, end_balance in rollup.items():
        hyp.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
            depth_specific - 1, timestamp, "Hyperblock", addr, str(end_balance), "0", "0", "0", "0",
            "0", "0", "0"))
    hyper.commit()

    hyp.execute(
        "DELETE FROM transactions WHERE address != 'Hyperblock' AND (block_height < ? AND block_height > ?);",
        (depth_specific, -depth_specific,))
    hyper.commit()

    hyp.execute("DELETE FROM misc WHERE (block_height < ? AND block_height > ?);",
                (depth_specific, -depth_specific,))  # remove diff calc
    hyper.commit()

    hyp.execute("VACUUM")
    # Fold the temp DB's own WAL back into its main file before we close+rename, so `.temp` is a single
    # self-contained file. Otherwise a surviving `.temp-wal` would either be dropped by the rename (main
    # file only) or, worse, get mis-associated with the renamed hyper.db.
    hyp.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    hyper.close()

    if os.path.exists(node.hyper_path):
        # Remove the old hyperblock DB **and its WAL sidecars**. Removing only hyper.db (the old
        # behaviour) left any stale `hyper.db-wal`/`-shm` from a non-clean exit orphaned next to the
        # freshly-renamed DB; SQLite then mis-associates that sidecar (whose page refs belong to the OLD
        # file) with the new hyper.db on the next open, wedging WAL recovery ("disk image is malformed" /
        # indefinite stall) — the "hangs at Recompressing" that only a wipe + ledger.tar.gz re-extract
        # cleared. bootstrap()/seed_genesis() already clear these sidecars on every DB swap; match them.
        for ext in ("", "-wal", "-shm"):
            try:
                os.remove(node.hyper_path + ext)
            except OSError:
                pass
        # Drop any leftover temp sidecars too (the checkpoint above should have removed them) so only the
        # self-contained `.temp` main file is promoted to hyper.db.
        for ext in ("-wal", "-shm"):
            try:
                os.remove(node.ledger_path + '.temp' + ext)
            except OSError:
                pass
        os.rename(node.ledger_path + '.temp', node.hyper_path)


def ledger_check_heights(node, db_handler):
    # TODO: Candidate for single user mode
    """conversion of normal blocks into hyperblocks from ledger.db or hyper.db to hyper.db"""
    if os.path.exists(node.hyper_path):

        # cross-integrity check
        hdd_block_max = db_handler.block_height_max()
        hdd_block_max_diff = db_handler.block_height_max_diff()
        hdd2_block_last = db_handler.block_height_max_hyper()
        hdd2_block_last_misc = db_handler.block_height_max_diff_hyper()

        # cross-integrity check

        if hdd_block_max == hdd2_block_last == hdd2_block_last_misc == hdd_block_max_diff and node.hyper_recompress:  # cross-integrity check
            node.logger.app_log.warning("Status: Recompressing hyperblocks (keeping full ledger)")
            node.recompress = True

            #print (hdd_block_max,hdd2_block_last,node.hyper_recompress)
        elif hdd_block_max == hdd2_block_last and not node.hyper_recompress:
            node.logger.app_log.warning("Status: Hyperblock recompression skipped")
            node.recompress = False
        else:
            lowest_block = min(hdd_block_max, hdd2_block_last, hdd_block_max_diff, hdd2_block_last_misc)
            highest_block = max(hdd_block_max, hdd2_block_last, hdd_block_max_diff, hdd2_block_last_misc)

            node.logger.app_log.warning(
                f"Status: Cross-integrity check failed (db heights span {lowest_block}..{highest_block}); "
                f"trimming the excess above the common height {lowest_block} (kept) and resyncing from there")

            # rollback drops heights >= its arg, so pass lowest_block+1 to KEEP lowest_block (the height all
            # DBs share) and discard only the excess above it — minimal, instead of nuking a valid block.
            rollback(node, db_handler, lowest_block + 1)
            node.recompress = False

    else:
        node.logger.app_log.warning("Status: Compressing ledger to Hyperblocks")
        node.recompress = True


def _tips_of(path):
    """Return (max transactions height, max misc height) for a ledger/hyper SQLite file, or (None, None)
    if the file or tables don't exist yet. Uses a private short-lived connection — NEVER the cached
    DbHandler reads (block_height_max has a 1s cache) — so reconciliation always sees ground truth."""
    if not os.path.exists(path):
        return (None, None)
    conn = sqlite3.connect(path, timeout=5)
    try:
        c = conn.cursor()
        try:
            c.execute("SELECT max(block_height) FROM transactions")
            tx = c.fetchone()[0]
            c.execute("SELECT max(block_height) FROM misc")
            misc = c.fetchone()[0]
            return (tx, misc)
        except sqlite3.OperationalError:
            # tables not created yet (fresh/empty db)
            return (None, None)
    finally:
        conn.close()


def _marker_of(path):
    """Return the lockstep ``commit_marker.committed_height`` for a store, or None when the file/table
    is absent (legacy DB never written by the lockstep code) or the marker was never set.

    db_to_drive advances ledger.db's and hyper.db's markers TOGETHER in one transaction, so the marker
    is "the height at which both files agreed at the last flush". When BOTH markers are present, the
    common consistent height is exactly min(markers) — an UNAMBIGUOUS, monotonic recovery floor that
    does not depend on inferring direction from the four tips. None ⇒ fall back to tip inference."""
    if not os.path.exists(path):
        return None
    conn = sqlite3.connect(path, timeout=5)
    try:
        try:
            row = conn.execute("SELECT committed_height FROM commit_marker WHERE id = 0").fetchone()
            return row[0] if row else None
        except sqlite3.OperationalError:
            return None
    finally:
        conn.close()


def _set_marker(path, height):
    """Best-effort: set a store's commit_marker to `height` (used after a reconcile-trim so the marker
    matches the new tip). Creates the table+row if missing so post-reconcile stores carry the marker
    going forward. Never raises — recovery already converged on the rows; the marker is the signal."""
    if not os.path.exists(path):
        return
    conn = sqlite3.connect(path, timeout=5)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS commit_marker "
            "(id INTEGER PRIMARY KEY CHECK(id = 0), committed_height INTEGER)")
        conn.execute(
            "INSERT INTO commit_marker (id, committed_height) VALUES (0, ?) "
            "ON CONFLICT(id) DO UPDATE SET committed_height = excluded.committed_height", (height,))
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()


def _trim_store_to(path, keep_height, label, app_log, trace_db_calls=False):
    """Delete every height ABOVE keep_height from one store's transactions+misc, so its tip becomes
    exactly keep_height. Mirrors rollback_under's delete shape (the negative-height mirror rows for a
    height live at -height, so `> keep_height` already keeps them). Returns rows removed from
    transactions."""
    conn = sqlite3.connect(path, timeout=5)
    if trace_db_calls:
        conn.set_trace_callback(functools.partial(sql_trace_callback, app_log, "RECONCILE"))
    conn.text_factory = str
    try:
        c = conn.cursor()
        c.execute("SELECT count(*) FROM transactions WHERE block_height > ?", (keep_height,))
        removed = c.fetchone()[0]
        c.execute("DELETE FROM transactions WHERE block_height > ?", (keep_height,))
        c.execute("DELETE FROM misc WHERE block_height > ?", (keep_height,))
        conn.commit()
        app_log.warning(
            f"Status: Reconcile trimmed {label} ({path}) down to height {keep_height} "
            f"({removed} transaction-rows removed)")
        return removed
    finally:
        conn.close()


def reconcile_ledger_hyper(node):
    """Deterministic startup reconciliation of the full ledger vs the hyperblock rollup.

    Bismuth keeps two on-disk chains that are written by SEPARATE code paths / connections at
    different moments per block (digest.to_db -> hyper.db via `conn`; db_to_drive -> ledger.db via
    `hdd`), with NO atomic cross-DB transaction. An unclean exit (OOM SIGKILL, power loss) can commit
    one file a few blocks ahead of the other. The runtime then derives node.hdd_block from ledger.db
    (block_height_max -> self.h) but reads its working tip/last hash from hyper.db (block_max_ram /
    last_block_hash -> self.c); when the two disagree, the FIRST digest after restart rejects the next
    block ("... already in ledger"), handle_processing_error snaps node.hdd_block down to the shorter
    store, and the node wedges re-fetching a height it already half-has.

    This heals that split BEFORE syncing (and before ledger_check_heights / recompress, which assume
    the two are already coherent): roll whichever store is ahead back to the common consistent height
    (the min of all four tips), so both files end at one identical, contiguous tip. The trimmed blocks
    are re-downloadable from peers. This is recovery, not validation — it never runs in the digest path
    and only removes the divergent excess above the shared height, never the agreed bulk.

    In hyperblock mode (full_ledger=False) ledger_path and hyper_path are the same file (the ledger is
    a clone of the hyperblocks), so there is nothing to reconcile and we return early.
    """
    app_log = node.logger.app_log

    # full_ledger=False clones hyper->ledger (same path); nothing to cross-check.
    if getattr(node, "ledger_path", None) == getattr(node, "hyper_path", None):
        return

    if not (os.path.exists(node.ledger_path) and os.path.exists(node.hyper_path)):
        # one side absent (fresh node / hyperblock builder will create it) -> nothing to reconcile.
        return

    ledger_tx, ledger_misc = _tips_of(node.ledger_path)
    hyper_tx, hyper_misc = _tips_of(node.hyper_path)

    tips = [t for t in (ledger_tx, ledger_misc, hyper_tx, hyper_misc) if t is not None]
    if not tips:
        return  # both empty

    # Lockstep markers (db_to_drive stamps both files together): "the height at which both files agreed
    # at the last flush". When BOTH are present they give an UNAMBIGUOUS recovery floor = min(markers),
    # independent of inferring direction from the four tips. Absent/NULL ⇒ legacy DB ⇒ tip inference.
    ledger_marker = _marker_of(node.ledger_path)
    hyper_marker = _marker_of(node.hyper_path)
    markers_usable = ledger_marker is not None and hyper_marker is not None

    if ledger_tx == ledger_misc == hyper_tx == hyper_misc and (
            not markers_usable or ledger_marker == hyper_marker == ledger_tx):
        return  # already coherent — common, fast path; stay silent to avoid log noise.

    tip_common = min(tips)
    if markers_usable:
        # Marker floor is authoritative for DIRECTION, but clamp by the real tips so a marker can never
        # ask us to "keep" a height whose rows aren't actually present (defence in depth — the marker is
        # written in-txn with the rows, so this clamp is belt-and-suspenders).
        common = min(ledger_marker, hyper_marker, tip_common)
        basis = (f"lockstep markers (ledger={ledger_marker}, hyper={hyper_marker}; tips floor "
                 f"{tip_common})")
    else:
        common = tip_common
        basis = "tip inference (no lockstep markers present)"

    app_log.warning(
        f"Status: Ledger/hyper mismatch detected "
        f"(ledger tx={ledger_tx} misc={ledger_misc} marker={ledger_marker}; "
        f"hyper tx={hyper_tx} misc={hyper_misc} marker={hyper_marker}). "
        f"Reconciling both stores down to the common consistent height {common} via {basis} before sync.")

    # Trim only the file(s) that are actually above the common height. A store exactly at `common`
    # (or absent) is skipped, so we never rewrite a file needlessly.
    if (ledger_tx is not None and ledger_tx > common) or (ledger_misc is not None and ledger_misc > common):
        _trim_store_to(node.ledger_path, common, "ledger", app_log, node.trace_db_calls)
    if (hyper_tx is not None and hyper_tx > common) or (hyper_misc is not None and hyper_misc > common):
        _trim_store_to(node.hyper_path, common, "hyper", app_log, node.trace_db_calls)

    # Re-stamp both markers to the reconciled height so the files agree on the marker too (otherwise a
    # store we trimmed would still carry its old higher marker). Best-effort: a legacy file without the
    # table simply keeps None and the next clean db_to_drive sets it.
    _set_marker(node.ledger_path, common)
    _set_marker(node.hyper_path, common)

    # Verify both stores now agree on a single tip; if not, fail loud rather than boot wedged.
    l2_tx, l2_misc = _tips_of(node.ledger_path)
    h2_tx, h2_misc = _tips_of(node.hyper_path)
    if not (l2_tx == l2_misc == h2_tx == h2_misc == common):
        raise ValueError(
            f"Ledger/hyper reconciliation failed to converge: after trimming to {common} the tips are "
            f"ledger tx={l2_tx} misc={l2_misc}; hyper tx={h2_tx} misc={h2_misc}")
    app_log.warning(f"Status: Ledger/hyper reconciled to a single consistent tip {common}")


def blocknf(node, block_hash_delete, peer_ip, db_handler, hyperblocks=False, peer_height=None, peer_port=None):
    """A peer says it does not have our tip block ``block_hash_delete``.

    ``fork_resolution=measured`` (default): the nado-derived rules in ``fork_resolution`` — measure the
    common ancestor with tri-state probes, fetch the competing branch FIRST, prove it longer, tie-break a
    same-height fork once at the first divergent block, corroborate a below-checkpoint rollback with a
    hash-level peer majority — and only then roll back to the ancestor in ONE operation and apply the held
    branch through ``digest_block``. ``fork_resolution=legacy``: the old blind one-block rollback.

    ``peer_height`` / ``peer_port`` are what the peer advertised (its claimed tip, its listening port when
    known — outbound connections). Returns a dict describing what happened (``rolled``, ``state``,
    ``reason``) — callers may ignore it."""
    if getattr(node, "fork_resolution", "measured") == "legacy":
        _blocknf_legacy(node, block_hash_delete, peer_ip, db_handler, hyperblocks=hyperblocks)
        return {"rolled": None, "state": "legacy", "reason": ""}
    return _blocknf_measured(node, block_hash_delete, peer_ip, db_handler, hyperblocks=hyperblocks,
                             peer_height=peer_height, peer_port=peer_port)


def _our_hash_at_factory(db_handler):
    """``our_hash_at(height) -> block_hash | None`` against the FULL ledger cursor (what
    ``block_height_from_hash`` serves to peers, so both sides of a probe read the same store)."""
    def our_hash_at(height):
        try:
            db_handler.execute_param(db_handler.h, "SELECT block_hash FROM transactions WHERE block_height = ? "
                                                   "AND reward != 0 LIMIT 1;", (int(height),))
            row = db_handler.h.fetchone()
            return str(row[0]) if row and row[0] else None
        except Exception:
            return None
    return our_hash_at


def _blocknf_measured(node, block_hash_delete, peer_ip, db_handler, hyperblocks=False, peer_height=None,
                      peer_port=None):
    import fork_resolution as fr

    my_time = time.time()
    log = node.logger.app_log
    if not hasattr(node, "fork_lock"):
        node.fork_lock = threading.Lock()

    def _skip(reason, state=fr.UNKNOWN, height=None, bhash=None, cache_key=None, verdict=None):
        if cache_key is not None:
            fr._cache_put(node, cache_key, verdict or state)
        log.warning(f"Fork resolution ({peer_ip}): no rollback — {state}: {reason}")
        node.plugin_manager.execute_action_hook('rollback', {"timestamp": my_time, "height": height, "ip": peer_ip,
                                                             "hash": bhash, "skipped": True, "reason": reason})
        return {"rolled": False, "state": state, "reason": reason}

    # ---- cheap pre-gates (no locks, no network) ---------------------------------------------------------
    if node.db_lock.locked():
        return _skip("other ledger operation in progress")   # our tip is in flux; measure it next time
    try:
        tipinfo = db_handler.block_max_ram()
        tip, tip_hash = int(tipinfo['block_height']), str(tipinfo['block_hash'])
    except Exception as e:
        return _skip(f"cannot read our tip: {e}")
    ip = {'ip': peer_ip}
    node.plugin_manager.execute_filter_hook('filter_rollback_ip', ip)
    if ip['ip'] == 'no':
        return _skip("Filter blocked this rollback", height=tip, bhash=tip_hash)
    if tip_hash != block_hash_delete:
        return _skip("We moved away from the block to rollback, skipping", fr.SYNCED, tip, tip_hash)
    if hyperblocks and node.last_block_ago > 30000:
        return _skip(f"{peer_ip} is running on hyperblocks and our last block is too old, skipping",
                     fr.UNKNOWN, tip, tip_hash)
    if peer_height is None:
        try:
            peer_height = node.peers.peer_opinion_dict.get(peer_ip)
        except Exception:
            peer_height = None
    if peer_height is None:
        return _skip("peer advertised no height — no evidence", fr.UNKNOWN, tip, tip_hash)
    peer_height = int(peer_height)
    if peer_height < tip:
        return _skip(f"peer is at {peer_height} < our {tip}: a shorter chain never displaces ours",
                     fr.SYNCED, tip, tip_hash)
    cache_key = (tip_hash, peer_ip)
    cached = fr._cache_get(node, cache_key)
    if cached is not None:
        return _skip(f"verdict for this tip cached ({cached})", cached, tip, tip_hash)
    if not node.fork_lock.acquire(blocking=False):
        return _skip("another fork resolution is in progress", fr.UNKNOWN, tip, tip_hash)
    link = None
    try:
        # ---- measure: the ancestor, by tri-state probes against the advertising peer ------------------------
        our_hash_at = _our_hash_at_factory(db_handler)
        checkpoint = int(getattr(node, "checkpoint", 0) or 0)
        # Verdict first, donor second: measure against the advertiser when its listening port answers; an
        # inbound-only (NATed) advertiser can't be dialled, so fall back to an outbound peer at >= its height
        # that ALSO does not know our tip — the same branch, from a peer we can actually fetch from.
        link, src_ip = None, peer_ip
        for cand_ip, cand_port in fr.candidate_peers(node, first_ip=peer_ip, first_port=peer_port,
                                                     min_height=peer_height):
            cand = fr.PeerLink(cand_ip, cand_port, proxy=fr._proxy_of(node))
            if not cand.alive:
                cand.close(); continue
            if cand_ip != peer_ip and cand.knows(tip, tip_hash) is not False:
                cand.close(); continue           # unreachable/mute, or on OUR chain — not this branch's donor
            link, src_ip = cand, cand_ip
            break
        if link is None:
            link = fr.PeerLink(peer_ip, int(peer_port or getattr(node, "port", 5658) or 5658))   # dead: UNKNOWN below
        if src_ip != peer_ip:
            log.warning(f"Fork resolution ({peer_ip}): advertiser not dialable — measuring/fetching via {src_ip}")

        def floor_ok(ancestor):
            return (ancestor + 1) >= checkpoint          # the shallow policy: the removed block >= checkpoint

        def deep_ok(ancestor):
            # doc/14 reputation + height-consensus gate AND a hash-level majority: a strict majority of the
            # ANSWERING peers (the advertiser counts as one "no") must also not know our tip. Ignorance refuses.
            if not essentials.rollback_allowed(node, ancestor + 1):
                log.warning(f"Fork resolution ({peer_ip}): deep rollback to {ancestor} refused by the "
                            f"reputation/consensus gate (checkpoint {checkpoint})")
                return False
            others = [p for p in fr.candidate_peers(node, min_height=tip) if p[0] != peer_ip]

            def knows(peer, h, bh):
                l2 = fr.PeerLink(peer[0], peer[1], proxy=fr._proxy_of(node))
                try:
                    return l2.knows(h, bh) if l2.alive else None
                finally:
                    l2.close()
            verdict, answers, disagree = fr.majority_disagrees(tip, tip_hash, others, knows,
                                                               min_answers=max(1, fr.MIN_ANSWERS_DEEP - 1))
            answers, disagree = answers + 1, disagree + 1          # + the advertiser's own "no"
            ok = answers >= fr.MIN_ANSWERS_DEEP and disagree * 2 > answers
            log.warning(f"Fork resolution ({peer_ip}): deep-reorg corroboration at tip {tip}: {disagree}/{answers} "
                        f"answering peers do not know our tip -> {'corroborated' if ok else 'REFUSED'}")
            return ok

        v = fr.measure(node, our_hash_at, tip, tip_hash, link, peer_height, floor_ok, deep_ok)
        log.warning(f"Fork resolution ({peer_ip}, height {peer_height} vs our {tip}): {v.state} — {v.reason} "
                    f"[{v.probes} probes, ancestor {v.ancestor}]")
        if v.state != fr.REORG:
            # BEHIND = it said "not found" a moment ago but serves our tip now (it just synced it): no strike,
            # no revert — the ordinary forward sync handles whatever it has above us
            if v.state == fr.DEAD_FORK:
                log.warning(f"Fork resolution ({peer_ip}): DEAD FORK — our chain diverges from the peer's at "
                            f"{v.ancestor}, below what this node may roll back on this evidence. If this node "
                            f"is stuck on a minority fork: raise 'rollback_depth', check 'rollback_consensus', "
                            f"or drop a 'rollback_to' file (doc/30) / resync.")
            return _skip(v.reason, v.state, tip, tip_hash, cache_key)
        ancestor = int(v.ancestor)
        anc_hash = our_hash_at(ancestor)
        if not anc_hash:
            return _skip(f"no local hash at ancestor {ancestor}", fr.UNKNOWN, tip, tip_hash)

        # ---- same-height fork: decide ONCE at the first divergent block ------------------------------------
        tie = peer_height == tip
        if tie:
            theirs = link.hash_at(ancestor + 1)
            ours = our_hash_at(ancestor + 1)
            if fr.tie_winner(ours, theirs) == "ours":
                return _skip(f"same-height fork from {ancestor}: our branch wins the tie-break at {ancestor + 1} "
                             f"(ours {str(ours)[:12]} vs theirs {str(theirs)[:12]}) — the peer should reorg",
                             fr.TIE_WIN, tip, tip_hash, cache_key)
            log.warning(f"Fork resolution ({peer_ip}): same-height fork from {ancestor}: THEIR branch wins the "
                        f"tie-break at {ancestor + 1} (ours {str(ours)[:12]} vs theirs {str(theirs)[:12]})")

        # ---- possession: hold the competing branch BEFORE reverting anything ---------------------------------
        held, complete = fr.fetch_branch(link, ancestor, anc_hash, stop_height=peer_height)
        if not held:
            node.peers.warning(None, peer_ip, "Advertised a longer chain but served nothing from the ancestor", 2)
            return _skip(f"peer served nothing above the ancestor {ancestor} — nothing to adopt",
                         fr.UNKNOWN, tip, tip_hash, cache_key)
        their_tip = ancestor + len(held)
        longer = their_tip > tip or (tie and their_tip == tip)
        if not longer:
            if complete:
                node.peers.warning(None, peer_ip, f"Advertised {peer_height} but its branch ends at {their_tip}", 2)
            return _skip(f"held branch reaches {their_tip} (complete={complete}), not longer than our {tip} — "
                         f"possession disproved the advertisement", fr.SYNCED, tip, tip_hash, cache_key)

        # ---- roll back to the ancestor in ONE operation, then apply the held branch, UNDER ONE LOCK ----------
        # (rollback + apply must be atomic w.r.t. the other peer threads: a digest slipping in between would
        # build on the bare ancestor and strand the held branch AND our backup)
        if node.db_lock.locked():
            return _skip("other ledger operation in progress", fr.UNKNOWN, tip, tip_hash)
        node.db_lock.acquire()
        log.warning("Database lock acquired")
        backup_data, adopted, new_tip = None, False, tip
        try:
            log.warning(f"Fork resolution ({peer_ip}): REORG — rolling back {tip - ancestor} block(s) to the measured "
                        f"ancestor {ancestor} to adopt a held branch of {len(held)} block(s) reaching {their_tip}")
            backup_data = db_handler.backup_higher(ancestor + 1)
            _roll_to_locked(node, db_handler, ancestor)
            fr.invalidate(node)                  # the tip every cached verdict described no longer exists
            _apply_blocks_locked(node, db_handler, held, src_ip)
            new_tip = int(node.last_block)
            adopted = new_tip > tip or (tie and new_tip == tip and new_tip > ancestor)
            if not adopted:
                # ---- the held branch did not beat ours under full validation: restore OUR branch ------------
                log.warning(f"Fork resolution ({peer_ip}): peer's branch validated only to {new_tip} (< our {tip}) — "
                            f"restoring our own branch from the backup")
                if int(node.last_block) > ancestor:
                    rollback(node, db_handler, ancestor + 1)
                    _roll_to_locked(node, db_handler, ancestor)
                fr.invalidate(node)
                _apply_blocks_locked(node, db_handler, fr.blocks_from_rows(backup_data), "127.0.0.1")
                if int(node.last_block) < tip:
                    log.warning(f"Fork resolution ({peer_ip}): our branch restored only to {node.last_block} of {tip}; "
                                f"the rest will re-sync from peers")
        finally:
            node.db_lock.release()
            log.warning("Database lock released")

        if adopted:
            log.warning(f"Fork resolution ({peer_ip}): adopted the peer's branch — tip {tip} -> {new_tip} "
                        f"(ancestor {ancestor}, {new_tip - ancestor} block(s) applied)")
            _return_txs_to_mempool(node, db_handler, backup_data, peer_ip, my_time, tip_hash, "")
            return {"rolled": True, "state": fr.REORG, "reason": f"adopted longer branch from {ancestor}"}
        try:
            import peers_reputation
            node.peers.penalize(src_ip, peers_reputation.PENALTY_INVALID_BLOCK, "invalid competing branch")
        except Exception:
            pass
        node.peers.warning(None, src_ip, "Served a competing branch that failed validation", 5)
        fr._cache_put(node, (tip_hash, peer_ip), fr.UNKNOWN)
        return {"rolled": False, "state": fr.UNKNOWN, "reason": "competing branch failed validation; ours restored"}
    except Exception as e:
        log.warning(f"Fork resolution ({peer_ip}) failed: {type(e).__name__}: {e}")
        return {"rolled": False, "state": fr.UNKNOWN, "reason": str(e)}
    finally:
        if link is not None:
            link.close()
        node.fork_lock.release()


def _roll_to_locked(node, db_handler, ancestor):
    """Ledger + derived stores + node tip variables -> ``ancestor`` (caller holds node.db_lock and has already
    taken its backup). Mirrors the legacy blocknf mutation, for N blocks at once."""
    db_handler.rollback_under(ancestor + 1)
    db_handler.tokens_rollback(node, ancestor + 1)
    db_handler.aliases_rollback(node, ancestor + 1)
    _rebuild_derived_state(node, db_handler, ancestor)
    node.last_block_timestamp = db_handler.last_block_timestamp()
    node.last_block_hash = db_handler.last_block_hash()
    node.last_block = ancestor
    node.hdd_hash = db_handler.last_block_hash()
    node.hdd_block = ancestor
    tokens.tokens_update(node, db_handler)


def _apply_blocks_locked(node, db_handler, blocks, peer_ip):
    """Apply ``blocks`` through the ONE canonical validation path (digest.process_block_data: PoW, signatures,
    balances, hash linkage, fork gating) while the CALLER holds node.db_lock — the same body as
    digest.digest_block minus its own lock handling. Never raises: a rejected block leaves the chain at the
    last valid height (handle_processing_error) and the caller reads node.last_block."""
    import digest
    while mp.MEMPOOL is not None and mp.MEMPOOL.lock.locked():
        time.sleep(0.1)
    try:
        processor = digest.BlockProcessor(node, db_handler, peer_ip)
        digest.process_block_data(node, blocks, processor, db_handler, peer_ip)
        node.peers.reward(peer_ip)
        essentials.checkpoint_set(node)
    except Exception as e:
        try:
            digest.handle_processing_error(node, db_handler, None, peer_ip, e)   # raises after restoring the tip
        except Exception:
            pass
    finally:
        try:
            db_handler.db_to_drive(node)
        except Exception as e:
            node.logger.app_log.warning(f"Fork resolution: db_to_drive after apply failed: {e}")


def _return_txs_to_mempool(node, db_handler, backup_data, peer_ip, my_time, old_hash, reason):
    """After an adopted reorg: put the rolled-back non-coinbase txs back into the mempool (the mempool's
    merge de-duplicates against the ledger, so txs the new branch also contains are dropped) and fire the
    'rollback' hook — the same bookkeeping the legacy path did."""
    nb_tx, miner, height = 0, "", None
    try:
        for tx in backup_data or []:
            if tx[9] == 0:
                try:
                    nb_tx += 1
                    node.logger.app_log.info(
                        mp.MEMPOOL.merge((tx[1], tx[2], tx[3], tx[4], tx[5], tx[6], tx[10], tx[11]),
                                         peer_ip, db_handler.c, False, revert=True))
                except Exception as e:
                    node.logger.app_log.warning(f"Error during moving tx back to mempool: {e}")
            else:
                miner, height = tx[3], tx[0]
        node.plugin_manager.execute_action_hook('rollback', {"timestamp": my_time, "height": height, "ip": peer_ip,
                                                             "miner": miner, "hash": old_hash, "tx_count": nb_tx,
                                                             "skipped": False, "reason": reason})
    except Exception as e:
        node.logger.app_log.warning(f"Error during moving txs back to mempool: {e}")


def _blocknf_legacy(node, block_hash_delete, peer_ip, db_handler, hyperblocks=False):
    """
    LEGACY (fork_resolution=legacy) one-block blind rollback — kept verbatim as the opt-out path.
    Rolls back a single block, updates node object variables.
    Rollback target must be above checkpoint.
    Hash to rollback must match in case our ledger moved.
    Not trusting hyperblock nodes for old blocks because of trimming,
    they wouldn't find the hash and cause rollback.
    """
    node.logger.app_log.info(f"Rollback operation on {block_hash_delete} initiated by {peer_ip}")

    my_time = time.time()

    if not node.db_lock.locked():
        node.db_lock.acquire()
        node.logger.app_log.warning(f"Database lock acquired")
        backup_data = None  # used in "finally" section
        skip = False
        reason = ""

        try:
            block_max_ram = db_handler.block_max_ram()
            db_block_height = block_max_ram ['block_height']
            db_block_hash = block_max_ram ['block_hash']

            ip = {'ip': peer_ip}
            node.plugin_manager.execute_filter_hook('filter_rollback_ip', ip)
            if ip['ip'] == 'no':
                reason = "Filter blocked this rollback"
                skip = True

            elif not essentials.rollback_allowed(node, db_block_height):
                reason = (f"Block {db_block_height} is at/below the rollback checkpoint {node.checkpoint} and "
                          f"peer consensus does not justify a deeper rollback; refusing. If this node is stuck "
                          f"on a minority fork, raise 'rollback_depth', enable 'rollback_consensus', or resync.")
                node.logger.app_log.warning(reason)  # visible at default log level so the stall is diagnosable
                skip = True

            elif db_block_hash != block_hash_delete:
                # print db_block_hash
                # print block_hash_delete
                reason = "We moved away from the block to rollback, skipping"
                skip = True

            elif hyperblocks and node.last_block_ago > 30000: #more than 5000 minutes/target blocks away
                reason = f"{peer_ip} is running on hyperblocks and our last block is too old, skipping"
                skip = True

            else:
                backup_data = db_handler.backup_higher(db_block_height)

                node.logger.app_log.warning(f"Node {peer_ip} didn't find block {db_block_height}({db_block_hash})")

                # roll back hdd too
                db_handler.rollback_under(db_block_height)
                # /roll back hdd too

                # rollback indices
                db_handler.tokens_rollback(node, db_block_height)
                db_handler.aliases_rollback(node, db_block_height)
                # rebuild the re-executable projections (balance index, aux stores) — this is the LIVE
                # reorg path.
                _rebuild_derived_state(node, db_handler, db_block_height - 1)
                # /rollback indices

                node.last_block_timestamp = db_handler.last_block_timestamp()
                node.last_block_hash = db_handler.last_block_hash()
                node.last_block = db_block_height - 1
                node.hdd_hash = db_handler.last_block_hash()
                node.hdd_block = db_block_height - 1
                tokens.tokens_update(node, db_handler)

        except Exception as e:
            node.logger.app_log.warning(e)

        finally:
            node.db_lock.release()

            node.logger.app_log.warning(f"Database lock released")

            if skip:
                rollback = {"timestamp": my_time, "height": db_block_height, "ip": peer_ip,
                            "hash": db_block_hash, "skipped": True, "reason": reason}
                node.plugin_manager.execute_action_hook('rollback', rollback)
                node.logger.app_log.info(f"Skipping rollback: {reason}")
            else:
                try:
                    nb_tx = 0
                    for tx in backup_data:
                        tx_short = f"{tx[1]} - {tx[2]} to {tx[3]}: {tx[4]} ({tx[11]})"
                        if tx[9] == 0:
                            try:
                                nb_tx += 1
                                node.logger.app_log.info(
                                    mp.MEMPOOL.merge((tx[1], tx[2], tx[3], tx[4], tx[5], tx[6], tx[10], tx[11]),
                                                     peer_ip, db_handler.c, False, revert=True))  # will get stuck if you change it to respect node.db_lock
                                node.logger.app_log.warning(f"Moved tx back to mempool: {tx_short}")
                            except Exception as e:
                                node.logger.app_log.warning(f"Error during moving tx back to mempool: {e}")
                        else:
                            # It's the coinbase tx, so we get the miner address
                            miner = tx[3]
                            height = tx[0]
                    rollback = {"timestamp": my_time, "height": height, "ip": peer_ip, "miner": miner,
                                "hash": db_block_hash, "tx_count": nb_tx, "skipped": False, "reason": ""}
                    node.plugin_manager.execute_action_hook('rollback', rollback)

                except Exception as e:
                    node.logger.app_log.warning(f"Error during moving txs back to mempool: {e}")

    else:
        reason = "Skipping rollback, other ledger operation in progress"
        rollback = {"timestamp": my_time, "ip": peer_ip, "skipped": True, "reason": reason}
        node.plugin_manager.execute_action_hook('rollback', rollback)
        node.logger.app_log.info(reason)


def seed_genesis(node):
    """doc/30 sync_from_genesis: create a fresh GENESIS-ONLY ledger (block 1) instead of
    downloading a snapshot, so the node syncs the chain from block 1. The genesis block is
    the canonical one shared by mainnet/regnet (height 1, block_hash 7a0f3848…, the
    4edadac… genesis address) — reused from regnet.SQL_LEDGER so the long key/sig literal is
    not duplicated. Catch-up then pulls block 2..tip from peers; everything below the
    assume_valid_height horizon is accepted on the trusted prefix and anchored by the
    checkpoints. Never clobbers a ledger that already has blocks."""
    import amounts
    import regnet
    ledger_sql = regnet.SQL_LEDGER_INTEGER if amounts.LEDGER_INTEGER else regnet.SQL_LEDGER

    def _has_blocks(path):
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            return False
        try:
            con = sqlite3.connect(path); cur = con.cursor()
            cur.execute("SELECT MAX(block_height) FROM transactions")
            mx = cur.fetchone()[0]; con.close()
            return bool(mx) and int(mx) >= 1
        except Exception:
            return False

    # the node reads from hyper_path and (cloned) ledger_path; seed both with genesis
    for path in dict.fromkeys([p for p in (node.hyper_path, node.ledger_path) if p]):
        if _has_blocks(path):
            node.logger.app_log.warning(f"sync_from_genesis: {path} already populated — not reseeding")
            continue
        d = os.path.dirname(path) or "."
        if d and not os.path.exists(d):
            os.makedirs(d, exist_ok=True)
        for ext in ("", "-wal", "-shm"):
            try: os.remove(path + ext)
            except OSError: pass
        with sqlite3.connect(path) as con:
            for stmt in ledger_sql:
                con.execute(stmt)
            con.commit()
        node.logger.app_log.warning(f"sync_from_genesis: seeded canonical genesis (height 1) into {path}")

    # the alias/token index db
    idx = getattr(node, "index_db", None)
    if idx and not (os.path.exists(idx) and os.path.getsize(idx) > 0):
        d = os.path.dirname(idx) or "."
        if d and not os.path.exists(d):
            os.makedirs(d, exist_ok=True)
        with sqlite3.connect(idx) as con:
            for stmt in regnet.SQL_INDEX:
                con.execute(stmt)
            con.commit()
    node.logger.app_log.warning("sync_from_genesis: genesis seeded; will sync the chain from block 2")


def bootstrap(node):
    # TODO: Candidate for single user mode
    # doc/30: when syncing from genesis, never download a snapshot — seed the genesis block and let
    # catch-up build the chain from block 1. Single choke point for every bootstrap call site.
    if getattr(node, "sync_from_genesis", False):
        node.logger.app_log.warning("Status: sync_from_genesis set — seeding genesis instead of bootstrapping")
        seed_genesis(node)
        return
    try:
        # Extract into the ledger's own directory (the default mainnet ledger lives in static/).
        dest_dir = os.path.dirname(node.ledger_path) or "."
        for pattern in ('*.db-wal', '*.db-shm'):
            for f in glob.glob(os.path.join(dest_dir, pattern)):
                os.remove(f)
                print(f, "deleted")

        archive_path = node.ledger_path + ".tar.gz"

        # Source resolution, local first: an operator can drop the ledger archive in place (handy when
        # the download host is unreachable, as the historical fixed host became). Only download when no
        # local archive is available, and from a CONFIGURABLE url rather than a single hardcoded host.
        local_archive = getattr(node, "bootstrap_file", "") or ""
        if local_archive and os.path.exists(local_archive):
            node.logger.app_log.warning(f"Status: Bootstrapping from local archive {local_archive}")
            archive_path = local_archive
        elif os.path.exists(archive_path):
            node.logger.app_log.warning(f"Status: Bootstrapping from existing archive {archive_path}")
        else:
            _fetched = False
            if getattr(node, "bootstrap_p2p", False):   # doc/43: try peers (sha256-verified) before the central host
                try:
                    import snapshot_p2p
                    _cands = snapshot_p2p.candidates(node)
                    _got = snapshot_p2p.fetch_from_peers(node, _cands, archive_path) if _cands else None
                    if _got:
                        node.logger.app_log.warning(
                            f"Status: P2P snapshot verified (height {_got[1]['height']}, "
                            f"sha256 {_got[1]['sha256'][:12]}) — bootstrapping from peers")
                        _fetched = True
                    else:
                        node.logger.app_log.warning("Status: P2P snapshot unavailable; falling back to bootstrap_url")
                except Exception as _e:
                    node.logger.app_log.warning(f"Status: P2P snapshot attempt failed ({_e}); falling back to bootstrap_url")
            if not _fetched:
                url = getattr(node, "bootstrap_url", "") or "https://bismuth.cz/ledger.tar.gz"
                node.logger.app_log.warning(f"Status: No local bootstrap archive; downloading ledger from {url}")
                download_file(url, archive_path)

        with tarfile.open(archive_path) as tar:

            def is_within_directory(directory, target):
                abs_directory = os.path.abspath(directory)
                abs_target = os.path.abspath(target)
                prefix = os.path.commonprefix([abs_directory, abs_target])
                return prefix == abs_directory

            def safe_extract(tar, path=".", members=None, *, numeric_owner=False):
                for member in tar.getmembers():
                    member_path = os.path.join(path, member.name)
                    if not is_within_directory(path, member_path):
                        raise Exception("Attempted Path Traversal in Tar File")
                tar.extractall(path, members, numeric_owner=numeric_owner)

            safe_extract(tar, dest_dir)
        node.logger.app_log.warning(f"Status: Bootstrap ledger extracted into {dest_dir}/")

    except Exception as e:
        node.logger.app_log.warning(
            f"Status: Bootstrapping failed ({e}). Provide a local archive via the 'bootstrap_file' "
            f"config option (or drop it at {node.ledger_path}.tar.gz), or set a reachable 'bootstrap_url'.")
        raise


def check_integrity(node, database):
    # TODO: Candidate for single user mode
    # check ledger integrity

    if not os.path.exists("static"):
        os.mkdir("static")

    with sqlite3.connect(database) as ledger_check:
        if node.trace_db_calls:
            ledger_check.set_trace_callback(functools.partial(sql_trace_callback,node.logger.app_log,"CHECK_INTEGRITY"))

        ledger_check.text_factory = str
        l = ledger_check.cursor()

        try:
            l.execute("PRAGMA table_info('transactions')")
            redownload = False
        except:
            redownload = True

        if len(l.fetchall()) != 12:
            node.logger.app_log.warning(
                f"Status: Integrity check on database {database} failed, bootstrapping from the website")
            redownload = True

    if redownload and node.is_mainnet:
        bootstrap(node)


def sequencing_check(node, db_handler):
    # TODO: Candidate for single user mode
    try:
        with open("sequencing_last", 'r') as filename:
            sequencing_last = int(filename.read())

    except:
        node.logger.app_log.warning("Sequencing anchor not found, going through the whole chain")
        sequencing_last = 0

    node.logger.app_log.warning(f"Status: Testing chain sequencing, starting with block {sequencing_last}")

    chains_to_check = [node.ledger_path, node.hyper_path]

    for chain in chains_to_check:
        conn = sqlite3.connect(chain)
        if node.trace_db_calls:
            conn.set_trace_callback(functools.partial(sql_trace_callback,node.logger.app_log,"SEQUENCE-CHECK-CHAIN"))
        c = conn.cursor()

        # try/finally so a SQL error or a failing index rollback in the loop below can't leak this
        # per-chain connection (a leaked handle keeps the ledger file locked, notably on Windows).
        try:
            # perform test on transaction table
            y = None
            # Egg: not sure block_height != (0 OR 1)  gives the proper result, 0 or 1  = 1. not in (0, 1) could be better.
            for row in c.execute(
                    "SELECT block_height FROM transactions WHERE reward != 0 AND block_height > 1 AND block_height >= ? ORDER BY block_height ASC",
                    (sequencing_last,)):
                y_init = row[0]

                if y is None:
                    y = y_init

                if row[0] != y:

                    for chain2 in chains_to_check:
                        conn2 = sqlite3.connect(chain2)
                        if node.trace_db_calls:
                            conn2.set_trace_callback(functools.partial(sql_trace_callback,node.logger.app_log,"SEQUENCE-CHECK-CHAIN2"))
                        c2 = conn2.cursor()
                        node.logger.app_log.warning(f"Status: Chain {chain} transaction sequencing error at: {row[0]}. {row[0]} instead of {y}")
                        c2.execute("DELETE FROM transactions WHERE block_height >= ? OR block_height <= ?", (row[0], -row[0],))
                        conn2.commit()
                        c2.execute("DELETE FROM misc WHERE block_height >= ?", (row[0],))
                        conn2.commit()

                        # rollback indices to the SAME boundary as the ledger DELETE above (row[0], the ACTUAL
                        # break) and _rebuild_derived_state below — NOT y (the EXPECTED height). On a
                        # duplicate-height break y is one past row[0], so rolling these height-keyed side
                        # indexes to y stranded height-row[0] entries ABOVE the ledger cut, which then
                        # double-count tokens/aliases when the resync re-digests row[0] (token inflation wedge).
                        _cut = row[0]
                        db_handler.tokens_rollback(node, _cut)
                        db_handler.aliases_rollback(node, _cut)
                        # the ledger DELETE above cuts at row[0]; rebuild the derived projections to the
                        # SAME boundary (they re-execute from the ledger).
                        _rebuild_derived_state(node, db_handler, _cut - 1)
                        # rollback indices

                        node.logger.app_log.warning(f"Status: Due to a sequencing issue at block {y}, {chain} has been rolled back and will be resynchronized")
                        conn2.close()  # don't leak this connection (the misc-error branch below already closes its conn2)
                    break

                y = y + 1

            # perform test on misc table
            y = None

            # Bound the difficulty (misc) scan to the same recent tail the transactions scan above already
            # uses (>= sequencing_last anchor) instead of re-reading every misc row from 300000 to the tip
            # on EVERY boot — the dominant cost of the startup sequencing scan on a large mainnet ledger.
            # Deep history below the anchor is immutable and was validated on the boot that advanced the
            # anchor; any NEW gap/dup lives at/after it (and the live autoheal loop guards the tip). When no
            # anchor file exists (sequencing_last == 0) this is max(300000, 0) == 300000 — unchanged, so a
            # first/unanchored boot still does the original full difficulty scan (no regression).
            misc_floor = max(300000, sequencing_last)

            for row in c.execute("SELECT block_height FROM misc WHERE block_height > ? ORDER BY block_height ASC",
                                 (misc_floor,)):
                y_init = row[0]

                if y is None:
                    y = y_init
                    # print("assigned")
                    # print(row[0], y)

                if row[0] != y:
                    # print(row[0], y)
                    for chain2 in chains_to_check:
                        conn2 = sqlite3.connect(chain2)
                        if node.trace_db_calls:
                            conn2.set_trace_callback(functools.partial(sql_trace_callback,node.logger.app_log,"SEQUENCE-CHECK-CHAIN2B"))
                        c2 = conn2.cursor()
                        node.logger.app_log.warning(
                            f"Status: Chain {chain} difficulty sequencing error at: {row[0]}. {row[0]} instead of {y}")
                        # Delete positive rows AND every negative-height mirror row generically (block_height
                        # <= -row[0]) — matching the transactions branch above. The old code enumerated only
                        # "Development Reward" and "Hypernode Payouts" negative mirrors, so the generic
                        # negative mirrors for any other sink address survived above
                        # the cut as orphans and were re-credited by the balance-index rebuild below —
                        # balance-index inflation. One generic delete covers all mirror addresses.
                        c2.execute("DELETE FROM transactions WHERE block_height >= ? OR block_height <= ?",
                                   (row[0], -row[0],))
                        conn2.commit()
                        c2.execute("DELETE FROM misc WHERE block_height >= ?", (row[0],))
                        conn2.commit()
                        conn2.close()

                        # rollback indices to the ACTUAL break (row[0]), matching the ledger DELETE and
                        # _rebuild_derived_state — NOT y (the expected height); see the transactions branch.
                        _cut = row[0]
                        db_handler.tokens_rollback(node, _cut)
                        db_handler.aliases_rollback(node, _cut)
                        _rebuild_derived_state(node, db_handler, _cut - 1)   # VM/root to the ledger cut
                        # rollback indices

                        node.logger.app_log.warning(f"Status: Due to a sequencing issue at block {y}, {chain} has been rolled back and will be resynchronized")
                    break

                y = y + 1

            node.logger.app_log.warning(f"Status: Chain sequencing test complete for {chain}")
        finally:
            conn.close()

        if y:
            with open("sequencing_last", 'w') as filename:
                filename.write(str(y - 1000))  # room for rollbacks


def detect_recent_sequence_break(db_handler, tip, window=2000):
    """READ-ONLY: scan the most recent `window` coinbase heights for a gap or duplicate — the corruption a
    crash / block-churn leaves at the TIP. Returns the height to cut back to (first divergence from a
    strictly-consecutive run), or None if the recent tail is clean. Cheap (one indexed range, ~`window`
    rows); deep-history breaks remain the startup sequencing_check's full-scan job."""
    tip = int(tip or 0)
    if tip < 3:
        return None
    lo = max(2, tip - int(window))
    rows = db_handler.fetchall(db_handler.h,
                               "SELECT block_height FROM transactions WHERE reward != 0 "
                               "AND block_height >= ? AND block_height <= ? ORDER BY block_height ASC",
                               (lo, tip))
    expect = None
    for (h,) in (rows or []):
        h = int(h)
        if expect is None:
            expect = h
        if h != expect:
            return min(h, expect)   # dupe -> the repeated height; gap -> the missing height; both = the cut
        expect += 1
    return None


def autoheal_live(node, db_handler, window=2000):
    """Self-heal a sequence/dupe corruption WITHOUT a restart. Detect a break in the recent tail (read-only);
    if found, roll the chain back to the clean prefix using the SAME rollback() blocknf uses (truncate
    ledger+hyper + rebuild every derived projection: balance/txid index, VM state+root, tokens/aliases),
    reset the node's in-memory tip from disk, and let the normal sync loop re-fetch the
    discarded blocks. No-op (just the cheap recent scan) on a healthy chain. MUST be called holding
    node.db_lock (serialised with digestion). ram=False assumed (disk is the tip source). Returns True iff
    it repaired."""
    tip = int(getattr(node, "last_block", 0) or 0)
    brk = detect_recent_sequence_break(db_handler, tip, window)
    if brk is None:
        return False
    node.logger.app_log.warning(
        f"Status: AUTOHEAL — sequence/dupe break detected at height {brk} (tip {tip}); rolling back live")
    rollback(node, db_handler, brk)
    # reset the in-memory tip from the truncated DB (node_block_init pattern)
    node.hdd_block = db_handler.block_height_max()
    node.last_block = node.hdd_block
    node.last_block_hash = db_handler.last_block_hash()
    node.hdd_hash = node.last_block_hash
    node.last_block_timestamp = db_handler.last_block_timestamp()
    try:
        from difficulty import difficulty as _difficulty
        node.difficulty = _difficulty(node, db_handler)
    except Exception:
        pass
    essentials.checkpoint_set(node)
    node.logger.app_log.warning(
        f"Status: AUTOHEAL — rolled back to {node.last_block}; the sync loop will re-fetch the clean chain")
    return True


# ============================================================================================
# doc/35 — peer-difficulty divergence detector + guarded self-heal (#23, Stage 2a)
#
# The legacy controller (difficulty.py:70-94) is recursive on the STORED previous difficulty, so a
# wrong cached value at/below the tip is a self-reinforcing fixed point that "recompute-and-assert"
# can't catch (it reuses the same corrupt misc row). The only independent reference is a PEER that
# derived difficulty from a clean misc history. We therefore detect by peer-quorum comparison and
# (opt-in) heal by rolling back below the corruption + resyncing so difficulty() re-derives clean.
#
# OBSERVE-ONLY: reads the cached node.difficulty + polls peers over HTTP. NEVER scans the ledger
# (cf. [[no-heavy-scans-on-prod-ledger]]). SAFE BY DEFAULT: the loop in node.py only logs unless the
# operator opts into pause/heal.
# ============================================================================================

# Detector thresholds (doc/35 §Detector). Conservative on purpose: thin data -> abstain, never heal.
DIFF_DIVERGENCE_MIN_PEERS = 3          # min height-matched peer samples before we form any opinion
DIFF_DIVERGENCE_MAX_POLL = 8           # cap on peers polled per cycle (bounded HTTP fan-out)
DIFF_DIVERGENCE_ABS_THRESHOLD = 0.5    # diff-units: |local - median| floor that counts as diverged
DIFF_DIVERGENCE_REL_THRESHOLD = 0.02   # 2% relative: whichever (abs/rel) is LARGER is the threshold
DIFF_DIVERGENCE_HEIGHT_TOL = 1         # peer block_height must be within ±this of node.last_block
DIFF_DIVERGENCE_AGREE_FRAC = 0.75      # >=75% of samples must agree among themselves (within thr/2)
DIFF_DIVERGENCE_FORK_WINDOW = 1440     # skip ±this many blocks around node.fork_height (LWMA transition)

# Heal guards (doc/35 §Guarded self-heal). Per-ledger sidecar, NOT in the db.
DIFF_HEAL_COOLDOWN_S = 3600            # >=1h between heals (one full resync+stabilize window)
DIFF_HEAL_MAX_PER_24H = 2             # secondary rate limit within any rolling 24h window
DIFF_HEAL_LIFETIME_MAX = 2            # PERMANENT (per-ledger) cap on the monotonic heal_count — the hard stop
                                      # that makes a SURVIVING corruption go advisory-only FOREVER (the rolling
                                      # 24h window alone re-opens daily and would restart-loop prod) [review #1].
                                      # Reset to 0 only by a confirmed-CLEAN post-heal reading (diffheal_note_clean).
DIFF_HEAL_DEPTH_CAP_MULT = 3          # successive heals may go deeper, capped at 3 * rollback_depth


def _diff_effective_threshold(local, base_threshold):
    """The larger of the absolute floor and a 2%-relative band around the local value — so the same
    detector is meaningful both at low difficulties (abs dominates) and high ones (rel dominates)."""
    try:
        rel = abs(float(local)) * DIFF_DIVERGENCE_REL_THRESHOLD
    except (TypeError, ValueError):
        rel = 0.0
    return max(float(base_threshold), rel)


def _resolve_rest_port(node, host, sock_port, cache):
    """Resolve a socket peer's REST port via /api/capabilities (cached ip->rest_port). connection_pool holds
    SOCKET ip:port, and the socket port does NOT speak HTTP — so to REACH /api/capabilities we must probe the
    peer's REST port, which we don't yet know. Probe a small candidate set derived from the standard Bismuth
    deployment convention REST = socket+1 (e.g. mainnet 5658->5659; the regnet harness 4060->4061), then the
    socket port itself (co-hosted REST), then our OWN rest port (homogeneous clusters). The FIRST candidate
    that answers yields the AUTHORITATIVE rest_port from its body (used even if it differs from the candidate
    we reached it on). Returns the int REST port, or None if no candidate is REST-capable ("if the API is
    inaccessible it doesn't exist"). Only SUCCESSFUL resolutions are cached [review #2] — a failure is a cache
    miss that retries. (The socket handshake does NOT advertise the REST port — left untouched per the
    legacy-networking-being-replaced direction — hence this convention probe. Found by the doc/35 regnet soak:
    probing only the socket port resolved zero peers, so the detector would perpetually abstain on a real net.)"""
    import rest_client
    if host in cache:
        return cache[host]
    rest_port = None
    candidates = []
    for c in (sock_port + 1, sock_port, getattr(node, "rest_api_port", None)):
        if c and int(c) not in candidates:
            candidates.append(int(c))
    for candidate in candidates:
        try:
            caps = rest_client.get_capabilities(host, candidate)
        except Exception:
            caps = None
        if caps and caps.get("rest_api") and caps.get("rest_port"):
            rest_port = int(caps["rest_port"])   # authoritative, from the body
            break
    # Only memoize a SUCCESSFUL resolution. A transient /api/capabilities failure (peer restart, brief network
    # blip) must be a cache MISS that retries next cycle — never a permanent "non-REST" verdict that erodes the
    # height-matched sample set toward perpetual abstain over a long-running process. [review #2]
    if rest_port:
        cache[host] = rest_port
    return rest_port


def detect_difficulty_divergence(node, db, peers, threshold=DIFF_DIVERGENCE_ABS_THRESHOLD,
                                 rest_port_cache=None):
    """OBSERVE-ONLY peer-quorum difficulty check. Returns ``(diverged, local, median, sample_count)``.

    - ``local`` is the CACHED ``node.difficulty[0]`` — NO ledger scan, no recompute (the corrupt misc
      row would just reproduce the corrupt value).
    - Resolves each connected socket peer's REST port via /api/capabilities (cached), polls up to
      ``DIFF_DIVERGENCE_MAX_POLL`` peers' ``GET /api/difficulty`` (fail-soft), keeps only HEIGHT-MATCHED
      samples (peer ``block_height`` within ±``DIFF_DIVERGENCE_HEIGHT_TOL`` of ``node.last_block``).
    - ABSTAINS (``diverged=False``) on thin data: < ``DIFF_DIVERGENCE_MIN_PEERS`` samples, or local
      unknown. Never heal on thin data.
    - DIVERGED iff ``abs(local - median) > effective_threshold`` AND >= ``DIFF_DIVERGENCE_AGREE_FRAC``
      of samples agree among themselves within ``effective_threshold/2`` (peers consistent => it's us).

    ``rest_port_cache`` (a dict) persists ip->rest_port across cycles; the loop owns it. The detector is
    pure I/O + arithmetic — unit-testable by feeding a fake ``peers``/monkeypatched ``rest_client``."""
    import rest_client
    if rest_port_cache is None:
        rest_port_cache = {}

    d = getattr(node, "difficulty", None)
    if not d:
        return (False, None, None, 0)
    try:
        local = float(d[0])
    except (TypeError, ValueError, IndexError):
        return (False, None, None, 0)

    tip = int(getattr(node, "last_block", 0) or 0)

    # connection_pool entries are "ip:sock_port" strings (socket peers we are connected to).
    pool = list(getattr(peers, "connection_pool", []) or [])
    samples = []
    polled = 0
    for entry in pool:
        if polled >= DIFF_DIVERGENCE_MAX_POLL:
            break
        try:
            host, sock_port = entry.rsplit(":", 1)
            sock_port = int(sock_port)
        except (ValueError, AttributeError):
            continue
        rest_port = _resolve_rest_port(node, host, sock_port, rest_port_cache)
        if not rest_port:
            continue
        polled += 1
        info = rest_client.get_difficulty(host, rest_port)
        if not info:
            continue
        pdiff = info.get("difficulty")
        pheight = info.get("block_height")
        if pdiff is None or pheight is None:
            continue
        try:
            pdiff = float(pdiff)
            pheight = int(pheight)
        except (TypeError, ValueError):
            continue
        if abs(pheight - tip) > DIFF_DIVERGENCE_HEIGHT_TOL:
            continue                       # height-mismatched: a peer on a different tip is not comparable
        samples.append(pdiff)

    n = len(samples)
    if n < DIFF_DIVERGENCE_MIN_PEERS:
        return (False, local, None, n)     # ABSTAIN on thin data

    s = sorted(samples)
    mid = n // 2
    median = s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0

    eff = _diff_effective_threshold(local, threshold)
    # do the peers agree among THEMSELVES? (a fork/liar minority shouldn't trigger a heal)
    agree = sum(1 for x in samples if abs(x - median) <= eff / 2.0)
    peers_consistent = (agree / float(n)) >= DIFF_DIVERGENCE_AGREE_FRAC

    diverged = (abs(local - median) > eff) and peers_consistent
    return (diverged, local, median, n)


# -------------------- per-ledger heal sidecar + guards (doc/35 §Guarded self-heal) --------------------

def _diffheal_sidecar_path(node):
    """``<ledger>.diffheal.json`` — per-ledger (regnet/mainnet never share heal state), NOT in the db."""
    lp = getattr(node, "ledger_path", None) or "ledger.db"
    return lp + ".diffheal.json"


def diffheal_state_read(node):
    """Read the heal ledger sidecar: ``{last_heal_ts, heal_count, heals_24h:[ts,...], last_target}``.
    Tolerant: missing/corrupt -> a fresh empty state (never blocks detection)."""
    import json
    try:
        with open(_diffheal_sidecar_path(node), "r") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("bad sidecar")
    except Exception:
        data = {}
    data.setdefault("last_heal_ts", 0)
    data.setdefault("heal_count", 0)
    data.setdefault("heals_24h", [])
    data.setdefault("last_target", None)
    return data


def diffheal_state_write(node, state):
    """Atomically persist the heal sidecar (temp file + replace). Best-effort."""
    import json
    path = _diffheal_sidecar_path(node)
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(state, fh)
        os.replace(tmp, path)
    except Exception as e:
        try:
            node.logger.app_log.warning(f"diffheal: could not persist sidecar: {e}")
        except Exception:
            pass


def diffheal_guards_ok(node, state, now=None):
    """Return ``(ok, reason)`` — may we ARM a heal right now under the loop guards?

    - Once-per-boot: the in-memory ``node._diffheal_armed`` blocks a second arm before the restart.
    - Cooldown: >= ``DIFF_HEAL_COOLDOWN_S`` since ``last_heal_ts``.
    - Max-heals: < ``DIFF_HEAL_MAX_PER_24H`` heals in the rolling 24h window -> else advisory-only.
    Bounded depth is enforced at target computation (``diffheal_target``), not here."""
    if now is None:
        now = time.time()
    if getattr(node, "_diffheal_armed", False):
        return (False, "already armed this boot (awaiting restart)")
    last_ts = float(state.get("last_heal_ts") or 0)
    if last_ts and (now - last_ts) < DIFF_HEAL_COOLDOWN_S:
        return (False, f"cooldown ({int(DIFF_HEAL_COOLDOWN_S - (now - last_ts))}s left)")
    # PERMANENT lifetime cap on the MONOTONIC heal_count. The rolling-24h window below is only a secondary
    # rate limit — on its own it re-opens every 24h, so a corruption that SURVIVES every resync would restart-
    # loop the prod node forever (2 restarts/day). The lifetime cap is the hard stop: once reached the node
    # stays advisory-only and NEVER restarts again, until a confirmed-CLEAN reading restores the budget
    # (diffheal_note_clean). [review #1 CRITICAL — no restart-loop]
    if int(state.get("heal_count") or 0) >= DIFF_HEAL_LIFETIME_MAX:
        return (False, f"lifetime max heals ({DIFF_HEAL_LIFETIME_MAX}) reached — advisory-only, "
                       f"manual intervention required")
    window = [t for t in (state.get("heals_24h") or []) if (now - float(t)) < 86400]
    if len(window) >= DIFF_HEAL_MAX_PER_24H:
        return (False, f"max heals ({DIFF_HEAL_MAX_PER_24H}) reached in 24h — manual intervention required")
    return (True, "ok")


def diffheal_note_clean(node):
    """Called on a CONFIRMED-CLEAN reading (>= min_peers, peers agree AND we match them). Resets the monotonic
    heal_count + the rolling window so the depth-deepening ladder and the lifetime cap apply only WITHIN a
    single UNRESOLVED corruption episode: a corruption that a heal genuinely fixed restores the heal budget for
    a future unrelated incident, while one that SURVIVES every resync never reads clean, never resets, and
    stays advisory-only forever after the lifetime cap. [review #1/#3] Best-effort; only writes when there is
    something to reset (so a healthy node never churns the sidecar)."""
    try:
        state = diffheal_state_read(node)
        if int(state.get("heal_count") or 0) == 0 and not (state.get("heals_24h") or []):
            return
        state["heal_count"] = 0
        state["heals_24h"] = []
        diffheal_state_write(node, state)
        node.logger.app_log.info("diffheal: confirmed-clean peer-quorum reading — heal budget reset")
    except Exception:
        pass


def diffheal_target(node, state):
    """Compute the clamped rollback target for a heal, or ``None`` if not permitted by depth/anti-sybil.

    target = ``max(last_block - depth, checkpoint)``, where ``depth`` deepens by one ``rollback_depth``
    per successive heal (so a corruption that survives one resync gets a deeper cut next time), capped at
    ``DIFF_HEAL_DEPTH_CAP_MULT * rollback_depth``. Never below ``node.checkpoint``; gated by
    ``essentials.rollback_allowed`` (supermajority + reputable, anti-sybil). Returns an int height."""
    tip = int(getattr(node, "last_block", 0) or 0)
    base_depth = int(getattr(node, "rollback_depth", 30) or 30)
    heal_count = int(state.get("heal_count") or 0)
    depth = min(base_depth * (heal_count + 1), base_depth * DIFF_HEAL_DEPTH_CAP_MULT)
    checkpoint = int(getattr(node, "checkpoint", 0) or 0)
    target = max(tip - depth, checkpoint)
    if target >= tip:                       # nothing to roll back to (e.g. tip <= checkpoint)
        return None
    if not essentials.rollback_allowed(node, target):
        node.logger.app_log.warning(
            f"diffheal: rollback to {target} NOT allowed (anti-sybil/consensus gate) — staying advisory")
        return None
    return target


def diffheal_arm(node, target):
    """ARM a one-shot heal: write the proven ``rollback_to`` trigger file under ``node.db_lock`` (the SAME
    one-shot path node.py:1512 consumes once at the safe startup point that rebuilds derived state), record
    the heal in the per-ledger sidecar, set the once-per-boot flag, and return True. The caller requests a
    clean restart (systemd auto-restarts). We do NOT do an in-place deep rollback from the thread — only the
    startup sequence truncates, exactly as the recovery design requires."""
    now = time.time()
    locked = node.db_lock.acquire(blocking=True, timeout=30)
    if not locked:
        node.logger.app_log.warning("diffheal: could not acquire db_lock to arm heal; will retry next cycle")
        return False
    try:
        with open("rollback_to", "w") as fh:
            fh.write(str(int(target)))
        state = diffheal_state_read(node)
        state["last_heal_ts"] = now
        state["heal_count"] = int(state.get("heal_count") or 0) + 1
        window = [t for t in (state.get("heals_24h") or []) if (now - float(t)) < 86400]
        window.append(now)
        state["heals_24h"] = window
        state["last_target"] = int(target)
        diffheal_state_write(node, state)
        node._diffheal_armed = True
        node.logger.app_log.warning(
            f"Status: DIFFICULTY-HEAL ARMED — rollback_to={int(target)} written; requesting clean restart "
            f"(heal #{state['heal_count']}). On boot the chain rolls back below the corruption and resyncs.")
        return True
    finally:
        node.db_lock.release()
