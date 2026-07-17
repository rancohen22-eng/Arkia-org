# -*- coding: utf-8 -*-
"""Arkia org-chart system — FastAPI web app (Hebrew RTL).

A self-propagating organisation tree: each manager fills only their own direct
reports, and every manager gets a secret magic-link (shared over WhatsApp) so the
tree builds itself top-down. Admin screens require login; the manager "fill" pages
are reached by token only (no login).
"""
import os
from pathlib import Path
from urllib.parse import quote, unquote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import auth
from .db import connect, init_db
from .services import org

BASE = Path(__file__).resolve().parent
app = FastAPI(title="עץ ארגוני — ארקיע")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")
templates.env.globals["is_admin"] = auth.is_admin

# ---- authentication (session cookie + login gate) ----
# /org/fill/* and /org/api/public/* are reached by managers via a WhatsApp magic
# link with no login — access is gated by the secret token in the URL, not a session.
PUBLIC_PREFIXES = ("/login", "/logout", "/static", "/health", "/favicon",
                   "/org/fill", "/org/api/public")


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if request.session.get("user") or any(path.startswith(p) for p in PUBLIC_PREFIXES):
        return await call_next(request)
    if path.startswith("/api/") or path.startswith("/org/api/"):
        return JSONResponse({"error": "לא מחובר — נא להתחבר מחדש"}, status_code=401)
    return RedirectResponse(f"/login?next={quote(path)}", status_code=303)


IDLE_SECONDS = int(os.environ.get("SESSION_IDLE_SECONDS", str(60 * 60)))
templates.env.globals["idle_seconds"] = IDLE_SECONDS

app.add_middleware(
    SessionMiddleware,
    secret_key=auth.get_secret_key(),
    session_cookie="arkia_org_session",
    same_site="lax",
    https_only=os.environ.get("SESSION_HTTPS_ONLY", "").lower() in ("1", "true", "yes"),
    max_age=IDLE_SECONDS,
)


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/"):
    if request.session.get("user"):
        return RedirectResponse(next or "/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"title": "התחברות", "next": next})


@app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    username = (form.get("username") or "").strip()
    password = form.get("password") or ""
    nxt = form.get("next") or "/"
    if auth.verify(username, password):
        request.session["user"] = username
        return RedirectResponse(nxt, status_code=303)
    return templates.TemplateResponse(
        request, "login.html",
        {"title": "התחברות", "next": nxt, "error": "שם משתמש או סיסמה שגויים"},
        status_code=401)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


def get_user(request: Request) -> str:
    if "session" in request.scope:
        u = request.session.get("user")
        if u:
            return u
    raw = request.headers.get("X-User") or request.cookies.get("user") or "אנונימי"
    return unquote(raw)


def audit(con, user, tbl, row_key, field, old, new):
    con.execute(
        "INSERT INTO audit_log (user, tbl, row_key, field, old_val, new_val) VALUES (?,?,?,?,?,?)",
        (user, tbl, row_key, field, None if old is None else str(old), None if new is None else str(new)))


@app.on_event("startup")
def _startup():
    con = connect()
    init_db(con)
    con.close()


# ==================== עץ ארגוני (org chart) ====================

def _org_public_node(con, token: str):
    """(node, parent) for a token, or (None, None). Non-managers can't be filled."""
    node = org.get_by_token(con, token)
    if node is None or not node["is_manager"]:
        return None, None
    parent = None
    if node["parent_id"] is not None:
        parent = con.execute("SELECT name, title FROM org_nodes WHERE id=?",
                             (node["parent_id"],)).fetchone()
    return node, parent


@app.get("/", response_class=HTMLResponse)
def org_admin_page(request: Request):
    return templates.TemplateResponse(request, "org_admin.html", {"title": "עץ ארגוני"})


@app.get("/org/fill/{token}", response_class=HTMLResponse)
def org_fill_page(request: Request, token: str):
    con = connect()
    node, _ = _org_public_node(con, token)
    con.close()
    if node is None:
        return HTMLResponse(
            "<div dir='rtl' style='font-family:sans-serif;padding:40px;text-align:center'>"
            "<h2>הקישור אינו תקף</h2><p>ייתכן שנמחק או שהוקלד שגוי. פנה למי ששלח לך אותו.</p></div>",
            status_code=404)
    return templates.TemplateResponse(request, "org_fill.html",
                                      {"title": "מילוי עץ ארגוני", "token": token})


@app.get("/org/export.html")
def org_export_html(request: Request):
    """Standalone, self-contained HTML snapshot of the tree, for sharing."""
    from .services.org_export import render_html
    con = connect()
    forest = org.full_forest(con)
    dept = forest[0]["dept"] if forest else ""
    con.close()
    doc = render_html(forest, dept=dept)
    return HTMLResponse(doc, headers={
        "Content-Disposition": 'attachment; filename="arkia-org-chart.html"'})


# ---- admin API (login required) ----

@app.get("/org/api/tree")
def org_api_tree(request: Request):
    con = connect()
    forest = org.full_forest(con)
    st = org.stats(con)
    con.close()
    return {"forest": forest, "stats": st}


@app.post("/org/api/root")
async def org_api_root(request: Request):
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "חסר שם"}, status_code=400)
    con = connect()
    user = get_user(request)
    nid = org.create_root(con, name, body.get("title", ""), body.get("phone", ""),
                          body.get("dept", "finance"))
    audit(con, user, "org_nodes", str(nid), "create_root", None, name)
    con.commit()
    con.close()
    return {"id": nid}


