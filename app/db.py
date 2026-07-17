"""Database connection and schema for the Arkia org-chart system."""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "org.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    user TEXT,
    tbl TEXT NOT NULL,
    row_key TEXT,
    field TEXT,
    old_val TEXT,
    new_val TEXT
);

-- ===== org chart (עץ ארגוני) =====
-- Self-propagating org tree: each manager fills only their direct reports.
-- A node's `token` is its secret magic-link key — whoever holds it may edit
-- ONLY that node's own children (no login). Root = department head (parent_id NULL).
CREATE TABLE IF NOT EXISTS org_nodes (
    id INTEGER PRIMARY KEY,
    parent_id INTEGER REFERENCES org_nodes(id),
    token TEXT UNIQUE NOT NULL,   -- secret magic-link key (every node gets one)
    dept TEXT NOT NULL DEFAULT '',-- groups a tree, e.g. 'finance'; set on the root
    name TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',-- E.164-ish digits for wa.me links
    is_manager INTEGER NOT NULL DEFAULT 0,  -- 1 = has people below → gets its own link
    status TEXT NOT NULL DEFAULT 'pending', -- 'pending' | 'filled' (manager said "done")
    created_by TEXT,              -- name of the manager (or 'admin') who added this node
    created_at TEXT DEFAULT (datetime('now','localtime')),
    filled_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_org_parent ON org_nodes(parent_id);
"""


def connect(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    return con


def init_db(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)
    con.commit()
