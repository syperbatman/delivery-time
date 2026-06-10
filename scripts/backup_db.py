"""
Ежедневный бэкап SQLite-базы бота. Запускается кроном:

    0 4 * * * root /root/delivery-time/.venv/bin/python /root/delivery-time/scripts/backup_db.py

Снимок делается через sqlite3 backup API (консистентно даже при WAL и работающем
боте — обычный cp может скопировать базу в середине записи). Хранятся последние
KEEP копий; на 50 пользователей база крошечная, место не съест.
"""

import glob
import os
import sqlite3
from datetime import date

SRC = os.environ.get("DB_PATH", "/root/delivery-time/delivery.db")
DST_DIR = os.environ.get("BACKUP_DIR", "/root/backups")
KEEP = 14

if not os.path.exists(SRC):
    raise SystemExit(f"база не найдена: {SRC}")

os.makedirs(DST_DIR, exist_ok=True)
dst = os.path.join(DST_DIR, f"delivery-{date.today().isoformat()}.db")

src_conn = sqlite3.connect(SRC)
dst_conn = sqlite3.connect(dst)
with dst_conn:
    src_conn.backup(dst_conn)
dst_conn.close()
src_conn.close()

backups = sorted(glob.glob(os.path.join(DST_DIR, "delivery-*.db")))
for old in backups[:-KEEP]:
    os.remove(old)

print(f"backup ok: {dst} ({os.path.getsize(dst)} bytes), хранится копий: {min(len(backups), KEEP)}")
