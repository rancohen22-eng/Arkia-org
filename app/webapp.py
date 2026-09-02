# -*- coding: utf-8 -*-
"""Shared web-app factory for the Arkia suite.

Two ASGI entrypoints are built from this one module:

  * ``app.main:app``    — org chart **and** expenses (the full internal app).
  * ``app.exp_app:app`` — expenses **only**: a standalone service with no org
    tree, meant to run as its own systemd service on its own port/DB/users.

Both share login, session handling, templates and the SQLite layer; ``create_app``
decides which route groups are mounted. The DB file, session cookie and landing
page differ per entrypoint so the two can run side-by-side without interfering.
"""
import mimetypes
import os
from pathlib import Path
from urllib.parse import quote, unquote

# so StaticFiles serves the PWA manifest with the correct Content-Type
mimetypes.add_type("application/manifest+json", ".webmanifest")

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import auth, config
from .db import connect, init_db
from .services import org
from .services import expense
from .services import ocr as ocr_service
from .services import mailer, pdf, fx
from .services import passkey

BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=BASE / "templates")
templates.env.globals["is_admin"] = auth.is_admin
templates.env.globals["passkey_enabled"] = passkey.available

# /org/fill/* and /org/api/public/* are reached by managers via a WhatsApp magic
# link with no login — access is gated by the secret token in the URL, not a session.
# The list is shared by both entrypoints; prefixes for routes that aren't mounted
# in a given app are simply never hit.
PUBLIC_PREFIXES = ("/login", "/logout", "/static", "/health", "/favicon",
                   "/org/fill", "/org/api/public",
                   "/exp/approve", "/exp/api/public", "/exp/track",
                   "/auth/webauthn/login")   # passkey login is pre-auth; register isn't

# while a user still holds a one-time password, only these are reachable
CHANGE_PW_ALLOWED = ("/change-password", "/api/change-password",
                     "/logout", "/static", "/health", "/favicon")

IDLE_SECONDS = int(os.environ.get("SESSION_IDLE_SECONDS", str(60 * 60)))
templates.env.globals["idle_seconds"] = IDLE_SECONDS


# ==================== shared helpers ====================

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


def _forbidden_admin(request: Request) -> JSONResponse | None:
    """None if the caller is an admin, else a 403 JSON body."""
    if auth.is_admin(get_user(request)):
        return None
    return JSONResponse({"error": "פעולה זו מותרת למנהל מערכת בלבד"}, status_code=403)


def _owns(request: Request, report) -> bool:
    if report is None:
        return False
    user = get_user(request)
    return report["owner"] == (user or "").strip().lower() or auth.is_admin(user)


def _load_report_for_pdf(con, report) -> tuple[dict, list[dict], list]:
    """Assemble the report dict, lines (with invoice bytes read from disk) and
    per-category subtotals — the shape both the PDF and the e-mail builders want."""
    rep = expense.report_dict(con, report)
    lines = []
    for r in expense.get_lines(con, report["id"]):
        d = expense.line_dict(con, r)
        blobs = []
        for pth in expense.line_all_file_paths(con, r["id"], r["invoice_path"]):
            p = Path(pth)
            if p.exists():
                blobs.append(p.read_bytes())
        d["invoice_bytes"] = blobs[0] if blobs else None   # legacy single field
        d["invoice_bytes_list"] = blobs
        lines.append(d)
    return rep, lines, expense.category_totals(con, report["id"])


def _payment_recipients(con, rep: dict, prof) -> list:
    """Who gets the 'ready for payment' e-mail: the employee, the approver, and
    every active accounting-dept address — de-duplicated, empties dropped."""
    raw = []
    if prof and prof["email"]:
        raw.append(prof["email"])
    if rep.get("approver_email"):
        raw.append(rep["approver_email"])
    raw += [a["email"] for a in expense.list_accounting(con, active_only=True)]
    seen, out = set(), []
    for e in raw:
        e = (e or "").strip()
        if e and e.lower() not in seen:
            seen.add(e.lower()); out.append(e)
    return out


def _build_pdf(con, report) -> bytes:
    rep, lines, cats = _load_report_for_pdf(con, report)
    conversion = fx.convert_lines(con, lines)   # ILS-converted summary (by expense date)
    return pdf.build_report_pdf(rep, lines, cats, conversion=conversion)


def _mail_lines(lines: list[dict]) -> list[dict]:
    return [{"seq": l["seq"], "supplier": l["supplier"], "amount": l["amount"],
             "category": l.get("category")} for l in lines]


def _img_ext(content_type: str | None, filename: str | None) -> str:
    ct = (content_type or "").lower()
    if "png" in ct:
        return ".png"
    if "heic" in ct or "heif" in ct:
        return ".heic"
    if filename and "." in filename:
        return "." + filename.rsplit(".", 1)[1].lower()[:5]
    return ".jpg"


def _media_for(path: str) -> str:
    p = path.lower()
    if p.endswith(".png"):
        return "image/png"
    if p.endswith((".heic", ".heif")):
        return "image/heic"
    return "image/jpeg"


def _pdf_name(rep: dict) -> str:
    safe = "".join(c if c.isalnum() or c in " _-" else "_" for c in rep["title"])
    return f"{safe or 'report'}.pdf"


def _pdf_headers(rep: dict, inline: bool = True) -> dict:
    """Content-Disposition that survives latin-1 HTTP headers: an ASCII fallback
    plus an RFC 5987 UTF-8 name so the browser still shows the Hebrew title."""
    name = _pdf_name(rep)
    ascii_name = name.encode("ascii", "ignore").decode().strip() or "report.pdf"
    disp = "inline" if inline else "attachment"
    return {"Content-Disposition":
            f"{disp}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"}


def _passkey_off():
    return JSONResponse({"error": "התחברות ב-Passkey אינה מופעלת"}, status_code=404)


# ==================== common routes (login gate, always mounted) ====================

