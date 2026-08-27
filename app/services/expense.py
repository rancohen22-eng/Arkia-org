# -*- coding: utf-8 -*-
"""Expense-reimbursement / credit-summary data layer (החזרי הוצאות / ריכוז אשראי).

A user builds a monthly report and adds one line per invoice (supplier + amount,
pre-filled by OCR, category from the admin-managed list). Reimbursement reports
are sent to an approver by a secret magic-link (like the org tree's tokens); any
report can also be e-mailed to a free-typed address. This module owns the SQLite
reads/writes; routes live in ``app/main.py`` and higher-level flows (OCR, PDF,
mail) get their own service modules in later phases.

Phase 1 scope: the admin-managed settings entities (profiles, categories,
approvers, departments) plus the report/line skeleton used by later phases.
"""
from __future__ import annotations

import secrets

from .. import config


def _new_token() -> str:
    return secrets.token_urlsafe(24)


# ==================== profiles (per-user department / email) ====================

def get_profile(con, username: str):
    return con.execute(
        "SELECT * FROM exp_profiles WHERE username=?", ((username or "").strip().lower(),)
    ).fetchone()


def list_profiles(con):
    return con.execute(
        "SELECT * FROM exp_profiles ORDER BY display_name, username"
    ).fetchall()


def upsert_profile(con, username: str, display_name: str, email: str,
                   department: str, is_active: bool = True, approver_id=None) -> None:
    u = (username or "").strip().lower()
    if not u:
        raise ValueError("username required")
    approver_id = int(approver_id) if approver_id else None
    con.execute(
        "INSERT INTO exp_profiles (username, display_name, email, department, approver_id, is_active) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(username) DO UPDATE SET "
        "display_name=excluded.display_name, email=excluded.email, "
        "department=excluded.department, approver_id=excluded.approver_id, "
        "is_active=excluded.is_active",
        (u, display_name.strip(), email.strip(), department.strip(),
         approver_id, 1 if is_active else 0),
    )
    con.commit()


def profile_approver_id(con, username: str):
    """The approver assigned to this user by the admin, or None."""
    p = get_profile(con, username)
    return p["approver_id"] if p and p["approver_id"] is not None else None


def delete_profile(con, username: str) -> None:
    con.execute("DELETE FROM exp_profiles WHERE username=?",
                ((username or "").strip().lower(),))
    con.commit()


# ==================== categories / approvers / departments ====================
# Three small admin-managed lists sharing the same CRUD shape.

def list_categories(con, active_only: bool = False):
    q = "SELECT * FROM exp_categories"
    if active_only:
        q += " WHERE is_active=1"
    q += " ORDER BY sort, id"
    return con.execute(q).fetchall()


def add_category(con, name: str, sort: int = 0) -> int:
    cur = con.execute("INSERT INTO exp_categories (name, sort) VALUES (?, ?)",
                      (name.strip(), int(sort)))
    con.commit()
    return cur.lastrowid


def update_category(con, cid: int, name: str, sort: int, is_active: bool) -> bool:
    cur = con.execute(
        "UPDATE exp_categories SET name=?, sort=?, is_active=? WHERE id=?",
        (name.strip(), int(sort), 1 if is_active else 0, cid))
    con.commit()
    return cur.rowcount > 0


def delete_category(con, cid: int) -> None:
    con.execute("DELETE FROM exp_categories WHERE id=?", (cid,))
    con.commit()


def list_approvers(con, active_only: bool = False):
    q = "SELECT * FROM exp_approvers"
    if active_only:
        q += " WHERE is_active=1"
    q += " ORDER BY name, id"
    return con.execute(q).fetchall()


def add_approver(con, name: str, email: str) -> int:
    cur = con.execute("INSERT INTO exp_approvers (name, email) VALUES (?, ?)",
                      (name.strip(), email.strip()))
    con.commit()
    return cur.lastrowid


def update_approver(con, aid: int, name: str, email: str, is_active: bool) -> bool:
    cur = con.execute(
        "UPDATE exp_approvers SET name=?, email=?, is_active=? WHERE id=?",
        (name.strip(), email.strip(), 1 if is_active else 0, aid))
    con.commit()
    return cur.rowcount > 0


def delete_approver(con, aid: int) -> None:
    con.execute("DELETE FROM exp_approvers WHERE id=?", (aid,))
    con.commit()


def list_accounting(con, active_only: bool = False):
    q = "SELECT * FROM exp_accounting"
    if active_only:
        q += " WHERE is_active=1"
    q += " ORDER BY name, id"
    return con.execute(q).fetchall()


def add_accounting(con, name: str, email: str) -> int:
    cur = con.execute("INSERT INTO exp_accounting (name, email) VALUES (?, ?)",
                      (name.strip(), email.strip()))
    con.commit()
    return cur.lastrowid


