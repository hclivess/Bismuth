import tarfile

files = ["ledger.db-wal","ledger.db-shm","ledger.db","hyper.db-shm", "hyper.db-wal", "hyper.db", "index.db"]

tar = tarfile.open("ledger.tar.gz", "w:gz")

for file in files:
    try:
        print ("Compressing", file)
        tar.add(file, arcname=file)
    except:
        "Error compressing {}".format(file)

print("Compression finished for", files)
tar.close()
