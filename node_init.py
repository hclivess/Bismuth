"""
Node bootstrap / initialization helpers lifted out of ``node.py``'s ``__main__`` wiring: net-type setup,
opening (and, on a fresh install, bootstrapping) the LMDB ledger, key loading and the tip priming. Each
takes ``node`` explicitly; the consensus chain ``verify`` stays in ``node.py``.
"""
import os
import sys

import block_store
import essentials
import kvstore
import mining_heavy3
import regnet
from chain_ops import bootstrap, seed_genesis
from difficulty import difficulty
from digest import digest_block
from essentials import checkpoint_set


def just_int_from(s):
    # TODO: move to essentials.py
    return int(''.join(i for i in s if i.isdigit()))


def setup_net_type(node):
    """
    Adjust globals depending on mainnet, testnet or regnet
    """
    # TODO: only deals with 'node' structure, candidate for single user mode.
    # Defaults value, dup'd here for clarity sake.
    node.is_mainnet = True
    node.is_testnet = False
    node.is_regnet = False

    if "testnet" in node.version or node.is_testnet:
        node.is_testnet = True
        node.is_mainnet = False
        node.version_allow = "testnet"

    if "regnet" in node.version or node.is_regnet:
        node.is_regnet = True
        node.is_testnet = False
        node.is_mainnet = False

    node.logger.app_log.warning(f"Testnet: {node.is_testnet}")
    node.logger.app_log.warning(f"Regnet : {node.is_regnet}")

    # default mainnet config
    node.peerfile = "peers.txt"

    if node.is_mainnet:
        # Allow only 21 and up. mainnet0023 signals the modern capabilities (REST API + negotiated
        # transport compression), advertised via the `getcapabilities` handshake; 0021/0022 peers still
        # interoperate over the plain socket protocol. This bump is NOT a consensus change — nothing in
        # digest.py / bismuth_serialize.py gates on the version string (verified).
        if node.version != 'mainnet0023':
            node.version = 'mainnet0023'  # Force in code.
        if "mainnet0021" not in node.version_allow:
            node.version_allow = ['mainnet0021', 'mainnet0022', 'mainnet0023']
        # Do not allow bad configs.
        if not 'mainnet' in node.version:
            node.logger.app_log.error("Bad mainnet version, check config.txt")
            sys.exit()
        num_ver = just_int_from(node.version)
        if num_ver <21:
            node.logger.app_log.error("Too low mainnet version, check config.txt")
            sys.exit()
        for allowed in node.version_allow:
            num_ver = just_int_from(allowed)
            if num_ver < 20:
                node.logger.app_log.error("Too low allowed version, check config.txt")
                sys.exit()

    if "testnet" in node.version or node.is_testnet:
        node.port = 2829
        node.ledger_path = "static/ledger_test.db"
        node.peerfile = "peers_test.txt"
        if not 'testnet' in node.version:
            node.logger.app_log.error("Bad testnet version, check config.txt")
            sys.exit()

    if "regnet" in node.version or node.is_regnet:
        node.port = regnet.REGNET_PORT
        node.ledger_path = regnet.REGNET_DB
        node.peerfile = regnet.REGNET_PEERS
        if not 'regnet' in node.version:
            node.logger.app_log.error("Bad regnet version, check config.txt")
            sys.exit()
        if not node.heavy:
            node.logger.app_log.warning("Regnet with no heavy file...")
            mining_heavy3.heavy = False
        node.logger.app_log.warning("Regnet init...")
        regnet.init(node.logger.app_log)
        regnet.DIGEST_BLOCK = digest_block
        mining_heavy3.is_regnet = True
        """
        node.logger.app_log.warning("Regnet still is WIP atm.")
        sys.exit()
        """

    # hf2 activation height: load the persisted lock-in NOW — node.ledger_path is final for the chosen
    # net only from this point (the testnet/regnet branches above override it), and the sidecar is
    # namespaced BY LEDGER FILENAME (fork.lockin_path). Loading any earlier reads the mainnet-named
    # sidecar on a regnet/testnet node (the same cross-net bleed class as the 2026-06-09 incident).
    # Stays None until loaded here or until the digester locks it in from the chain.
    try:
        import fork as _fork_persist
        node.fork_height = _fork_persist.load_locked_height(node.ledger_path, "hf2")
        if node.fork_height is not None:
            node.logger.app_log.warning(
                f"Status: hf2 activation height {node.fork_height} replayed from the lock-in sidecar")
        # Authoritative operator/snapshot PIN (doc/17). A node bootstrapped from a ledger snapshot has no
        # lock-in sidecar, so until its first digest re-derives the height the on-chain detection can return None and leave the node validating a POST-fork chain under PRE-fork rules — a hard
        # split. This pin (BISMUTH_HF2_FORK_HEIGHT env / node.hf2_fork_height config) lets the height be
        # supplied explicitly for exactly that case. It is cross-checked against the sidecar: a DISAGREEMENT
        # is a guaranteed chain split, so we HALT loudly rather than pick one. When only the pin is present it
        # becomes the source of truth and is persisted to the sidecar so restarts replay it.
        import os as _os_pin
        _pin = _os_pin.environ.get("BISMUTH_HF2_FORK_HEIGHT") or getattr(node, "hf2_fork_height", None)
        _pin = int(_pin) if _pin not in (None, "", "0", 0) else None
        if _pin is not None:
            if node.fork_height is not None and int(node.fork_height) != _pin:
                node.logger.app_log.error(
                    f"FATAL: hf2 fork-height pin {_pin} disagrees with the lock-in sidecar "
                    f"{node.fork_height} — one of them is wrong and running either way splits the chain. "
                    f"Resolve before starting.")
                sys.exit(1)
            if node.fork_height is None:
                node.fork_height = _pin
                _fork_persist.save_locked_height(node.ledger_path, "hf2", _pin)
                node.logger.app_log.warning(
                    f"Status: hf2 activation height {_pin} taken from the operator pin and persisted")
    except SystemExit:
        raise
    except Exception:
        node.fork_height = None


