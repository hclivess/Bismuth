"""Restore the ledger from a gzip tarball (snapshot utility).

A standalone restore script: it first removes stray SQLite WAL/SHM sidecar
files, then safely extracts ``ledger.tar.gz`` into the current directory,
guarding against path-traversal entries in the archive. The counterpart to the
``tar.py`` packing scripts. Not invoked by the node or the test suite.
"""

import tarfile
import glob
import os

types = ['*.db-wal', '*.db-shm']
for t in types:
    for f in glob.glob(t):
        os.remove(f)
        print(f, "deleted")

with tarfile.open("ledger.tar.gz") as tar:
    def is_within_directory(directory, target):
        """True if ``target`` resolves to a path inside ``directory``."""
        abs_directory = os.path.abspath(directory)
        abs_target = os.path.abspath(target)
    
        prefix = os.path.commonprefix([abs_directory, abs_target])
        
        return prefix == abs_directory
    
    def safe_extract(tar, path=".", members=None, *, numeric_owner=False):
        """Extract ``tar`` but refuse any member that would escape ``path``."""
        for member in tar.getmembers():
            member_path = os.path.join(path, member.name)
            if not is_within_directory(path, member_path):
                raise Exception("Attempted Path Traversal in Tar File")
    
        tar.extractall(path, members, numeric_owner=numeric_owner) 
        
    
    safe_extract(tar, "")