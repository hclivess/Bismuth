"""
Lightweight, idempotent schema migrations for the Bismuth SQLite databases (see doc/16).

Versioning uses SQLite's native ``PRAGMA user_version`` (an integer stored in the DB header), so no
bookkeeping table is needed. Each migration is ``(version, step)`` where ``step`` is either a list of
SQL statements or a ``callable(cursor)``; migrations are applied in ascending order and any already
applied (``version <= user_version``) are skipped.

Rules: migrations MUST be idempotent (e.g. ``CREATE ... IF NOT EXISTS``) and MUST NOT change consensus
serialization (that lives, frozen, in bismuth_serialize.py). This framework is the foundation for the
later storage-rework phases (balance index, schema cleanup, …).
"""

__version__ = "0.0.1"


def ledger_migrations(old_sqlite=False):
    """Migrations for the ledger / hyper / RAM databases (transactions + misc).

    v1 reproduces the indexes the node has always created at startup: the partial-signature index
    (skipped on very old SQLite that cannot index ``substr``) and the misc block-height index.
    """
    v1 = ["CREATE INDEX IF NOT EXISTS 'Misc Block Height Index' ON misc(block_height)"]
    if not old_sqlite:
        v1.insert(0, "CREATE INDEX IF NOT EXISTS TXID4_Index ON transactions(substr(signature,1,4))")
    return [(1, v1)]


def _user_version(cursor):
    cursor.execute("PRAGMA user_version")
    return cursor.fetchone()[0]


def run(cursor, migrations, app_log=None):
    """Apply pending `migrations` to the database behind `cursor`. Returns the resulting version."""
    current = _user_version(cursor)
    for version, step in migrations:
        if version <= current:
            continue
        if callable(step):
            step(cursor)
        else:
            for sql in step:
                cursor.execute(sql)
        cursor.execute("PRAGMA user_version = {}".format(int(version)))
        if app_log:
            app_log.warning("DB migrated to schema version {}".format(version))
    try:
        cursor.connection.commit()
    except Exception:
        pass
    return _user_version(cursor)
