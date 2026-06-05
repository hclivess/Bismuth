"""
Mempool SQL statements and tuning constants, extracted from ``mempool.py`` so the query mixin and the
Mempool core share one definition. ``mempool`` re-exports them (``from mempool_sql import *``), so
external ``mp.SQL_*`` / ``mp.REFUSE_OLDER_THAN`` access is unchanged.
"""
from decimal import Decimal


DECIMAL0 = Decimal(0)

# If set to true, will always send empty Tx to other peers (but will accept theirs)
# Only to be used for debug/testing purposes
DEBUG_DO_NOT_SEND_TX = False

# Tx age limit (in seconds) - Default 82800
# REFUSE_OLDER_THAN = 82800
REFUSE_OLDER_THAN = 60 * 60 * 2  # reduced to 2 hours
# See also SQL_PURGE, SQL_MEMPOOL_GET and SQL_SELECT_ALL_VALID_TXS a few lines down.
# I used a filter on some requests rather than calling purge() every time.
# Maybe a systematic purge() would be easier and faster. To be tested.

# How long for freeze nodes that send late enough tx we already have in ledger
FREEZE_MIN = 5

"""
Common Sql requests
"""

# Create mempool table
SQL_CREATE = "CREATE TABLE IF NOT EXISTS transactions (" \
             "timestamp TEXT, address TEXT, recipient TEXT, amount TEXT, signature TEXT, " \
             "public_key TEXT, operation TEXT, openfield TEXT, mergedts INTEGER(4) not null default (strftime('%s','now')) )"

# Purge old txs that may be stuck
SQL_PURGE = "DELETE FROM transactions WHERE timestamp <= strftime('%s', 'now', '-2 hour')"

# Delete all transactions
SQL_CLEAR = "DELETE FROM transactions"

# Check for presence of a given tx signature
SQL_SIG_CHECK = 'SELECT timestamp FROM transactions WHERE substr(signature,1,4) = substr(?1,1,4) and signature = ?1'
SQL_SIG_CHECK_OLD = 'SELECT timestamp FROM transactions WHERE signature = ?1'

# delete a single tx
SQL_DELETE_TX = 'DELETE FROM transactions WHERE substr(signature,1,4) = substr(?1,1,4) and signature = ?1'
SQL_DELETE_TX_OLD = 'DELETE FROM transactions WHERE signature = ?1'

# Selects all tx from mempool - list fields so we don't send mergedts and keep compatibility
SQL_SELECT_ALL_TXS = 'SELECT timestamp, address, recipient, amount, signature, public_key, operation, openfield FROM transactions'

# Selects all tx from mempool - list fields so we don't send mergedts and keep compatibility
SQL_SELECT_ALL_VALID_TXS = "SELECT timestamp, address, recipient, amount, signature, public_key, operation, openfield FROM transactions WHERE timestamp > strftime('%s', 'now', '-2 hour')"

# Counts distinct senders from mempool
SQL_COUNT_DISTINCT_SENDERS = 'SELECT COUNT(DISTINCT(address)) FROM transactions'

# Counts distinct recipients from mempool
SQL_COUNT_DISTINCT_RECIPIENTS = 'SELECT COUNT(DISTINCT(recipient)) FROM transactions'

# A single requets for status info
SQL_STATUS = 'SELECT COUNT(*) AS nb, SUM(LENGTH(openfield)) AS len, COUNT(DISTINCT(address)) as senders, COUNT(DISTINCT(recipient)) as recipients FROM transactions'

# Select Tx to be sent to a peer
SQL_SELECT_TX_TO_SEND = 'SELECT * FROM transactions ORDER BY amount DESC'

# Select Tx to be sent to a peer since the given ts - what counts is the merged time, not the tx time.
SQL_SELECT_TX_TO_SEND_SINCE = 'SELECT * FROM transactions where mergedts > ? ORDER BY amount DESC'

SQL_MEMPOOL_GET = "SELECT amount, openfield, operation FROM transactions WHERE address = ? and timestamp > strftime('%s', 'now', '-2 hour')"
