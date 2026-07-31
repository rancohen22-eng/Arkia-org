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
                   department: str, is_active: bool = True) -> None:
    u = (username or "").strip().lower()
    if not u:
        raise ValueError("username required")
    con.execute(
        "INSERT INTO exp_profiles (username, display_name, email, department, is_active) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(username) DO UPDATE SET "
        "display_name=excluded.display_name, email=excluded.email, "
        "department=excluded.department, is_active=excluded.is_active",
        (u, display_name.strip(), email.strip(), department.strip(),
         1 if is_active else 0),
    )
    con.commit()


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
        "departments": [_dict(r, "id", "name", "is_active")
                        for r in list_departments(con)],
        "profiles": [_dict(r, "username", "display_name", "email", "department",
                           "is_active") for r in list_profiles(con)],
    }
