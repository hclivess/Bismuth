import tarfile

files = ["ledger.db","hyper.db"]

tar = tarfile.open("ledger.tar.gz", "w:gz")

for file in files:
    tar.add(file, arcname=file)
tar.close()