def update_accounting(con, aid: int, name: str, email: str, is_active: bool) -> bool:
    cur = con.execute(
        "UPDATE exp_accounting SET name=?, email=?, is_active=? WHERE id=?",
        (name.strip(), email.strip(), 1 if is_active else 0, aid))
    con.commit()
    return cur.rowcount > 0


def delete_accounting(con, aid: int) -> None:
    con.execute("DELETE FROM exp_accounting WHERE id=?", (aid,))
    con.commit()


def list_departments(con, active_only: bool = False):
    q = "SELECT * FROM exp_departments"
    if active_only:
        q += " WHERE is_active=1"
    q += " ORDER BY name, id"
    return con.execute(q).fetchall()


def add_department(con, name: str) -> int:
    cur = con.execute("INSERT INTO exp_departments (name) VALUES (?)", (name.strip(),))
    con.commit()
    return cur.lastrowid


def update_department(con, did: int, name: str, is_active: bool) -> bool:
    cur = con.execute(
        "UPDATE exp_departments SET name=?, is_active=? WHERE id=?",
        (name.strip(), 1 if is_active else 0, did))
    con.commit()
    return cur.rowcount > 0


def delete_department(con, did: int) -> None:
    con.execute("DELETE FROM exp_departments WHERE id=?", (did,))
    con.commit()


# ==================== row -> dict helpers ====================

def _dict(row, *keys) -> dict:
    return {k: row[k] for k in keys}


def settings_snapshot(con) -> dict:
    """Everything the admin settings screen renders, in one call."""
    return {
        "categories": [_dict(r, "id", "name", "sort", "is_active")
                       for r in list_categories(con)],
        "approvers": [_dict(r, "id", "name", "email", "is_active")
                      for r in list_approvers(con)],
        "accounting": [_dict(r, "id", "name", "email", "is_active")
                       for r in list_accounting(con)],
        "departments": [_dict(r, "id", "name", "is_active")
                        for r in list_departments(con)],
        "profiles": [_dict(r, "username", "display_name", "email", "department",
                           "approver_id", "is_active") for r in list_profiles(con)],
    }


# ==================== reports & lines ====================
# A report is one month + one type + one owner. Reimbursement reports carry an
# approver and a status workflow (draft→pending→approved/rejected); credit-card
# summaries are compiled and exported without an approver. Either can be e-mailed
# to a free-typed address once produced.

REIMBURSEMENT, CREDIT = "reimbursement", "credit"
EDITABLE_STATUSES = ("draft", "rejected")   # user may add/edit/delete lines only here


def owner_name(con, username: str) -> str:
    p = get_profile(con, username)
    if p and p["display_name"].strip():
        return p["display_name"].strip()
    return username


def create_report(con, owner: str, rtype: str, month: str, department: str,
                  approver_id, title: str) -> int:
    rtype = CREDIT if rtype == CREDIT else REIMBURSEMENT
    cur = con.execute(
        "INSERT INTO exp_reports (type, owner, title, month, department, approver_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (rtype, (owner or "").strip().lower(), title.strip(), month.strip(),
         department.strip(), approver_id if rtype == REIMBURSEMENT else None),
    )
    con.commit()
    return cur.lastrowid


def get_report(con, rid: int):
    return con.execute("SELECT * FROM exp_reports WHERE id=?", (rid,)).fetchone()


def get_report_by_token(con, token: str):
    if not token:
        return None
    return con.execute("SELECT * FROM exp_reports WHERE approve_token=?",
                       (token,)).fetchone()


def list_reports(con, owner: str):
    return con.execute(
        "SELECT * FROM exp_reports WHERE owner=? ORDER BY created_at DESC, id DESC",
        ((owner or "").strip().lower(),)).fetchall()


def update_report_fields(con, rid: int, month: str, department: str,
                         approver_id, title: str) -> None:
    con.execute(
        "UPDATE exp_reports SET month=?, department=?, approver_id=?, title=? WHERE id=?",
        (month.strip(), department.strip(), approver_id, title.strip(), rid))
    con.commit()


def delete_report(con, rid: int) -> None:
    con.execute("DELETE FROM exp_lines WHERE report_id=?", (rid,))
    con.execute("DELETE FROM exp_reports WHERE id=?", (rid,))
    con.commit()


def get_lines(con, rid: int):
    return con.execute(
        "SELECT * FROM exp_lines WHERE report_id=? ORDER BY seq, id", (rid,)).fetchall()


def _cur(code: str) -> str:
    """Normalise a currency code to a supported one (fallback ILS)."""
    c = (code or "ILS").strip().upper()
    return c if c in config.CURRENCIES else "ILS"


