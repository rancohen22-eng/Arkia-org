#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""One-time migration: copy the org tree from the pricing DB into this app's DB.

Run ONCE on the server after arkia-org is deployed, before removing the org
feature from the pricing system:

    cd /opt/arkia-org
    .venv/bin/python migrate_from_pricing.py            # dry run (shows counts)
    .venv/bin/python migrate_from_pricing.py --commit   # actually copy

By default it reads /opt/arkia-pricing/data/pricing.db and writes data/org.db.
Both tables share the same schema, so rows (ids, tokens, parent links) are copied
verbatim — existing magic-links keep working.
"""
import argparse
import sqlite3
import sys
from pathlib import Path

from app.db import DB_PATH as DST_DB, connect, init_db

COLS = ("id", "parent_id", "token", "dept", "name", "title", "phone",
        "is_manager", "status", "created_by", "created_at", "filled_at")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="/opt/arkia-pricing/data/pricing.db",
                    help="path to the pricing SQLite DB")
    ap.add_argument("--commit", action="store_true", help="perform the copy (default: dry run)")
    ap.add_argument("--force", action="store_true", help="overwrite even if the target already has rows")
    args = ap.parse_args()

    src_path = Path(args.src)
    if not src_path.exists():
        print(f"❌ מקור לא נמצא: {src_path}")
        return 1

    src = sqlite3.connect(src_path)
    src.row_factory = sqlite3.Row
    has_tbl = src.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='org_nodes'").fetchone()
    if not has_tbl:
        print("❌ בקובץ המקור אין טבלת org_nodes — אין מה להעביר.")
        return 1
    rows = src.execute(f"SELECT {', '.join(COLS)} FROM org_nodes ORDER BY id").fetchall()
    src.close()
    print(f"נמצאו {len(rows)} צמתים במקור ({src_path}).")

    dst = connect()
    init_db(dst)
    existing = dst.execute("SELECT COUNT(*) c FROM org_nodes").fetchone()["c"]
    if existing and not args.force:
        print(f"⚠️ ביעד ({DST_DB}) כבר יש {existing} צמתים. הוסף --force כדי לדרוס.")
        dst.close()
        return 1

    if not args.commit:
        print("(הרצת יבש — לא בוצע שינוי). הוסף --commit כדי להעביר בפועל.")
        dst.close()
        return 0

    dst.execute("DELETE FROM org_nodes")
    dst.executemany(
        f"INSERT INTO org_nodes ({', '.join(COLS)}) VALUES ({', '.join(['?'] * len(COLS))})",
        [tuple(r[c] for c in COLS) for r in rows])
    dst.commit()
    n = dst.execute("SELECT COUNT(*) c FROM org_nodes").fetchone()["c"]
    dst.close()
    print(f"✅ הועברו {n} צמתים אל {DST_DB}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
