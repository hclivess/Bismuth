"""
Chain-maintenance operations: rollback, the ``blocknf`` ("block not found") fork resolution, genesis
seeding and snapshot bootstrap of a fresh ledger, and the peer-difficulty divergence detector + guarded
self-heal. They take ``node``/``db_handler`` explicitly. ``node.py`` re-exports ``blocknf`` so
``worker.py``'s ``from node import blocknf`` keeps working.

The ledger is ONE LMDB environment (``block_store``): a rollback is one atomic transaction that drops the
blocks and every index entry they created, so there is nothing to reconcile after a crash.
"""
import os
import shutil
import threading
import time

import essentials
import mempool as mp


def _rollback_plugin_stores(node, db_handler, block_height):
    """Roll the plugin-owned stores (token/alias index) back with the ledger: drop heights >= block_height."""
    db_handler.tokens_rollback(node, block_height)
    db_handler.aliases_rollback(node, block_height)


def rollback(node, db_handler, block_height):
    """Drop every block at or above ``block_height`` (ledger + all its indexes in one transaction, then the
    plugin stores). Returns the removed rows."""
    node.logger.app_log.warning(f"Status: Rolling back below: {block_height}")
    removed = db_handler.rollback_under(block_height)
    _rollback_plugin_stores(node, db_handler, block_height)
    node.logger.app_log.warning(f"Status: Chain rolled back below {block_height} and will be resynchronized")
    return removed


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
    """``our_hash_at(height) -> block_hash | None`` from the ledger (what ``block_height_from_hash`` serves to
    peers, so both sides of a probe read the same store)."""
    def our_hash_at(height):
        try:
            return db_handler.store.block_hash(int(height))
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
    """Ledger + plugin stores + node tip variables -> ``ancestor`` (caller holds node.db_lock and has already
    taken its backup). Mirrors the legacy blocknf mutation, for N blocks at once."""
    db_handler.rollback_under(ancestor + 1)
    _rollback_plugin_stores(node, db_handler, ancestor + 1)
    node.last_block_timestamp = db_handler.last_block_timestamp()
    node.last_block_hash = db_handler.last_block_hash()
    node.last_block = ancestor
    node.hdd_hash = node.last_block_hash
    node.hdd_block = ancestor


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
        node.hdd_block = node.last_block
        node.hdd_hash = node.last_block_hash


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
                                         peer_ip, db_handler, False, revert=True))
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
                node.logger.app_log.warning(f"Node {peer_ip} didn't find block {db_block_height}({db_block_hash})")
                # ledger + every index in one transaction, then the plugin stores
                backup_data = db_handler.backup_higher(db_block_height)
                _rollback_plugin_stores(node, db_handler, db_block_height)

                node.last_block_timestamp = db_handler.last_block_timestamp()
                node.last_block_hash = db_handler.last_block_hash()
                node.last_block = db_block_height - 1
                node.hdd_hash = db_handler.last_block_hash()
                node.hdd_block = db_block_height - 1

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
                                                     peer_ip, db_handler, False, revert=True))  # will get stuck if you change it to respect node.db_lock
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
    """Seed the canonical genesis block (height 1) into an EMPTY ledger; a no-op once it has blocks. The
    chain above it is synced from peers (or mined, on regnet)."""
    import ledger_genesis
    import regnet
    store = node.ledger
    if store.tip() is not None:
        return False
    diff = regnet.REGNET_DIFF if getattr(node, "is_regnet", False) else 1
    store.commit_block(ledger_genesis.GENESIS_HEIGHT, ledger_genesis.GENESIS_BLOCK_HASH, ledger_genesis.rows(),
                       difficulty=diff, fork_height=None)
    store.set_allocations(ledger_genesis.allocations(node))
    node.logger.app_log.warning("Status: seeded the genesis block into a fresh ledger")
    return True


def bootstrap(node):
    """Bootstrap a FRESH (empty) ledger: unless ``sync_from_genesis`` is set, try a DB-direct snapshot of a
    peer's LMDB ledger (doc/43, tip height + hash verified; the digester re-validates forward), then fall
    back to seeding genesis and syncing the whole chain. Returns True if a snapshot was installed (the
    caller must reopen the ledger)."""
    if getattr(node, "sync_from_genesis", False) or not getattr(node, "bootstrap_p2p", False):
        return False
    try:
        import kvstore
        import snapshot_p2p
        dest = kvstore.store_path(node.ledger_path, "blockstore")
        cands = snapshot_p2p.candidates(node)
        tmp = dest + ".snapshot"
        got = snapshot_p2p.fetch_db_from_peers(node, cands, tmp) if cands else None
        if not got:
            node.logger.app_log.warning("Status: no DB-direct snapshot available from peers; syncing from genesis")
            return False
        if os.path.isdir(dest):
            shutil.rmtree(dest)
        os.replace(tmp, dest)
        node.logger.app_log.warning(f"Status: installed a peer ledger snapshot at height {got[1]['height']}")
        return True
    except Exception as e:
        node.logger.app_log.warning(f"Status: snapshot bootstrap failed ({e}); syncing from genesis")
        return False



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
