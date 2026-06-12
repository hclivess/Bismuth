"""
Node bootstrap / initialization helpers lifted out of ``node.py``'s ``__main__`` wiring: net-type
setup, RAM-ledger load, the initial DB/schema check, key loading, the ram/height priming and the
index/migration step. Each already operated on the global ``node`` singleton, so it takes ``node``
explicitly now (pure relocation by dependency injection — bodies are byte-identical to the originals
apart from the added param). ``bootstrap`` comes from ``chain_ops`` and ``sql_trace_callback`` from
``dbhandler``; the consensus chain ``verify`` deliberately stays in ``node.py``.
"""
import functools
import glob
import os
import sqlite3
import sys
import tarfile

import aliases
import db_migrations
import dbhandler
import essentials
import mining_heavy3
import regnet
from chain_ops import bootstrap
from dbhandler import sql_trace_callback
from difficulty import difficulty
from digest import digest_block
from essentials import checkpoint_set, download_file


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
    node.ledger_ram_file = "file:ledger?mode=memory&cache=shared"
    node.index_db = "static/index.db"

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
        node.hyper_path = "static/hyper_test.db"
        node.ledger_path = "static/ledger_test.db"

        node.ledger_ram_file = "file:ledger_testnet?mode=memory&cache=shared"
        #node.hyper_recompress = False
        node.peerfile = "peers_test.txt"
        node.index_db = "static/index_test.db"
        if not 'testnet' in node.version:
            node.logger.app_log.error("Bad testnet version, check config.txt")
            sys.exit()

        redownload_test = input("Status: Welcome to the testnet. Redownload test ledger? y/n")
        if redownload_test == "y":
            types = ['static/ledger_test.db-wal', 'static/ledger_test.db-shm', 'static/index_test.db', 'static/hyper_test.db-wal', 'static/hyper_test.db-shm']
            for type in types:
                for file in glob.glob(type):
                    os.remove(file)
                    print(file, "deleted")
            download_file("https://bismuth.cz/test.tar.gz", "static/test.tar.gz")
            with tarfile.open("static/test.tar.gz") as tar:
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
                    
                
                safe_extract(tar, "static/")
        else:
            print("Not redownloading test db")

    if "regnet" in node.version or node.is_regnet:
        node.port = regnet.REGNET_PORT
        node.hyper_path = regnet.REGNET_DB
        node.ledger_path = regnet.REGNET_DB
        node.ledger_ram_file = "file:ledger_regnet?mode=memory&cache=shared"
        node.hyper_recompress = False
        node.peerfile = regnet.REGNET_PEERS
        node.index_db = regnet.REGNET_INDEX
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
    except Exception:
        node.fork_height = None


def node_block_init(node, database):
    # TODO: candidate for single user mode
    node.hdd_block = database.block_height_max()
    node.difficulty = difficulty(node, database)  # check diff for miner

    node.last_block = node.hdd_block  # ram equals drive at this point

    node.last_block_hash = database.last_block_hash()
    node.hdd_hash = node.last_block_hash # ram equals drive at this point

    node.last_block_timestamp = database.last_block_timestamp()

    checkpoint_set(node)

    node.logger.app_log.warning("Status: Indexing aliases")

    # When the tokens_aliases plugin owns the index (node.token_index set, doc/27), this no-ops — the
    # plugin already backfilled tokens AND aliases from the ledger in plugin_manager.start(). On the legacy
    # path it indexes aliases into index.db as before.
    aliases.aliases_update(node, database)


def ram_init(node, database):
    # TODO: candidate for single user mode
    try:
        if node.ram:
            node.logger.app_log.warning("Status: Moving database to RAM")

            if node.py_version >= 370:
                temp_target = sqlite3.connect(node.ledger_ram_file, uri=True, isolation_level=None, timeout=1)
                if node.trace_db_calls:
                    temp_target.set_trace_callback(functools.partial(sql_trace_callback,node.logger.app_log,"TEMP-TARGET"))

                temp_source = sqlite3.connect(node.hyper_path, uri=True, isolation_level=None, timeout=1)
                if node.trace_db_calls:
                    temp_source.set_trace_callback(functools.partial(sql_trace_callback,node.logger.app_log,"TEMP-SOURCE"))
                temp_source.backup(temp_target)
                temp_source.close()

            else:
                source_db = sqlite3.connect(node.hyper_path, timeout=1)
                if node.trace_db_calls:
                    source_db.set_trace_callback(functools.partial(sql_trace_callback,node.logger.app_log,"SOURCE-DB"))
                database.to_ram = sqlite3.connect(node.ledger_ram_file, uri=True, timeout=1, isolation_level=None)
                if node.trace_db_calls:
                    database.to_ram.set_trace_callback(functools.partial(sql_trace_callback,node.logger.app_log,"DATABASE-TO-RAM"))
                database.to_ram.text_factory = str
                database.tr = database.to_ram.cursor()

                query = "".join(line for line in source_db.iterdump())
                database.to_ram.executescript(query)
                source_db.close()

            node.logger.app_log.warning("Status: Hyperblock ledger moved to RAM")

            #source = sqlite3.connect('existing_db.db')
            #dest = sqlite3.connect(':memory:')
            #source.backup(dest)

    except Exception as e:
        node.logger.app_log.warning(e)
        raise


def initial_db_check(node):
    """
    Initial bootstrap check and chain validity control
    """
    # TODO: candidate for single user mode
    # force bootstrap via adding an empty "fresh_sync" file in the dir.
    if os.path.exists("fresh_sync") and node.is_mainnet:
        node.logger.app_log.warning("Status: Fresh sync required, bootstrapping from the website")
        os.remove("fresh_sync")
        bootstrap(node)
    # UPDATE mainnet DB if required
    if node.is_mainnet:
        upgrade = sqlite3.connect(node.ledger_path)
        if node.trace_db_calls:
            upgrade.set_trace_callback(functools.partial(sql_trace_callback,node.logger.app_log,"INITIAL_DB_CHECK"))
        u = upgrade.cursor()
        try:
            u.execute("PRAGMA table_info(transactions);")
            # HARDFORK / cleanup (doc/16): brittle schema sniff — a hardcoded column index [10]
            # (operation) plus a string-equality type check. The integer-storage migration declares
            # explicit column types (amount/fee/reward INTEGER, timestamp/text TEXT), so this ad-hoc
            # probe should become a real schema-version check (PRAGMA user_version; see db_migrations.py).
            result = u.fetchall()[10][2]
            if result != "TEXT":
                raise ValueError("Database column type outdated for Command field")
            upgrade.close()
        except Exception as e:
            print(e)
            upgrade.close()
            print("Database needs upgrading, bootstrapping...")
            bootstrap(node)


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


def add_indices(node, db_handler: dbhandler.DbHandler):
    # Versioned, idempotent schema migrations (db_migrations.py, see doc/16). v1 creates exactly the
    # indexes this function historically created -- the TXID4 partial-signature index (skipped when
    # old_sqlite) and the misc block-height index -- now tracked via PRAGMA user_version on each DB.
    node.logger.app_log.warning("Applying schema migrations / creating indices")
    if node.old_sqlite:
        node.logger.app_log.warning("old_sqlite is True, skipping the signature-prefix index; lookups will be slower.")
    migrations = db_migrations.ledger_migrations(old_sqlite=node.old_sqlite)
    for cursor in (db_handler.h, db_handler.h2, db_handler.c):
        db_migrations.run(cursor, migrations, app_log=node.logger.app_log)
    node.logger.app_log.warning("Finished schema migrations")
