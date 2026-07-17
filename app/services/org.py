# -*- coding: utf-8 -*-
"""Org-chart data layer (עץ ארגוני).

A self-propagating organisation tree. Each node is a person/position; a node
flagged ``is_manager`` gets a secret ``token`` that opens a public "fill" page
where that manager adds *only* their own direct reports. The token is the whole
access-control story: holding it lets you edit that node's children and nothing
else, so no login is needed and links can be pasted into WhatsApp.
"""
from __future__ import annotations

import secrets


def _new_token() -> str:
    return secrets.token_urlsafe(16)


def _row(con, node_id):
    return con.execute("SELECT * FROM org_nodes WHERE id=?", (node_id,)).fetchone()


def get_by_token(con, token: str):
    return con.execute("SELECT * FROM org_nodes WHERE token=?", (token,)).fetchone()


def children(con, parent_id: int):
    return con.execute(
        "SELECT * FROM org_nodes WHERE parent_id=? ORDER BY id", (parent_id,)
    ).fetchall()


def roots(con):
    return con.execute(
        "SELECT * FROM org_nodes WHERE parent_id IS NULL ORDER BY id"
    ).fetchall()


def create_root(con, name: str, title: str, phone: str, dept: str) -> int:
    """Create a department head (tree root). Returns the new node id."""
    cur = con.execute(
        "INSERT INTO org_nodes (parent_id, token, dept, name, title, phone, "
        "is_manager, created_by) VALUES (NULL, ?, ?, ?, ?, ?, 1, 'admin')",
        (_new_token(), dept.strip(), name.strip(), title.strip(), phone.strip()),
    )
    con.commit()
    return cur.lastrowid


def add_child(con, parent_id: int, name: str, title: str, phone: str,
              is_manager: bool, created_by: str) -> int:
    """Add a direct report under ``parent_id``. Every node gets its own token so
    toggling ``is_manager`` on later never needs a fresh link."""
    parent = _row(con, parent_id)
    if parent is None:
        raise ValueError("parent not found")
    cur = con.execute(
        "INSERT INTO org_nodes (parent_id, token, dept, name, title, phone, "
        "is_manager, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (parent_id, _new_token(), parent["dept"], name.strip(), title.strip(),
         phone.strip(), 1 if is_manager else 0, (created_by or "").strip()),
    )
    con.commit()
    return cur.lastrowid


def update_child(con, parent_id: int, child_id: int, name: str, title: str,
                 phone: str, is_manager: bool) -> bool:
    """Edit a child — but only if it really is a child of ``parent_id`` (the
    token holder). Returns False if the ownership check fails."""
    child = _row(con, child_id)
    if child is None or child["parent_id"] != parent_id:
        return False
    con.execute(
        "UPDATE org_nodes SET name=?, title=?, phone=?, is_manager=? WHERE id=?",
        (name.strip(), title.strip(), phone.strip(), 1 if is_manager else 0, child_id),
    )
    con.commit()
    return True


def insert_parent(con, node_id: int, name: str, title: str, phone: str,
                  is_manager: bool, created_by: str) -> int | None:
    """Wedge a new role BETWEEN ``node_id`` and its current parent: the new node
    takes the old parent, and ``node_id`` (with its whole subtree) hangs under it.
    Lets you add a supervisory role in the middle of a branch. Returns the new id,
    or None if the node is missing."""
    node = _row(con, node_id)
    if node is None:
        return None
    cur = con.execute(
        "INSERT INTO org_nodes (parent_id, token, dept, name, title, phone, "
        "is_manager, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (node["parent_id"], _new_token(), node["dept"], name.strip(), title.strip(),
         phone.strip(), 1 if is_manager else 0, (created_by or "").strip()),
    )
    new_id = cur.lastrowid
    con.execute("UPDATE org_nodes SET parent_id=? WHERE id=?", (new_id, node_id))
    con.commit()
    return new_id


def update_node(con, node_id: int, name: str, title: str, phone: str,
                is_manager: bool) -> bool:
    """Admin edit of any node by id (no parent-ownership check). Returns False
    if the node is missing."""
    if _row(con, node_id) is None:
        return False
    con.execute(
        "UPDATE org_nodes SET name=?, title=?, phone=?, is_manager=? WHERE id=?",
        (name.strip(), title.strip(), phone.strip(), 1 if is_manager else 0, node_id))
    con.commit()
    return True


def delete_subtree(con, node_id: int, parent_id: int | None = None) -> bool:
    """Delete a node and everything under it. When ``parent_id`` is given the
    node must be its child (guards public/token deletes)."""
    node = _row(con, node_id)
    if node is None:
        return False
    if parent_id is not None and node["parent_id"] != parent_id:
        return False
    # gather the subtree breadth-first, then delete leaves-up
    ids, frontier = [], [node_id]
    while frontier:
        nid = frontier.pop()
        ids.append(nid)
        frontier.extend(r["id"] for r in children(con, nid))
    con.executemany("DELETE FROM org_nodes WHERE id=?", [(i,) for i in reversed(ids)])
    con.commit()
    return True


def mark_filled(con, node_id: int) -> None:
    con.execute(
        "UPDATE org_nodes SET status='filled', filled_at=datetime('now','localtime') "
        "WHERE id=?", (node_id,))
    con.commit()


def reopen(con, node_id: int) -> None:
    con.execute(
        "UPDATE org_nodes SET status='pending', filled_at=NULL WHERE id=?", (node_id,))
    con.commit()


def _node_dict(row) -> dict:
    return {
        "id": row["id"], "parent_id": row["parent_id"], "token": row["token"],
        "dept": row["dept"], "name": row["name"], "title": row["title"],
        "phone": row["phone"], "is_manager": bool(row["is_manager"]),
        "status": row["status"], "created_by": row["created_by"],
        "created_at": row["created_at"], "filled_at": row["filled_at"],
    }


def subtree(con, node_id: int) -> dict:
    """Nested dict for a node and its descendants (admin tree view)."""
    d = _node_dict(_row(con, node_id))
    d["children"] = [subtree(con, r["id"]) for r in children(con, node_id)]
    return d


def full_forest(con) -> list[dict]:
    return [subtree(con, r["id"]) for r in roots(con)]


def stats(con) -> dict:
    total = con.execute("SELECT COUNT(*) c FROM org_nodes").fetchone()["c"]
    managers = con.execute(
        "SELECT COUNT(*) c FROM org_nodes WHERE is_manager=1").fetchone()["c"]
    filled = con.execute(
        "SELECT COUNT(*) c FROM org_nodes WHERE is_manager=1 AND status='filled'"
    ).fetchone()["c"]
    return {"total": total, "managers": managers, "filled": filled,
            "pending": managers - filled}
