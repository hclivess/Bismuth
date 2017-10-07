import tarfile

files = ["ledger.db","hyper.db"]

tar = tarfile.open("ledger.tar.gz", "w:gz")

for file in files:
    print ("Compressing", file)
    tar.add(file, arcname=file)
print("Compression finished for", files)
tar.close()