@app.post("/org/api/node/{node_id}/child")
async def org_api_admin_add(node_id: int, request: Request):
    """Admin adds a direct report to ANY node (build/expand a branch yourself)."""
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "חסר שם"}, status_code=400)
    con = connect()
    if con.execute("SELECT 1 FROM org_nodes WHERE id=?", (node_id,)).fetchone() is None:
        con.close()
        return JSONResponse({"error": "צומת לא נמצא"}, status_code=404)
    user = get_user(request)
    cid = org.add_child(con, node_id, name, body.get("title", ""),
                        body.get("phone", ""), bool(body.get("is_manager")),
                        created_by=user)
    audit(con, user, "org_nodes", str(cid), "admin_add", None, name)
    con.commit()
    con.close()
    return {"id": cid}


@app.post("/org/api/node/{node_id}/insert-parent")
async def org_api_admin_insert_parent(node_id: int, request: Request):
    """Admin inserts a role BETWEEN a node and its parent (middle-of-branch)."""
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "חסר שם"}, status_code=400)
    con = connect()
    user = get_user(request)
    new_id = org.insert_parent(con, node_id, name, body.get("title", ""),
                               body.get("phone", ""),
                               bool(body.get("is_manager", True)), created_by=user)
    if new_id is None:
        con.close()
        return JSONResponse({"error": "צומת לא נמצא"}, status_code=404)
    audit(con, user, "org_nodes", str(new_id), "insert_parent", None, name)
    con.commit()
    con.close()
    return {"id": new_id}


@app.post("/org/api/node/{node_id}/update")
async def org_api_admin_update(node_id: int, request: Request):
    """Admin edits any node (name / title / manager flag / phone)."""
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "חסר שם"}, status_code=400)
    con = connect()
    ok = org.update_node(con, node_id, name, body.get("title", ""),
                         body.get("phone", ""), bool(body.get("is_manager")))
    con.close()
    if not ok:
        return JSONResponse({"error": "צומת לא נמצא"}, status_code=404)
    return {"ok": True}


@app.post("/org/api/node/{node_id}/reopen")
async def org_api_reopen(node_id: int, request: Request):
    con = connect()
    org.reopen(con, node_id)
    con.close()
    return {"ok": True}


@app.post("/org/api/node/{node_id}/delete")
async def org_api_admin_delete(node_id: int, request: Request):
    con = connect()
    ok = org.delete_subtree(con, node_id)   # admin: no parent guard, may remove a root
    audit(con, get_user(request), "org_nodes", str(node_id), "delete", None, None)
    con.commit()
    con.close()
    return {"ok": ok}


# ---- public API (token-gated, no login) ----

@app.get("/org/api/public/node/{token}")
def org_api_public_node(token: str):
    con = connect()
    node, parent = _org_public_node(con, token)
    if node is None:
        con.close()
        return JSONResponse({"error": "קישור לא תקף"}, status_code=404)
    kids = [org._node_dict(r) for r in org.children(con, node["id"])]
    con.close()
    return {
        "me": {"name": node["name"], "title": node["title"], "dept": node["dept"],
               "status": node["status"]},
        "appointed_by": ({"name": parent["name"], "title": parent["title"]}
                         if parent else None),
        "children": kids,
    }


@app.post("/org/api/public/node/{token}/child")
async def org_api_public_add(token: str, request: Request):
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "חסר שם"}, status_code=400)
    con = connect()
    node, _ = _org_public_node(con, token)
    if node is None:
        con.close()
        return JSONResponse({"error": "קישור לא תקף"}, status_code=404)
    cid = org.add_child(con, node["id"], name, body.get("title", ""),
                        body.get("phone", ""), bool(body.get("is_manager")),
                        created_by=node["name"])
    row = con.execute("SELECT * FROM org_nodes WHERE id=?", (cid,)).fetchone()
    result = org._node_dict(row)
    con.close()
    return result


@app.post("/org/api/public/node/{token}/child/{child_id}")
async def org_api_public_update(token: str, child_id: int, request: Request):
    body = await request.json()
    con = connect()
    node, _ = _org_public_node(con, token)
    if node is None:
        con.close()
        return JSONResponse({"error": "קישור לא תקף"}, status_code=404)
    ok = org.update_child(con, node["id"], child_id, (body.get("name") or "").strip(),
                          body.get("title", ""), body.get("phone", ""),
                          bool(body.get("is_manager")))
    row = con.execute("SELECT * FROM org_nodes WHERE id=?", (child_id,)).fetchone()
    result = org._node_dict(row) if (ok and row) else None
    con.close()
    if not ok:
        return JSONResponse({"error": "לא נמצא / אינו כפוף לך"}, status_code=403)
    return result


@app.post("/org/api/public/node/{token}/child/{child_id}/delete")
async def org_api_public_delete(token: str, child_id: int, request: Request):
    con = connect()
    node, _ = _org_public_node(con, token)
    if node is None:
        con.close()
        return JSONResponse({"error": "קישור לא תקף"}, status_code=404)
    ok = org.delete_subtree(con, child_id, parent_id=node["id"])
    con.close()
    if not ok:
        return JSONResponse({"error": "לא נמצא / אינו כפוף לך"}, status_code=403)
    return {"ok": True}


@app.post("/org/api/public/node/{token}/submit")
def org_api_public_submit(token: str):
    con = connect()
    node, _ = _org_public_node(con, token)
    if node is None:
        con.close()
        return JSONResponse({"error": "קישור לא תקף"}, status_code=404)
    org.mark_filled(con, node["id"])
    con.close()
    return {"ok": True}
