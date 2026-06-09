"""Periodic ledger-file backup loop (snapshot utility).

A tiny standalone script that copies ``ledger.db`` to a fixed destination
(here a Windows Google Drive path) every ``interval`` seconds, forever,
printing a timestamped line after each copy. Edit the destination path before
use. Not invoked by the node or the test suite.
"""

from shutil import copyfile
import time
interval = 43200

while True:
    copyfile("ledger.db", "C:\\Users\\Meegopad\\Google Drive\\ledger\\ledger.db")
    print("Backup complete at {}, interval of {} minutes ({} hours)".format(time.strftime("%Y/%m/%d,%H:%M:%S",
                                                                            time.gmtime()),
                                                                            interval/60,
                                                                            interval/60/60))
    time.sleep(interval)
