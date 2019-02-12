def fork():
    POW_FORK = 854660
    FORK_AHEAD = 5
    FORK_DIFF = 108.9
    return POW_FORK, FORK_AHEAD, FORK_DIFF

def limit_version(node):
    if 'mainnet0018' in node.version_allow:
        node.logger.app_log.warning(f"Beginning to reject mainnet0018 - block {node.last_block}")
        node.version_allow.remove('mainnet0018')