def add_line(con, rid: int, supplier: str = "", amount: float = 0.0,
             category_id=None, invoice_path: str | None = None,
             ocr_raw: str = "", line_date: str = "", note: str = "",
             currency: str = "ILS") -> int:
    nxt = (con.execute("SELECT COALESCE(MAX(seq),0)+1 n FROM exp_lines WHERE report_id=?",
                       (rid,)).fetchone()["n"])
    cur = con.execute(
        "INSERT INTO exp_lines (report_id, seq, supplier, amount, line_date, note, currency, "
        "category_id, invoice_path, ocr_raw) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (rid, nxt, (supplier or "").strip(), float(amount or 0), (line_date or "").strip(),
         (note or "").strip(), _cur(currency), category_id, invoice_path, ocr_raw or ""))
    _recompute_total(con, rid)
    con.commit()
    return cur.lastrowid


def mark_viewed(con, rid: int) -> None:
    """Record the first time the approver opened the request (read-receipt)."""
    con.execute("UPDATE exp_reports SET viewed_at=datetime('now','localtime') "
                "WHERE id=? AND viewed_at IS NULL", (rid,))
    con.commit()


def convert_report_type(con, rid: int, rtype: str, approver_id) -> bool:
    """Switch a report between reimbursement/credit (to fix a wrong choice at creation).
    Resets it to an editable draft, fixes the default title prefix and the approver."""
    report = con.execute("SELECT * FROM exp_reports WHERE id=?", (rid,)).fetchone()
    if report is None:
        return False
    new_type = CREDIT if rtype == CREDIT else REIMBURSEMENT
    old_prefix = "ריכוז אשראי" if report["type"] == CREDIT else "החזר הוצאות"
    new_prefix = "ריכוז אשראי" if new_type == CREDIT else "החזר הוצאות"
    title = (report["title"] or "").strip()
    if title.startswith(old_prefix):                 # only rewrite an auto-generated title
        title = (new_prefix + title[len(old_prefix):]).strip()
    con.execute(
        "UPDATE exp_reports SET type=?, title=?, approver_id=?, status='draft', "
        "decision_note=NULL, decided_at=NULL, submitted_at=NULL, sent_to=NULL, sent_at=NULL "
        "WHERE id=?",
        (new_type, title, approver_id if new_type == REIMBURSEMENT else None, rid))
    con.commit()
    return True


def update_line(con, rid: int, lid: int, supplier: str, amount: float,
                category_id, line_date: str = "", note: str = "",
                currency: str = "ILS") -> bool:
    line = con.execute("SELECT * FROM exp_lines WHERE id=? AND report_id=?",
                       (lid, rid)).fetchone()
    if line is None:
        return False
    con.execute(
        "UPDATE exp_lines SET supplier=?, amount=?, line_date=?, note=?, currency=?, category_id=? WHERE id=?",
        ((supplier or "").strip(), float(amount or 0), (line_date or "").strip(),
         (note or "").strip(), _cur(currency), category_id, lid))
    _recompute_total(con, rid)
    con.commit()
    return True


def delete_line(con, rid: int, lid: int) -> list:
    """Delete a line; returns all its document paths (so the caller can remove files)."""
    line = con.execute("SELECT * FROM exp_lines WHERE id=? AND report_id=?",
                       (lid, rid)).fetchone()
    if line is None:
        return []
    paths = line_all_file_paths(con, lid, line["invoice_path"])
    con.execute("DELETE FROM exp_line_files WHERE line_id=?", (lid,))
    con.execute("DELETE FROM exp_lines WHERE id=?", (lid,))
    _resequence(con, rid)
    _recompute_total(con, rid)
    con.commit()
    return paths


def set_line_image(con, rid: int, lid: int, path: str) -> None:
    con.execute("UPDATE exp_lines SET invoice_path=? WHERE id=? AND report_id=?",
                (path, lid, rid))
    con.commit()


def line_image_path(con, rid: int, lid: int) -> str | None:
    row = con.execute("SELECT invoice_path FROM exp_lines WHERE id=? AND report_id=?",
                      (lid, rid)).fetchone()
    return row["invoice_path"] if row else None


# ---- additional documents per line (beyond the primary invoice_path) ----

def add_line_file(con, lid: int, path: str) -> int:
    cur = con.execute("INSERT INTO exp_line_files (line_id, path) VALUES (?, ?)",
                      (lid, path))
    con.commit()
    return cur.lastrowid


def list_line_files(con, lid: int):
    return con.execute("SELECT * FROM exp_line_files WHERE line_id=? ORDER BY id",
                       (lid,)).fetchall()


def line_file_path(con, lid: int, fid: int) -> str | None:
    row = con.execute("SELECT path FROM exp_line_files WHERE id=? AND line_id=?",
                      (fid, lid)).fetchone()
    return row["path"] if row else None


