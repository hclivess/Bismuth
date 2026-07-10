"""Consensus-height tracking across peers (mixin)."""
from essentials import most_common_dict, percentage_in
from peers_reputation import PENALTY_HEIGHT_LIE


class PeersConsensusMixin:
    __slots__ = ()

    @property
    def consensus_most_common(self):
        """Consensus vote"""
        try:
            with self.peers_lock:                      # most_common_dict iterates the live opinion dict
                return most_common_dict(self.peer_opinion_dict)
        except:
            return 0

    @property
    def consensus_max(self):
        try:
            with self.peers_lock:
                return max(self.peer_opinion_dict.values())
        except:
            return 0

    @property
    def consensus_size(self):
        """Number of nodes in consensus"""
        return len(self.peer_opinion_dict)

    def consensus_add(self, peer_ip, consensus_blockheight, sdef, last_block):
        # Optimization: Early exit for too old blocks
        too_old = last_block - 720

        if peer_ip not in self.peer_opinion_dict and consensus_blockheight < too_old:
            self.app_log.warning(f"{peer_ip} received block too old ({consensus_blockheight}) for consensus")
            return

        try:
            self.app_log.info(f"Updating {peer_ip} in consensus")
            # Lock the write + the two tallies that iterate the dict, so a concurrent consensus_add/
            # consensus_remove from another peer thread can't resize it mid-iteration. penalize/warning
            # below re-enter the same RLock harmlessly.
            with self.peers_lock:
                self.peer_opinion_dict[peer_ip] = consensus_blockheight
                self.consensus = most_common_dict(self.peer_opinion_dict)
                self.consensus_percentage = percentage_in(self.peer_opinion_dict[peer_ip],
                                                         self.peer_opinion_dict.values())

            if (int(consensus_blockheight) > int(self.consensus) + 30 and
                self.consensus_percentage > 50 and
                len(self.peer_opinion_dict) > 10):
                # penalize the reputation for claiming a tip far above consensus it can't back, then the
                # legacy warning/ban (whitelist-immune + bounded, so this can't isolate the node).
                self.penalize(peer_ip, PENALTY_HEIGHT_LIE, "height lie")
                if self.warning(sdef, peer_ip, f"Consensus deviation too high, {peer_ip} banned", 10):
                    return

        except Exception as e:
            self.app_log.warning(f"consensus_add failed for {peer_ip}: {type(e).__name__}: {e}")
            raise

    def consensus_remove(self, peer_ip):
        with self.peers_lock:
            if peer_ip in self.peer_opinion_dict:
                try:
                    self.app_log.info(f"Will remove {peer_ip} from consensus pool")
                    self.peer_opinion_dict.pop(peer_ip)
                except:
                    raise
