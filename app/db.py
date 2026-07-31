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

-- ===== החזרי הוצאות / ריכוז אשראי (expense reimbursement & credit summary) =====
-- A user builds a monthly report (reimbursement or credit-card summary), adds an
-- invoice photo per line (supplier + amount pre-filled by OCR, category chosen from
-- a managed list), then "produces a form": a branded PDF. Reimbursement reports go
-- to an approver via a secret magic-link (no login, like the org tree); any report
-- can also be e-mailed to a free-typed address. Settings tables are admin-managed.

-- per-user profile: department is "defined by the user" (per the spec). Login still
-- goes through users.txt / ARKIA_USERS; this only holds display/email/department.
CREATE TABLE IF NOT EXISTS exp_profiles (
    username TEXT PRIMARY KEY,     -- lower-cased login username
    display_name TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    department TEXT NOT NULL DEFAULT '',
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

-- expense classifications shown in the line-item dropdown (admin-managed)
CREATE TABLE IF NOT EXISTS exp_categories (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    sort INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1
);

-- approving managers a user can pick from (admin-managed name + email)
CREATE TABLE IF NOT EXISTS exp_approvers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1
);

-- department list (for dropdowns); a user's own department lives on exp_profiles
CREATE TABLE IF NOT EXISTS exp_departments (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1
);

-- one report = one month, one type, one owner
CREATE TABLE IF NOT EXISTS exp_reports (
    id INTEGER PRIMARY KEY,
    type TEXT NOT NULL DEFAULT 'reimbursement',  -- 'reimbursement' | 'credit'
    owner TEXT NOT NULL,                          -- username (lower)
    title TEXT NOT NULL DEFAULT '',               -- report/form name shown on PDF & mail
    month TEXT NOT NULL DEFAULT '',               -- 'YYYY-MM'
    department TEXT NOT NULL DEFAULT '',
    approver_id INTEGER REFERENCES exp_approvers(id),
    status TEXT NOT NULL DEFAULT 'draft',         -- draft|pending|approved|rejected|compiled
    approve_token TEXT UNIQUE,                     -- secret magic-link for the approver
    decision_note TEXT,                           -- approver's reason on reject/approve
    sent_to TEXT,                                 -- last free-typed email the form was sent to
    sent_at TEXT,
    total REAL NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    submitted_at TEXT,
    decided_at TEXT
);

-- one line per invoice; seq = the document number (1..N) used across the PDF
CREATE TABLE IF NOT EXISTS exp_lines (
    id INTEGER PRIMARY KEY,
    report_id INTEGER NOT NULL REFERENCES exp_reports(id),
    seq INTEGER NOT NULL DEFAULT 0,
    supplier TEXT NOT NULL DEFAULT '',
    amount REAL NOT NULL DEFAULT 0,
    category_id INTEGER REFERENCES exp_categories(id),
    invoice_path TEXT,                            -- data/uploads/... (never in the repo)
    ocr_raw TEXT,                                 -- raw OCR text, for debugging/audit
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_exp_lines_report ON exp_lines(report_id);

-- passkeys (WebAuthn credentials) registered per user for Face ID / fingerprint login
CREATE TABLE IF NOT EXISTS exp_webauthn (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL,
    credential_id TEXT NOT NULL UNIQUE,
    public_key TEXT NOT NULL,
    sign_count INTEGER NOT NULL DEFAULT 0,
    label TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
"""

# seeded once when exp_categories is empty — admin can edit/extend later
DEFAULT_CATEGORIES = [
    "דלק", "אש\"ל", "חניה", "כיבוד", "נסיעות", "לינה",
    "ציוד משרדי", "טלפון ותקשורת", "אחר",
]


def connect(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    return con


def init_db(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)
    if con.execute("SELECT COUNT(*) c FROM exp_categories").fetchone()["c"] == 0:
        con.executemany(
            "INSERT INTO exp_categories (name, sort) VALUES (?, ?)",
            [(name, i) for i, name in enumerate(DEFAULT_CATEGORIES)],
        )
    con.commit()