def open_ledger(node):
    """Open the node's LMDB ledger (``<ledger_path>`` -> ``blockstore-<name>``). A fresh install first tries
    a DB-direct peer snapshot (doc/43), else seeds the genesis block and syncs from peers. Sets
    ``node.ledger`` (and ``node.block_store``, the same object)."""
    path = kvstore.store_path(node.ledger_path, "blockstore")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if not os.path.exists(os.path.join(path, "data.mdb")) and not node.is_regnet:
        bootstrap(node)
    node.ledger = block_store.open_ledger(path)
    node.block_store = node.ledger
    schema = node.ledger.schema()
    if node.ledger.tip() is not None and schema != block_store.SCHEMA:
        node.logger.app_log.critical(
            f"Status: the ledger at {path} has schema {schema}, this node needs {block_store.SCHEMA}: "
            f"run scripts/upgrade_ledger.py {path} with the node stopped")
        sys.exit(1)
    seed_genesis(node)
    import ledger_genesis
    if node.ledger.tip() is not None and node.ledger.allocations() != ledger_genesis.allocations(node):
        node.ledger.set_allocations(ledger_genesis.allocations(node))
        node.logger.app_log.warning("Status: applied the fixed ledger allocations")
    node.logger.app_log.warning(f"Status: ledger open at {path} (tip {node.ledger.tip()}, "
                                f"{node.ledger.tx_count()} txs)")


def node_block_init(node, database):
    node.hdd_block = database.block_height_max()
    node.difficulty = difficulty(node, database)  # check diff for miner
    node.last_block = node.hdd_block
    node.last_block_hash = database.last_block_hash()
    node.hdd_hash = node.last_block_hash
    node.last_block_timestamp = database.last_block_timestamp()
    checkpoint_set(node)


def load_keys(node):
    """Initial loading of crypto keys"""
    # TODO: candidate for single user mode
    essentials.keys_check(node.logger.app_log, "wallet.der")

    node.keys.key, node.keys.public_key_readable, node.keys.private_key_readable, _, _, node.keys.public_key_b64encoded, node.keys.address, node.keys.keyfile = essentials.keys_load(
        "privkey.der", "pubkey.der")

    if node.is_regnet:
        regnet.PRIVATE_KEY_READABLE = node.keys.private_key_readable
        regnet.PUBLIC_KEY_B64ENCODED = node.keys.public_key_b64encoded
        regnet.ADDRESS = node.keys.address
        regnet.KEY = node.keys.key

    node.logger.app_log.warning(f"Status: Local address: {node.keys.address}")
