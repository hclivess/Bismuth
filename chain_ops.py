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
    on EVERY rollback path (rollback / blocknf / sequencing_check). Skipping the VM rebuild is a real
    rollback-attack vector: a stale ``node.vm_state_root`` makes the mandatory state-root check (digest.py)
    REJECT the canonical branch — a consensus wedge — AND leaves contract custody balances ahead of their
    VM_SINK ledger backing, i.e. inflation / double-spend of VM custody across the reorg. The height-keyed
    DELETE stores (ledger / tokens / aliases / shielded) are rolled back at each call site with the site's
    own boundary; this rebuilds the RE-EXECUTABLE projections. All guarded + logged; disabled stores skip."""
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
    # the VM contract state is a re-executable projection of the chain's vm: txs — rebuild it from the
    # rolled-back ledger and recompute the committed state root (deterministic, post-fork only).
    if getattr(node, "vm_state", None) is not None and getattr(node, "fork_height", None) is not None:
        try:
            import vm_engine
            vm_engine.rebuild(node.vm_state, db_handler.h, node.fork_height, keep_height)
            node.vm_state_root = node.vm_state.state_root()
        except Exception as e:
            node.logger.app_log.warning(f"vm state rollback rebuild failed: {e}")


def rollback(node, db_handler, block_height):
    node.logger.app_log.warning(f"Status: Rolling back below: {block_height}")

    db_handler.rollback_under(block_height)

    # rollback indices
    db_handler.tokens_rollback(node, block_height)
    db_handler.aliases_rollback(node, block_height)
    if getattr(node, "shielded_state", None) is not None:   # shielded note/nullifier set (doc/22)
        node.shielded_state.rollback_under(block_height)
    # rollback indices

    # rollback_under(block_height) drops heights >= block_height, so the derived stores keep <= height-1
    _rebuild_derived_state(node, db_handler, block_height - 1)

    node.logger.app_log.warning(f"Status: Chain rolled back below {block_height} and will be resynchronized")


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

        shutil.copy(node.ledger_path, node.ledger_path + '.temp')
        hyper = sqlite3.connect(node.ledger_path + '.temp')
    else:
        shutil.copy(node.hyper_path, node.ledger_path + '.temp')
        hyper = sqlite3.connect(node.ledger_path + '.temp')
    if node.trace_db_calls:
       hyper.set_trace_callback(functools.partial(sql_trace_callback,node.logger.app_log,"HYPER"))
    hyper.text_factory = str
    hyp = hyper.cursor()

    hyp.execute("UPDATE transactions SET address = 'Hypoblock' WHERE address = 'Hyperblock'")

    hyp.execute("SELECT max(block_height) FROM transactions")
    db_block_height = int(hyp.fetchone()[0])
    depth_specific = db_block_height - depth

    hyp.execute(
        "SELECT distinct(recipient) FROM transactions WHERE (block_height < ? AND block_height > ?) ORDER BY block_height;",
        (depth_specific, -depth_specific,))  # new addresses will be ignored until depth passed
    unique_addressess = hyp.fetchall()

    for x in set(unique_addressess):
        credit = Decimal("0")
        for entry in hyp.execute(
                "SELECT amount,reward FROM transactions WHERE recipient = ? AND (block_height < ? AND block_height > ?);",
                (x[0],) + (depth_specific, -depth_specific,)):
            try:
                credit = quantize_eight(credit) + quantize_eight(entry[0]) + quantize_eight(entry[1])
                credit = 0 if credit is None else credit
            except Exception:
                credit = 0

        debit = Decimal("0")
        for entry in hyp.execute(
                "SELECT amount,fee FROM transactions WHERE address = ? AND (block_height < ? AND block_height > ?);",
                (x[0],) + (depth_specific, -depth_specific,)):
            try:
                debit = quantize_eight(debit) + quantize_eight(entry[0]) + quantize_eight(entry[1])
                debit = 0 if debit is None else debit
            except Exception:
                debit = 0

        end_balance = quantize_eight(credit - debit)

        if end_balance > 0:
            timestamp = str(time.time())
            hyp.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
                depth_specific - 1, timestamp, "Hyperblock", x[0], str(end_balance), "0", "0", "0", "0",
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
    hyper.close()

    if os.path.exists(node.hyper_path):
        os.remove(node.hyper_path)  # remove the old hyperblocks to rebuild
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


def blocknf(node, block_hash_delete, peer_ip, db_handler, hyperblocks=False):
    """
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
                if getattr(node, "shielded_state", None) is not None:   # shielded set (doc/22)
                    node.shielded_state.rollback_under(db_block_height)
                # rebuild the re-executable projections (VM state + root, balance index, aux stores) — this
                # is the LIVE reorg path; without the VM rebuild a stale vm_state_root wedges consensus and
                # custody balances outrun their ledger backing (inflation across the reorg).
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


def bootstrap(node):
    # TODO: Candidate for single user mode
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

                        # rollback indices
                        db_handler.tokens_rollback(node, y)
                        db_handler.aliases_rollback(node, y)
                        if getattr(node, "shielded_state", None) is not None:   # shielded set (doc/22)
                            node.shielded_state.rollback_under(y)
                        # the ledger DELETE above cuts at row[0]; rebuild the VM state + root to the SAME
                        # boundary (it re-executes from the ledger), else the startup vm_state OPEN reads a
                        # stale root and the state-root check wedges the resync.
                        _rebuild_derived_state(node, db_handler, row[0] - 1)
                        # rollback indices

                        node.logger.app_log.warning(f"Status: Due to a sequencing issue at block {y}, {chain} has been rolled back and will be resynchronized")
                        conn2.close()  # don't leak this connection (the misc-error branch below already closes its conn2)
                    break

                y = y + 1

            # perform test on misc table
            y = None

            for row in c.execute("SELECT block_height FROM misc WHERE block_height > ? ORDER BY block_height ASC",
                                 (300000,)):
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
                        c2.execute("DELETE FROM transactions WHERE block_height >= ?", (row[0],))
                        conn2.commit()
                        c2.execute("DELETE FROM misc WHERE block_height >= ?", (row[0],))
                        conn2.commit()

                        db_handler.execute_param(conn2, (
                            'DELETE FROM transactions WHERE address = "Development Reward" AND block_height <= ?'),
                                                 (-row[0],))
                        conn2.commit()

                        db_handler.execute_param(conn2, (
                            'DELETE FROM transactions WHERE address = "Hypernode Payouts" AND block_height <= ?'),
                                                 (-row[0],))
                        conn2.commit()
                        conn2.close()

                        # rollback indices
                        db_handler.tokens_rollback(node, y)
                        db_handler.aliases_rollback(node, y)
                        if getattr(node, "shielded_state", None) is not None:   # shielded set (doc/22)
                            node.shielded_state.rollback_under(y)
                        _rebuild_derived_state(node, db_handler, row[0] - 1)   # VM/root to the ledger cut
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