def _register_common(app: FastAPI) -> None:

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, next: str = ""):
        home = request.app.state.home_path
        nxt = next or home
        if request.session.get("user"):
            return RedirectResponse(nxt or "/", status_code=303)
        return templates.TemplateResponse(request, "login.html", {"title": "התחברות", "next": nxt})

    @app.post("/login")
    async def login_submit(request: Request):
        form = await request.form()
        username = (form.get("username") or "").strip()
        password = form.get("password") or ""
        nxt = form.get("next") or request.app.state.home_path
        # env/file users (admin etc.) first; then in-app accounts (exp_users)
        if auth.verify(username, password):
            request.session["user"] = username
            request.session.pop("must_change", None)
            return RedirectResponse(nxt, status_code=303)
        con = connect()
        try:
            if expense.verify_db_user(con, username, password):
                request.session["user"] = username.strip().lower()
                if expense.user_must_change(con, username):
                    request.session["must_change"] = True
                    return RedirectResponse("/change-password", status_code=303)
                request.session.pop("must_change", None)
                return RedirectResponse(nxt, status_code=303)
        finally:
            con.close()
        return templates.TemplateResponse(
            request, "login.html",
            {"title": "התחברות", "next": nxt, "error": "שם משתמש או סיסמה שגויים"},
            status_code=401)

    @app.get("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.get("/change-password", response_class=HTMLResponse)
    def change_password_page(request: Request):
        if not request.session.get("user"):
            return RedirectResponse("/login?next=/change-password", status_code=303)
        return templates.TemplateResponse(
            request, "change_password.html",
            {"title": "שינוי סיסמה", "forced": bool(request.session.get("must_change"))})

    @app.post("/api/change-password")
    async def change_password_submit(request: Request):
        if not request.session.get("user"):
            return JSONResponse({"error": "לא מחובר"}, status_code=401)
        body = await request.json()
        new = body.get("new_password") or ""
        confirm = body.get("confirm") or ""
        current = body.get("current_password") or ""
        forced = bool(request.session.get("must_change"))
        if len(new) < 8:
            return JSONResponse({"error": "הסיסמה חייבת להכיל לפחות 8 תווים"}, status_code=400)
        if new != confirm:
            return JSONResponse({"error": "אימות הסיסמה אינו תואם"}, status_code=400)
        u = (get_user(request) or "").strip().lower()
        con = connect()
        try:
            # a voluntary change (not the forced first-login one) re-checks the current password
            if not forced and not (auth.verify(u, current) or expense.verify_db_user(con, u, current)):
                return JSONResponse({"error": "הסיסמה הנוכחית שגויה"}, status_code=403)
            expense.set_user_password(con, u, new, must_change=False)
        finally:
            con.close()
        request.session.pop("must_change", None)
        return {"ok": True}


# ==================== עץ ארגוני (org chart) ====================

def _register_org(app: FastAPI) -> None:

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


# ==================== החזרי הוצאות / ריכוז אשראי (expense) ====================

def _register_exp(app: FastAPI) -> None:

    # ---- settings (admin-only) ----

    @app.get("/exp/admin", response_class=HTMLResponse)
    def exp_admin_page(request: Request):
        if not auth.is_admin(get_user(request)):
            return HTMLResponse(
                "<div dir='rtl' style='font-family:sans-serif;padding:40px;text-align:center'>"
                "<h2>אין הרשאה</h2><p>מסך ההגדרות פתוח למנהל מערכת בלבד.</p>"
                "<p><a href='/exp'>חזרה</a></p></div>", status_code=403)
        return templates.TemplateResponse(request, "expense_admin.html",
                                          {"title": "הגדרות החזרי הוצאות"})

    @app.get("/exp/api/settings")
    def exp_api_settings(request: Request):
        if (err := _forbidden_admin(request)):
            return err
        con = connect()
        data = expense.settings_snapshot(con)
        con.close()
        return data

    # ---- categories ----

    @app.post("/exp/api/settings/category")
    async def exp_api_category_add(request: Request):
        if (err := _forbidden_admin(request)):
            return err
        body = await request.json()
        name = (body.get("name") or "").strip()
        if not name:
            return JSONResponse({"error": "חסר שם סיווג"}, status_code=400)
        con = connect()
        cid = expense.add_category(con, name, int(body.get("sort") or 0))
        audit(con, get_user(request), "exp_categories", str(cid), "add", None, name)
        con.commit()
        con.close()
        return {"id": cid}

    @app.post("/exp/api/settings/category/{cid}")
    async def exp_api_category_update(cid: int, request: Request):
        if (err := _forbidden_admin(request)):
            return err
        body = await request.json()
        name = (body.get("name") or "").strip()
        if not name:
            return JSONResponse({"error": "חסר שם סיווג"}, status_code=400)
        con = connect()
        ok = expense.update_category(con, cid, name, int(body.get("sort") or 0),
                                     bool(body.get("is_active", True)))
        con.close()
        if not ok:
            return JSONResponse({"error": "סיווג לא נמצא"}, status_code=404)
        return {"ok": True}

    @app.post("/exp/api/settings/category/{cid}/delete")
    def exp_api_category_delete(cid: int, request: Request):
        if (err := _forbidden_admin(request)):
            return err
        con = connect()
        expense.delete_category(con, cid)
        con.close()
        return {"ok": True}

    # ---- approvers ----

    @app.post("/exp/api/settings/approver")
    async def exp_api_approver_add(request: Request):
        if (err := _forbidden_admin(request)):
            return err
        body = await request.json()
        name = (body.get("name") or "").strip()
        email = (body.get("email") or "").strip()
        if not name or not email:
            return JSONResponse({"error": "חסר שם או מייל מאשר"}, status_code=400)
        con = connect()
        aid = expense.add_approver(con, name, email)
        audit(con, get_user(request), "exp_approvers", str(aid), "add", None, email)
        con.commit()
        con.close()
        return {"id": aid}

    @app.post("/exp/api/settings/approver/{aid}")
    async def exp_api_approver_update(aid: int, request: Request):
        if (err := _forbidden_admin(request)):
            return err
        body = await request.json()
        name = (body.get("name") or "").strip()
        email = (body.get("email") or "").strip()
        if not name or not email:
            return JSONResponse({"error": "חסר שם או מייל מאשר"}, status_code=400)
        con = connect()
        ok = expense.update_approver(con, aid, name, email,
                                     bool(body.get("is_active", True)))
        con.close()
        if not ok:
            return JSONResponse({"error": "מאשר לא נמצא"}, status_code=404)
        return {"ok": True}

    @app.post("/exp/api/settings/approver/{aid}/delete")
    def exp_api_approver_delete(aid: int, request: Request):
        if (err := _forbidden_admin(request)):
            return err
        con = connect()
        expense.delete_approver(con, aid)
        con.close()
        return {"ok": True}

    # ---- accounting dept (CC'd on the payment e-mail) ----

    @app.post("/exp/api/settings/accounting")
    async def exp_api_accounting_add(request: Request):
        if (err := _forbidden_admin(request)):
            return err
        body = await request.json()
        name = (body.get("name") or "").strip()
        email = (body.get("email") or "").strip()
        if not name or not email:
            return JSONResponse({"error": "חסר שם או מייל"}, status_code=400)
        con = connect()
        aid = expense.add_accounting(con, name, email)
        audit(con, get_user(request), "exp_accounting", str(aid), "add", None, email)
        con.commit()
        con.close()
        return {"id": aid}

    @app.post("/exp/api/settings/accounting/{aid}")
    async def exp_api_accounting_update(aid: int, request: Request):
        if (err := _forbidden_admin(request)):
            return err
        body = await request.json()
        name = (body.get("name") or "").strip()
        email = (body.get("email") or "").strip()
        if not name or not email:
            return JSONResponse({"error": "חסר שם או מייל"}, status_code=400)
        con = connect()
        ok = expense.update_accounting(con, aid, name, email,
                                       bool(body.get("is_active", True)))
        con.close()
        if not ok:
            return JSONResponse({"error": "לא נמצא"}, status_code=404)
        return {"ok": True}

    @app.post("/exp/api/settings/accounting/{aid}/delete")
    def exp_api_accounting_delete(aid: int, request: Request):
        if (err := _forbidden_admin(request)):
            return err
        con = connect()
        expense.delete_accounting(con, aid)
        con.close()
        return {"ok": True}

    # ---- departments ----

    @app.post("/exp/api/settings/department")
    async def exp_api_department_add(request: Request):
        if (err := _forbidden_admin(request)):
            return err
        body = await request.json()
        name = (body.get("name") or "").strip()
        if not name:
            return JSONResponse({"error": "חסר שם מחלקה"}, status_code=400)
        con = connect()
        did = expense.add_department(con, name)
        con.close()
        return {"id": did}

    @app.post("/exp/api/settings/department/{did}")
    async def exp_api_department_update(did: int, request: Request):
        if (err := _forbidden_admin(request)):
            return err
        body = await request.json()
        name = (body.get("name") or "").strip()
        if not name:
            return JSONResponse({"error": "חסר שם מחלקה"}, status_code=400)
        con = connect()
        ok = expense.update_department(con, did, name, bool(body.get("is_active", True)))
        con.close()
        if not ok:
            return JSONResponse({"error": "מחלקה לא נמצאה"}, status_code=404)
        return {"ok": True}

    @app.post("/exp/api/settings/department/{did}/delete")
    def exp_api_department_delete(did: int, request: Request):
        if (err := _forbidden_admin(request)):
            return err
        con = connect()
        expense.delete_department(con, did)
        con.close()
        return {"ok": True}

    # ---- user profiles (username -> display name / email / department) ----

    @app.post("/exp/api/settings/profile")
    async def exp_api_profile_upsert(request: Request):
        if (err := _forbidden_admin(request)):
            return err
        body = await request.json()
        username = (body.get("username") or "").strip()
        if not username:
            return JSONResponse({"error": "חסר שם משתמש"}, status_code=400)
        con = connect()
        expense.upsert_profile(con, username, body.get("display_name", ""),
                               body.get("email", ""), body.get("department", ""),
                               bool(body.get("is_active", True)),
                               approver_id=body.get("approver_id") or None)
        con.close()
        return {"ok": True}

    @app.post("/exp/api/settings/profile/{username}/delete")
    def exp_api_profile_delete(username: str, request: Request):
        if (err := _forbidden_admin(request)):
            return err
        con = connect()
        expense.delete_profile(con, username)
        con.close()
        return {"ok": True}

    @app.post("/exp/api/settings/profile/{username}/invite")
    def exp_api_profile_invite(username: str, request: Request):
        """Generate a one-time password for the user, e-mail it (welcome + login
        link), and force a change on first login. Returns the password too, so the
        admin can relay it when no e-mail is on file / the mail didn't go out."""
        if (err := _forbidden_admin(request)):
            return err
        con = connect()
        prof = expense.get_profile(con, username)
        if prof is None:
            con.close()
            return JSONResponse({"error": "המשתמש לא נמצא — יש להוסיף אותו קודם"}, status_code=404)
        temp = auth.random_password()
        expense.set_user_password(con, username, temp, must_change=True)
        email = (prof["email"] or "").strip()
        display = (prof["display_name"] or "").strip()
        audit(con, get_user(request), "exp_users", (username or "").strip().lower(),
              "invite", None, "sent" if email else "manual")
        con.commit()
        con.close()
        emailed, err_msg = False, None
        if email:
            login_url = (config.APP_BASE_URL + "/login") if config.APP_BASE_URL else ""
            res = mailer.send_mail(
                email, "פרטי התחברות — מערכת החזרי הוצאות של ארקיע",
                mailer.welcome_html(display, (username or "").strip().lower(), temp, login_url))
            emailed = bool(res.get("sent"))
            err_msg = res.get("error")
        return {"ok": True, "emailed": emailed, "email": email,
                "password": temp, "error": err_msg}

    # ==================== expense: employee report flow ====================

    @app.get("/exp", response_class=HTMLResponse)
    def exp_home_page(request: Request):
        return templates.TemplateResponse(request, "exp_home.html",
                                          {"title": "החזרי הוצאות"})

    @app.get("/exp/api/home")
    def exp_api_home(request: Request):
        con = connect()
        user = get_user(request)
        prof = expense.get_profile(con, user)
        reports = [expense.report_dict(con, r) for r in expense.list_reports(con, user)]
        approver_name = None
        if prof and prof["approver_id"] is not None:
            a = con.execute("SELECT name FROM exp_approvers WHERE id=?",
                            (prof["approver_id"],)).fetchone()
            approver_name = a["name"] if a else None
        data = {
            "user": user,
            "profile": {"display_name": (prof["display_name"] if prof else ""),
                        "department": (prof["department"] if prof else ""),
                        "email": (prof["email"] if prof else ""),
                        "approver_name": approver_name},
            "reports": reports,
            "departments": [r["name"] for r in expense.list_departments(con, active_only=True)],
        }
        con.close()
        return data

    # ---- "awaiting my signature": reports a logged-in approver needs to sign ----

    @app.get("/exp/approvals", response_class=HTMLResponse)
    def exp_approvals_page(request: Request):
        return templates.TemplateResponse(request, "exp_approvals.html",
                                          {"title": "ממתין לחתימתי"})

    @app.get("/exp/api/my-approvals")
    def exp_api_my_approvals(request: Request):
        con = connect()
        user = get_user(request)
        prof = expense.get_profile(con, user)
        email = (prof["email"] if prof else "") or ""
        items = []
        for r in expense.reports_pending_approval_for_email(con, email):
            rep = expense.report_dict(con, r)
            items.append({
                "id": rep["id"], "title": rep["title"], "owner_name": rep["owner_name"],
                "department": rep["department"], "month": rep["month"],
                "total": rep["total"], "submitted_at": rep["submitted_at"],
                "viewed_at": rep["viewed_at"],
                "approve_url": f"/exp/approve/{r['approve_token']}" if r["approve_token"] else None,
            })
        con.close()
        return {"email": email, "items": items}

    @app.post("/exp/api/report")
    async def exp_api_report_create(request: Request):
        body = await request.json()
        rtype = body.get("type") or expense.REIMBURSEMENT
        month = (body.get("month") or "").strip()
        if not month:
            return JSONResponse({"error": "יש לבחור חודש"}, status_code=400)
        con = connect()
        user = get_user(request)
        prof = expense.get_profile(con, user)
        dept = (body.get("department") or (prof["department"] if prof else "") or "").strip()
        default_title = ("ריכוז אשראי" if rtype == expense.CREDIT else "החזר הוצאות") + f" — {month}"
        title = (body.get("title") or default_title).strip()
        # The approver is assigned to the user by the admin (profile), not chosen here.
        approver_id = prof["approver_id"] if prof else None
        rid = expense.create_report(con, user, rtype, month, dept, approver_id, title)
        audit(con, user, "exp_reports", str(rid), "create", None, rtype)
        con.commit()
        con.close()
        return {"id": rid}

    @app.post("/exp/api/report/{rid}/convert")
    async def exp_api_report_convert(rid: int, request: Request):
        body = await request.json()
        con = connect()
        report = expense.get_report(con, rid)
        if not _owns(request, report):
            con.close()
            return JSONResponse({"error": "אין הרשאה"}, status_code=403)
        user = get_user(request)
        if report["status"] == "approved" and not auth.is_admin(user):
            con.close()
            return JSONResponse({"error": "לא ניתן להמיר דוח שכבר אושר"}, status_code=409)
        prof = expense.get_profile(con, user)
        approver_id = prof["approver_id"] if prof else None
        expense.convert_report_type(con, rid, body.get("type"), approver_id)
        audit(con, user, "exp_reports", str(rid), "convert", report["type"],
              body.get("type"))
        con.commit()
        con.close()
        return {"ok": True}

    @app.get("/exp/report/{rid}", response_class=HTMLResponse)
    def exp_report_page(request: Request, rid: int):
        con = connect()
        report = expense.get_report(con, rid)
        ok = _owns(request, report)
        con.close()
        if report is None or not ok:
            return HTMLResponse(
                "<div dir='rtl' style='font-family:sans-serif;padding:40px;text-align:center'>"
                "<h2>הדוח לא נמצא</h2><p>ייתכן שנמחק, או שאינו שייך לך.</p>"
                "<p><a href='/exp'>חזרה</a></p></div>", status_code=404)
        return templates.TemplateResponse(request, "exp_report.html",
                                          {"title": "עריכת דוח", "rid": rid})

    @app.get("/exp/api/report/{rid}")
    def exp_api_report_get(rid: int, request: Request):
        con = connect()
        report = expense.get_report(con, rid)
        if not _owns(request, report):
            con.close()
            return JSONResponse({"error": "אין הרשאה"}, status_code=403)
        data = {
            "report": expense.report_dict(con, report),
            "lines": [expense.line_dict(con, r) for r in expense.get_lines(con, rid)],
            "categories": [expense._dict(r, "id", "name")
                           for r in expense.list_categories(con, active_only=True)],
            "approvers": [expense._dict(r, "id", "name", "email")
                          for r in expense.list_approvers(con, active_only=True)],
            "departments": [r["name"] for r in expense.list_departments(con, active_only=True)],
            "ocr_enabled": config.ocr_configured(),
        }
        # other open (editable) reports of this owner — targets for moving a line
        if report["status"] in expense.EDITABLE_STATUSES:
            data["move_targets"] = [
                {"id": t["id"], "title": t["title"], "month": t["month"], "type": t["type"]}
                for t in expense.list_reports(con, report["owner"])
                if t["id"] != rid and t["status"] in expense.EDITABLE_STATUSES]
        # the owner may share the approver's magic-link directly (e.g. via WhatsApp),
        # useful when e-mail to the corporate domain is filtered
        if report["type"] == expense.REIMBURSEMENT and report["approve_token"]:
            base = config.APP_BASE_URL or str(request.base_url).rstrip("/")
            data["approve_url"] = f"{base}/exp/approve/{report['approve_token']}"
        con.close()
        return data

    @app.post("/exp/api/report/{rid}/update")
    async def exp_api_report_update(rid: int, request: Request):
        body = await request.json()
        con = connect()
        report = expense.get_report(con, rid)
        if not _owns(request, report):
            con.close()
            return JSONResponse({"error": "אין הרשאה"}, status_code=403)
        if report["status"] not in expense.EDITABLE_STATUSES:
            con.close()
            return JSONResponse({"error": "לא ניתן לערוך דוח בסטטוס זה"}, status_code=409)
        # approver is admin-assigned (via the user's profile), never changed here
        expense.update_report_fields(
            con, rid, body.get("month", report["month"]),
            body.get("department", report["department"]),
            report["approver_id"],
            body.get("title", report["title"]))
        con.close()
        return {"ok": True}

    @app.post("/exp/api/report/{rid}/line")
    async def exp_api_line_add(rid: int, request: Request,
                               file: UploadFile | None = File(default=None)):
        con = connect()
        report = expense.get_report(con, rid)
        if not _owns(request, report):
            con.close()
            return JSONResponse({"error": "אין הרשאה"}, status_code=403)
        if report["status"] not in expense.EDITABLE_STATUSES:
            con.close()
            return JSONResponse({"error": "לא ניתן להוסיף שורות לדוח בסטטוס זה"}, status_code=409)

        # every expense line MUST have an invoice document attached
        blob = await file.read() if file is not None else b""
        if not blob:
            con.close()
            return JSONResponse(
                {"error": "ללא מסמך לא ניתן להוסיף הוצאה — יש לצרף צילום או קובץ של החשבונית"},
                status_code=400)
        res = ocr_service.extract_invoice(blob)
        supplier, amount, raw, ocr_engine = (res.supplier, res.amount or 0.0,
                                             res.raw, res.engine)

        lid = expense.add_line(con, rid, supplier=supplier, amount=amount,
                               ocr_raw=raw)
        ext = _img_ext(file.content_type, file.filename)
        folder = config.UPLOADS / str(rid)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{lid}{ext}"
        path.write_bytes(blob)
        expense.set_line_image(con, rid, lid, str(path))

        row = con.execute("SELECT * FROM exp_lines WHERE id=?", (lid,)).fetchone()
        line = expense.line_dict(con, row)
        total = expense.report_dict(con, expense.get_report(con, rid))["total"]
        con.close()
        return {"line": line, "ocr": {"supplier": supplier, "amount": amount,
                                      "engine": ocr_engine}, "total": total}

    @app.post("/exp/api/report/{rid}/line/{lid}")
    async def exp_api_line_update(rid: int, lid: int, request: Request):
        body = await request.json()
        con = connect()
        report = expense.get_report(con, rid)
        if not _owns(request, report):
            con.close()
            return JSONResponse({"error": "אין הרשאה"}, status_code=403)
        if report["status"] not in expense.EDITABLE_STATUSES:
            con.close()
            return JSONResponse({"error": "לא ניתן לערוך דוח בסטטוס זה"}, status_code=409)
        ok = expense.update_line(con, rid, lid, body.get("supplier", ""),
                                 body.get("amount", 0), body.get("category_id") or None,
                                 line_date=body.get("date", ""), note=body.get("note", ""),
                                 currency=body.get("currency", "ILS"))
        total = expense.report_dict(con, expense.get_report(con, rid))["total"]
        con.close()
        if not ok:
            return JSONResponse({"error": "שורה לא נמצאה"}, status_code=404)
        return {"ok": True, "total": total}

    @app.post("/exp/api/report/{rid}/line/{lid}/delete")
    def exp_api_line_delete(rid: int, lid: int, request: Request):
        con = connect()
        report = expense.get_report(con, rid)
        if not _owns(request, report):
            con.close()
            return JSONResponse({"error": "אין הרשאה"}, status_code=403)
        # a line may be cancelled until the report is approved (even while pending)
        if report["status"] == "approved" and not auth.is_admin(get_user(request)):
            con.close()
            return JSONResponse({"error": "לא ניתן לבטל שורה בדוח שכבר אושר"}, status_code=409)
        paths = expense.delete_line(con, rid, lid)
        total = expense.report_dict(con, expense.get_report(con, rid))["total"]
        con.close()
        for path in paths:
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                pass
        return {"ok": True, "total": total}

    @app.post("/exp/api/report/{rid}/line/{lid}/move")
    async def exp_api_line_move(rid: int, lid: int, request: Request):
        """Move a line to another open report of the same owner (both must be
        editable — a closed/approved report can't give or receive lines)."""
        body = await request.json()
        try:
            target_id = int(body.get("target_report_id"))
        except (TypeError, ValueError):
            return JSONResponse({"error": "יעד לא תקין"}, status_code=400)
        con = connect()
        source = expense.get_report(con, rid)
        target = expense.get_report(con, target_id)
        if not _owns(request, source) or not _owns(request, target):
            con.close()
            return JSONResponse({"error": "אין הרשאה"}, status_code=403)
        if target["owner"] != source["owner"]:
            con.close()
            return JSONResponse({"error": "ניתן להעביר רק בין דוחות של אותו עובד"}, status_code=403)
        if source["status"] not in expense.EDITABLE_STATUSES or \
           target["status"] not in expense.EDITABLE_STATUSES:
            con.close()
            return JSONResponse({"error": "ניתן להעביר רק בין דוחות פתוחים (לא סגורים)"}, status_code=409)
        ok = expense.move_line(con, lid, rid, target_id)
        con.close()
        if not ok:
            return JSONResponse({"error": "השורה לא נמצאה או שהיעד זהה למקור"}, status_code=404)
        return {"ok": True}

    @app.get("/exp/report/{rid}/line/{lid}/image")
    def exp_line_image(rid: int, lid: int, request: Request):
        con = connect()
        report = expense.get_report(con, rid)
        if not _owns(request, report):
            con.close()
            return JSONResponse({"error": "אין הרשאה"}, status_code=403)
        path = expense.line_image_path(con, rid, lid)
        con.close()
        if not path or not Path(path).exists():
            return JSONResponse({"error": "לא נמצא"}, status_code=404)
        data = Path(path).read_bytes()
        return Response(content=data, media_type=_media_for(path))

    @app.post("/exp/api/report/{rid}/line/{lid}/file")
    async def exp_api_line_file_add(rid: int, lid: int, request: Request,
                                    file: UploadFile | None = File(default=None)):
        con = connect()
        report = expense.get_report(con, rid)
        if not _owns(request, report):
            con.close()
            return JSONResponse({"error": "אין הרשאה"}, status_code=403)
        if report["status"] not in expense.EDITABLE_STATUSES:
            con.close()
            return JSONResponse({"error": "לא ניתן להוסיף מסמך בסטטוס זה"}, status_code=409)
        if con.execute("SELECT 1 FROM exp_lines WHERE id=? AND report_id=?",
                       (lid, rid)).fetchone() is None:
            con.close()
            return JSONResponse({"error": "שורה לא נמצאה"}, status_code=404)
        blob = await file.read() if file is not None else b""
        if not blob:
            con.close()
            return JSONResponse({"error": "לא צורף קובץ"}, status_code=400)
        folder = config.UPLOADS / str(rid)
        folder.mkdir(parents=True, exist_ok=True)
        ext = _img_ext(file.content_type, file.filename)
        fid = expense.add_line_file(con, lid, "")          # get an id for a unique filename
        path = folder / f"{lid}-{fid}{ext}"
        path.write_bytes(blob)
        con.execute("UPDATE exp_line_files SET path=? WHERE id=?", (str(path), fid))
        con.commit()
        row = con.execute("SELECT * FROM exp_lines WHERE id=?", (lid,)).fetchone()
        line = expense.line_dict(con, row)
        con.close()
        return {"ok": True, "line": line}

    @app.get("/exp/report/{rid}/line/{lid}/file/{fid}")
    def exp_line_file(rid: int, lid: int, fid: int, request: Request):
        con = connect()
        report = expense.get_report(con, rid)
        if not _owns(request, report):
            con.close()
            return JSONResponse({"error": "אין הרשאה"}, status_code=403)
        path = expense.line_file_path(con, lid, fid)
        con.close()
        if not path or not Path(path).exists():
            return JSONResponse({"error": "לא נמצא"}, status_code=404)
        return Response(content=Path(path).read_bytes(), media_type=_media_for(path))

    @app.post("/exp/api/report/{rid}/line/{lid}/file/{fid}/delete")
    def exp_api_line_file_delete(rid: int, lid: int, fid: int, request: Request):
        con = connect()
        report = expense.get_report(con, rid)
        if not _owns(request, report):
            con.close()
            return JSONResponse({"error": "אין הרשאה"}, status_code=403)
        if report["status"] not in expense.EDITABLE_STATUSES:
            con.close()
            return JSONResponse({"error": "לא ניתן למחוק מסמך בסטטוס זה"}, status_code=409)
        path = expense.delete_line_file(con, lid, fid)
        con.close()
        if path:
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                pass
        return {"ok": True}

    @app.post("/exp/api/report/{rid}/submit")
    def exp_api_report_submit(rid: int, request: Request):
        con = connect()
        report = expense.get_report(con, rid)
        if not _owns(request, report):
            con.close()
            return JSONResponse({"error": "אין הרשאה"}, status_code=403)
        if report["status"] not in expense.EDITABLE_STATUSES:
            con.close()
            return JSONResponse({"error": "הדוח כבר הוגש"}, status_code=409)
        lines = expense.get_lines(con, rid)
        if not lines:
            con.close()
            return JSONResponse({"error": "אין חשבוניות בדוח"}, status_code=400)

        if report["type"] == expense.CREDIT:
            # credit summary: compile, then send the 'ready for payment' e-mail to
            # the employee + accounting (credit reports have no approver)
            expense.set_status(con, rid, "compiled")
            report = expense.get_report(con, rid)
            rep, ldicts, _ = _load_report_for_pdf(con, report)
            pdf_bytes = _build_pdf(con, report)
            prof = expense.get_profile(con, report["owner"])
            recips = _payment_recipients(con, rep, prof)
            con.close()
            mail_sent = False
            if recips:
                html = mailer.payment_html(rep, _mail_lines(ldicts))
                mail_sent = mailer.send_mail(recips, f"לתשלום · {rep['title']}", html,
                                             [(_pdf_name(rep), pdf_bytes, "pdf")])["sent"]
            return {"ok": True, "status": "compiled", "mail_sent": mail_sent}

        # reimbursement: needs an approver, then send the request e-mail
        if report["approver_id"] is None:
            con.close()
            return JSONResponse({"error": "יש לבחור מנהל מאשר לפני הפקת הטופס"}, status_code=400)
        token = expense.ensure_approve_token(con, rid)
        expense.set_status(con, rid, "pending")
        report = expense.get_report(con, rid)
        rep, ldicts, _ = _load_report_for_pdf(con, report)
        pdf_bytes = _build_pdf(con, report)
        approve_url = f"{config.APP_BASE_URL}/exp/approve/{token}"
        pixel_url = f"{config.APP_BASE_URL}/exp/track/{token}.gif"
        html = mailer.approval_request_html(rep, _mail_lines(ldicts), approve_url, pixel_url)
        result = mailer.send_mail(rep["approver_email"], f"בקשת אישור · {rep['title']}",
                                  html, [(_pdf_name(rep), pdf_bytes, "pdf")])
        con.close()
        return {"ok": True, "status": "pending", "mail_sent": result["sent"],
                "mail_error": result.get("error")}

    @app.post("/exp/api/report/{rid}/send")
    async def exp_api_report_send(rid: int, request: Request):
        body = await request.json()
        email = (body.get("email") or "").strip()
        if "@" not in email:
            return JSONResponse({"error": "כתובת מייל לא תקינה"}, status_code=400)
        con = connect()
        report = expense.get_report(con, rid)
        if not _owns(request, report):
            con.close()
            return JSONResponse({"error": "אין הרשאה"}, status_code=403)
        if not expense.get_lines(con, rid):
            con.close()
            return JSONResponse({"error": "אין חשבוניות בדוח"}, status_code=400)
        rep, ldicts, _ = _load_report_for_pdf(con, report)
        pdf_bytes = _build_pdf(con, report)
        html = mailer.plain_report_html(rep, _mail_lines(ldicts), rep["owner_name"])
        result = mailer.send_mail(email, rep["title"], html,
                                  [(_pdf_name(rep), pdf_bytes, "pdf")])
        if result["sent"]:
            expense.record_sent(con, rid, email)
        con.close()
        return {"ok": True, "mail_sent": result["sent"], "mail_error": result.get("error")}

    @app.get("/exp/report/{rid}/pdf")
    def exp_report_pdf(rid: int, request: Request):
        con = connect()
        report = expense.get_report(con, rid)
        if not _owns(request, report):
            con.close()
            return JSONResponse({"error": "אין הרשאה"}, status_code=403)
        rep = expense.report_dict(con, report)
        data = _build_pdf(con, report)
        con.close()
        # ?view=1 → inline (for the in-app iframe viewer); default download (attachment)
        inline = request.query_params.get("view") == "1"
        return Response(content=data, media_type="application/pdf",
                        headers=_pdf_headers(rep, inline=inline))

    @app.post("/exp/api/report/{rid}/delete")
    def exp_api_report_delete(rid: int, request: Request):
        con = connect()
        report = expense.get_report(con, rid)
        if not _owns(request, report):
            con.close()
            return JSONResponse({"error": "אין הרשאה"}, status_code=403)
        if report["status"] == "approved" and not auth.is_admin(get_user(request)):
            con.close()
            return JSONResponse({"error": "לא ניתן למחוק דוח מאושר"}, status_code=409)
        expense.delete_report(con, rid)
        con.close()
        # best-effort cleanup of the report's uploaded images
        import shutil
        shutil.rmtree(config.UPLOADS / str(rid), ignore_errors=True)
        return {"ok": True}

    # ---- public approver flow (token-gated, no login) ----

    @app.get("/exp/approve/{token}", response_class=HTMLResponse)
    def exp_approve_page(request: Request, token: str):
        con = connect()
        report = expense.get_report_by_token(con, token)
        con.close()
        if report is None or report["type"] != expense.REIMBURSEMENT:
            return HTMLResponse(
                "<div dir='rtl' style='font-family:sans-serif;padding:40px;text-align:center'>"
                "<h2>הקישור אינו תקף</h2><p>ייתכן שהדוח נמחק או שהקישור שגוי.</p></div>",
                status_code=404)
        return templates.TemplateResponse(request, "exp_approve.html",
                                          {"title": "אישור דוח", "token": token})

    @app.get("/exp/api/public/approve/{token}")
    def exp_api_public_approve_get(token: str):
        con = connect()
        report = expense.get_report_by_token(con, token)
        if report is None:
            con.close()
            return JSONResponse({"error": "קישור לא תקף"}, status_code=404)
        # record the read-receipt: the approver actually opened the request page
        # (this fetch runs from page JS, so link-preview crawlers don't trip it)
        if report["status"] == "pending":
            expense.mark_viewed(con, report["id"])
        rep = expense.report_dict(con, report)
        lines = [expense.line_dict(con, r) for r in expense.get_lines(con, report["id"])]
        con.close()
        # the approver sees names/amounts/status but not tokens or e-mails
        safe = {k: rep[k] for k in ("title", "owner_name", "department", "month",
                                    "status", "total", "decision_note", "approver_name")}
        return {"report": safe, "lines": lines}

    # 1x1 transparent GIF that records an e-mail open (best-effort; images are often
    # blocked/proxied by mail clients — the reliable signal is the page-open above)
    _PIXEL = (b'GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04'
              b'\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x01D\x00;')

    @app.get("/exp/track/{token}.gif")
    def exp_track_pixel(token: str):
        con = connect()
        report = expense.get_report_by_token(con, token)
        if report is not None and report["status"] == "pending":
            expense.mark_viewed(con, report["id"])
        con.close()
        return Response(content=_PIXEL, media_type="image/gif",
                        headers={"Cache-Control": "no-store, no-cache, must-revalidate, private"})

    @app.get("/exp/approve/{token}/pdf")
    def exp_approve_pdf(token: str, request: Request):
        con = connect()
        report = expense.get_report_by_token(con, token)
        if report is None:
            con.close()
            return JSONResponse({"error": "קישור לא תקף"}, status_code=404)
        rep = expense.report_dict(con, report)
        data = _build_pdf(con, report)
        con.close()
        inline = request.query_params.get("view") == "1"
        return Response(content=data, media_type="application/pdf",
                        headers=_pdf_headers(rep, inline=inline))

    @app.post("/exp/api/public/approve/{token}")
    async def exp_api_public_approve_post(token: str, request: Request):
        body = await request.json()
        decision = (body.get("decision") or "").strip()
        note = body.get("note") or ""
        if decision not in ("approve", "reject"):
            return JSONResponse({"error": "החלטה לא תקינה"}, status_code=400)
        con = connect()
        report = expense.get_report_by_token(con, token)
        if report is None:
            con.close()
            return JSONResponse({"error": "קישור לא תקף"}, status_code=404)
        if report["status"] != "pending":
            con.close()
            return JSONResponse({"error": "הדוח כבר טופל"}, status_code=409)
        new_status = "approved" if decision == "approve" else "rejected"
        expense.set_status(con, report["id"], new_status, note=note)
        report = expense.get_report(con, report["id"])
        prof = expense.get_profile(con, report["owner"])
        if decision == "approve":
            # approved → the 'ready for payment' e-mail to employee + approver + accounting
            rep, ldicts, _ = _load_report_for_pdf(con, report)
            pdf_bytes = _build_pdf(con, report)
            recips = _payment_recipients(con, rep, prof)
            con.close()
            if recips:
                html = mailer.payment_html(rep, _mail_lines(ldicts))
                mailer.send_mail(recips, f"לתשלום · {rep['title']}", html,
                                 [(_pdf_name(rep), pdf_bytes, "pdf")])
        else:
            rep = expense.report_dict(con, report)
            con.close()
            # rejected → notify only the employee
            if prof and prof["email"]:
                html = mailer.status_update_html(rep, note=note)
                mailer.send_mail(prof["email"], f"עדכון סטטוס · {rep['title']}", html)
        return {"ok": True, "status": new_status}

    # ==================== passkeys / WebAuthn (optional login) ====================
    # Register requires an active session (you add a passkey to your own account);
    # login is pre-auth and gated only by the WebAuthn ceremony + a session challenge.

    @app.get("/exp/account", response_class=HTMLResponse)
    def exp_account_page(request: Request):
        return templates.TemplateResponse(request, "exp_account.html",
                                          {"title": "החשבון שלי"})

    @app.get("/auth/webauthn/credentials")
    def webauthn_credentials(request: Request):
        user = get_user(request)
        con = connect()
        creds = [{"id": c["id"], "label": c["label"], "created_at": c["created_at"]}
                 for c in passkey.get_credentials(con, user)]
        con.close()
        return {"enabled": passkey.available(), "credentials": creds}

    @app.post("/auth/webauthn/credentials/{cred_id}/delete")
    def webauthn_credential_delete(cred_id: int, request: Request):
        con = connect()
        passkey.delete_credential(con, get_user(request), cred_id)
        con.close()
        return {"ok": True}

    @app.post("/auth/webauthn/register/options")
    def webauthn_register_options(request: Request):
        if not passkey.available():
            return _passkey_off()
        user = get_user(request)
        con = connect()
        prof = expense.get_profile(con, user)
        display = prof["display_name"] if prof else user
        options_json, challenge = passkey.registration_options(con, user, display)
        con.close()
        request.session["wa_reg_chal"] = challenge
        return Response(content=options_json, media_type="application/json")

    @app.post("/auth/webauthn/register/verify")
    async def webauthn_register_verify(request: Request):
        if not passkey.available():
            return _passkey_off()
        body = await request.json()
        challenge = request.session.pop("wa_reg_chal", None)
        if not challenge:
            return JSONResponse({"error": "פג תוקף הבקשה, נסה שוב"}, status_code=400)
        con = connect()
        try:
            import json as _json
            passkey.verify_registration(con, get_user(request),
                                        _json.dumps(body.get("credential")),
                                        challenge, label=body.get("label", ""))
        except Exception:
            con.close()
            return JSONResponse({"error": "רישום ה-Passkey נכשל"}, status_code=400)
        con.close()
        return {"ok": True}

    @app.post("/auth/webauthn/login/options")
    async def webauthn_login_options(request: Request):
        if not passkey.available():
            return _passkey_off()
        body = await request.json()
        username = (body.get("username") or "").strip()
        if not username:
            return JSONResponse({"error": "יש להזין שם משתמש"}, status_code=400)
        con = connect()
        res = passkey.authentication_options(con, username)
        con.close()
        if res is None:
            return JSONResponse({"error": "למשתמש זה אין Passkey רשום"}, status_code=404)
        options_json, challenge = res
        request.session["wa_login_chal"] = challenge
        request.session["wa_login_user"] = username.lower()
        return Response(content=options_json, media_type="application/json")

    @app.post("/auth/webauthn/login/verify")
    async def webauthn_login_verify(request: Request):
        if not passkey.available():
            return _passkey_off()
        body = await request.json()
        challenge = request.session.pop("wa_login_chal", None)
        username = request.session.pop("wa_login_user", None)
        if not challenge or not username:
            return JSONResponse({"error": "פג תוקף הבקשה, נסה שוב"}, status_code=400)
        con = connect()
        ok = passkey.verify_authentication(con, username, body.get("credential") or {}, challenge)
        con.close()
        if not ok:
            return JSONResponse({"error": "אימות ה-Passkey נכשל"}, status_code=401)
        request.session["user"] = username
        return {"ok": True}


# ==================== app factory ====================

def create_app(*, include_org: bool = True, include_exp: bool = True,
               title: str = "עץ ארגוני — ארקיע",
               site_name: str = "עץ ארגוני · ארקיע",
               session_cookie: str = "arkia_org_session",
               home_path: str = "/") -> FastAPI:
    """Build an ASGI app with the requested route groups.

    include_org / include_exp choose which module is mounted. session_cookie and
    home_path are kept distinct per entrypoint so two services on the same host
    (e.g. the org tree and a standalone expense service) don't share a session or
    redirect to each other's landing page. include_org / site_name are exposed on
    app.state so templates can hide org-tree links and show the right brand when
    the expense service runs standalone.
    """
    app = FastAPI(title=title)
    app.state.home_path = home_path
    app.state.include_org = include_org
    app.state.site_name = site_name
    app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")

    # login gate (added first → runs inside the session middleware below)
    @app.middleware("http")
    async def require_login(request: Request, call_next):
        path = request.url.path
        if request.session.get("user"):
            # a user with a one-time password is confined to the change-password flow
            if request.session.get("must_change") and not any(
                    path.startswith(p) for p in CHANGE_PW_ALLOWED):
                if path.startswith(("/api/", "/org/api/", "/exp/api/")):
                    return JSONResponse({"error": "נדרש שינוי סיסמה לפני המשך"}, status_code=403)
                return RedirectResponse("/change-password", status_code=303)
            return await call_next(request)
        if any(path.startswith(p) for p in PUBLIC_PREFIXES):
            return await call_next(request)
        if path.startswith(("/api/", "/org/api/", "/exp/api/")):
            return JSONResponse({"error": "לא מחובר — נא להתחבר מחדש"}, status_code=401)
        return RedirectResponse(f"/login?next={quote(path)}", status_code=303)

    # session cookie (added last → outermost → establishes request.session first)
    app.add_middleware(
        SessionMiddleware,
        secret_key=auth.get_secret_key(),
        session_cookie=session_cookie,
        same_site="lax",
        https_only=os.environ.get("SESSION_HTTPS_ONLY", "").lower() in ("1", "true", "yes"),
        max_age=IDLE_SECONDS,
    )

    @app.on_event("startup")
    def _startup():
        config.ensure_dirs()
        con = connect()
        init_db(con)
        con.close()

    _register_common(app)
    if include_org:
        _register_org(app)
    if include_exp:
        _register_exp(app)

    # bare domain "/" → the service's home, so the root URL isn't a JSON 404
    # (only when home isn't already "/", which the org chart serves directly)
    if home_path != "/":
        @app.get("/", include_in_schema=False)
        def _root_redirect():
            return RedirectResponse(home_path, status_code=307)

    return app
