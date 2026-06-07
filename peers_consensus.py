"""Consensus-height tracking across peers (mixin)."""
from essentials import most_common_dict, percentage_in


class PeersConsensusMixin:
    __slots__ = ()

    @property
    def consensus_most_common(self):
        """Consensus vote"""
        try:
            return most_common_dict(self.peer_opinion_dict)
        except:
            return 0

    @property
    def consensus_max(self):
        try:
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
            self.peer_opinion_dict[peer_ip] = consensus_blockheight

            self.consensus = most_common_dict(self.peer_opinion_dict)
            self.consensus_percentage = percentage_in(self.peer_opinion_dict[peer_ip],
                                                     self.peer_opinion_dict.values())

            if (int(consensus_blockheight) > int(self.consensus) + 30 and
                self.consensus_percentage > 50 and
                len(self.peer_opinion_dict) > 10):
                if self.warning(sdef, peer_ip, f"Consensus deviation too high, {peer_ip} banned", 10):
                    return

        except Exception as e:
            self.app_log.warning(f"consensus_add failed for {peer_ip}: {type(e).__name__}: {e}")
            raise

    def consensus_remove(self, peer_ip):
        if peer_ip in self.peer_opinion_dict:
            try:
                self.app_log.info(f"Will remove {peer_ip} from consensus pool {self.peer_opinion_dict}")
                self.peer_opinion_dict.pop(peer_ip)
            except:
                raise
