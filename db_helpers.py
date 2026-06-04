"""
Shared SQLite retry/commit helper.

Several modules (dbhandler, mempool, essentials, ledger_queries, staking) each historically carried
a near-identical "retry the query until it works" loop, with subtly different abort conditions,
sleep delays and give-up policies. ``retry_db`` centralises that control flow while letting every
call site keep its exact previous behavior via parameters:

    abort_on   exception types that stop retrying immediately (logged, then return None)
    delay      seconds to sleep between retries (0 = no sleep)
    max_tries  cap the number of attempts; when reached, call on_give_up() (or re-raise)
    on_give_up callable invoked once max_tries is hit
    log        a logger exposing .warning(), or None to stay silent

Passing ``abort_on=()`` and ``max_tries=None`` reproduces a plain "retry forever" loop.
"""
import time

__version__ = "0.0.1"


def retry_db(operation, *, abort_on=(), delay=1.0, max_tries=None,
             on_give_up=None, log=None, describe=""):
    """Call ``operation`` (a zero-arg callable performing the DB action) and return its result.

    On exception: if it is an instance of ``abort_on`` -> log and return None; otherwise log,
    sleep ``delay``, and retry until ``max_tries`` is reached (then ``on_give_up()`` if provided,
    else the exception re-raises).
    """
    tries = 0
    while True:
        try:
            return operation()
        except Exception as e:
            if abort_on and isinstance(e, abort_on):
                if log is not None:
                    log.warning("Database query aborted ({}): {}".format(describe, e))
                return None
            tries += 1
            if log is not None:
                log.warning("Database query retry ({}): {}".format(describe, e))
            if max_tries is not None and tries >= max_tries:
                if on_give_up is not None:
                    return on_give_up()
                raise
            if delay:
                time.sleep(delay)
