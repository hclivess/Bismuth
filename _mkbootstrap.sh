#!/bin/bash
# Build a fresh bootstrap snapshot from the LIVE node (no downtime) and publish it at bismuth.cz.
# Uses SQLite online backup (WAL-safe consistent copy) so the node keeps syncing + serving the API.
set -e
SNAP=/var/bootsnap
WEBROOT=/var/www/bismuth.cz
STATIC=/root/bismuth-claude/Bismuth/static
rm -rf "$SNAP"; mkdir -p "$SNAP"

echo "[mkboot] online-backup of ledger.db + hyper.db + index.db (WAL-safe)..."
python3 - <<'PY'
import sqlite3, os
src='/root/bismuth-claude/Bismuth/static'; dst='/var/bootsnap'
for db in ['ledger.db','hyper.db','index.db']:
    s=sqlite3.connect(os.path.join(src,db), timeout=120)
    d=sqlite3.connect(os.path.join(dst,db))
    with d:
        s.backup(d)
    d.close(); s.close()
    print('[mkboot] backed up', db, os.path.getsize(os.path.join(dst,db)), 'bytes')
PY

H=$(python3 -c "import sqlite3;c=sqlite3.connect('/var/bootsnap/ledger.db');print(c.execute('SELECT max(block_height) FROM transactions').fetchone()[0]);c.close()")
echo "[mkboot] snapshot height $H"

echo "[mkboot] tar + pigz (parallel) ..."
tar -I pigz -cf "$WEBROOT/ledger.tar.gz.tmp" -C "$SNAP" ledger.db hyper.db index.db
mv "$WEBROOT/ledger.tar.gz.tmp" "$WEBROOT/ledger.tar.gz"   # atomic publish
echo "$H" > "$WEBROOT/HEIGHT"
rm -rf "$SNAP"

SZ=$(stat -c %s "$WEBROOT/ledger.tar.gz")
echo "[mkboot] DONE: ledger.tar.gz = $(numfmt --to=iec $SZ) at height $H"