def delete_line_file(con, lid: int, fid: int) -> str | None:
    row = con.execute("SELECT path FROM exp_line_files WHERE id=? AND line_id=?",
                      (fid, lid)).fetchone()
    if row is None:
        return None
    con.execute("DELETE FROM exp_line_files WHERE id=?", (fid,))
    con.commit()
    return row["path"]


def line_all_file_paths(con, lid: int, invoice_path: str | None) -> list:
    """Every document for a line: the primary invoice first, then the extras."""
    paths = [invoice_path] if invoice_path else []
    paths += [r["path"] for r in list_line_files(con, lid)]
    return paths


def _resequence(con, rid: int) -> None:
    rows = con.execute("SELECT id FROM exp_lines WHERE report_id=? ORDER BY seq, id",
                       (rid,)).fetchall()
    for i, r in enumerate(rows, start=1):
        con.execute("UPDATE exp_lines SET seq=? WHERE id=?", (i, r["id"]))


def _recompute_total(con, rid: int) -> None:
    t = con.execute("SELECT COALESCE(SUM(amount),0) s FROM exp_lines WHERE report_id=?",
                    (rid,)).fetchone()["s"]
    con.execute("UPDATE exp_reports SET total=? WHERE id=?", (float(t or 0), rid))


def set_status(con, rid: int, status: str, note: str | None = None) -> None:
    row = get_report(con, rid)
    sets, vals = ["status=?"], [status]
    if status == "pending" and (row is None or row["submitted_at"] is None):
        sets.append("submitted_at=datetime('now','localtime')")
    if status in ("approved", "rejected"):
        sets.append("decided_at=datetime('now','localtime')")
        sets.append("decision_note=?")
        vals.append(note or "")
    vals.append(rid)
    con.execute(f"UPDATE exp_reports SET {', '.join(sets)} WHERE id=?", vals)
    con.commit()


def ensure_approve_token(con, rid: int) -> str:
    row = get_report(con, rid)
    if row and row["approve_token"]:
        return row["approve_token"]
    tok = _new_token()
    con.execute("UPDATE exp_reports SET approve_token=? WHERE id=?", (tok, rid))
    con.commit()
    return tok


def record_sent(con, rid: int, email: str) -> None:
    con.execute(
        "UPDATE exp_reports SET sent_to=?, sent_at=datetime('now','localtime') WHERE id=?",
        (email.strip(), rid))
    con.commit()


def category_totals(con, rid: int) -> list[tuple[str, float]]:
    rows = con.execute(
        "SELECT COALESCE(c.name,'—') name, SUM(l.amount) amt "
        "FROM exp_lines l LEFT JOIN exp_categories c ON c.id=l.category_id "
        "WHERE l.report_id=? GROUP BY c.id ORDER BY amt DESC", (rid,)).fetchall()
    return [(r["name"], float(r["amt"] or 0)) for r in rows]


def line_dict(con, r) -> dict:
    cat = None
    if r["category_id"] is not None:
        row = con.execute("SELECT name FROM exp_categories WHERE id=?",
                          (r["category_id"],)).fetchone()
        cat = row["name"] if row else None
    extras = [{"id": f["id"]} for f in list_line_files(con, r["id"])]
    cur = _cur(r["currency"] if "currency" in r.keys() else "ILS")
    return {"id": r["id"], "seq": r["seq"], "supplier": r["supplier"],
            "amount": r["amount"], "date": r["line_date"], "note": r["note"],
            "currency": cur, "currency_symbol": config.currency_symbol(cur),
            "category_id": r["category_id"],
            "category": cat, "has_image": bool(r["invoice_path"]),
            "extra_files": extras}


def report_dict(con, r) -> dict:
    approver = None
    if r["approver_id"] is not None:
        approver = con.execute("SELECT name, email FROM exp_approvers WHERE id=?",
                               (r["approver_id"],)).fetchone()
    return {
        "id": r["id"], "type": r["type"], "owner": r["owner"],
        "owner_name": owner_name(con, r["owner"]), "title": r["title"],
        "month": r["month"], "department": r["department"],
        "approver_id": r["approver_id"],
        "approver_name": approver["name"] if approver else None,
        "approver_email": approver["email"] if approver else None,
        "status": r["status"], "decision_note": r["decision_note"],
        "total": float(r["total"] or 0), "sent_to": r["sent_to"], "sent_at": r["sent_at"],
        "created_at": r["created_at"], "submitted_at": r["submitted_at"],
        "decided_at": r["decided_at"], "viewed_at": r["viewed_at"],
        "editable": r["status"] in EDITABLE_STATUSES,
        # a report (and its lines) may be cancelled/deleted until the approver approves it
        "cancellable": r["status"] != "approved",
    }
