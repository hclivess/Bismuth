import tarfile
import glob
import os

types = ['*.db-wal', '*.db-shm']
for t in types:
    for f in glob.glob(t):
        os.remove(f)
        print(f, "deleted")

with tarfile.open("ledger.tar.gz") as tar:
    tar.extractall("")