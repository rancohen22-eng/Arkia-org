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

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import auth, config
from .db import connect, init_db
from .services import org
from .services import expense
from .services import ocr as ocr_service
from .services import mailer, pdf
from .services import passkey

BASE = Path(__file__).resolve().parent
app = FastAPI(title="עץ ארגוני — ארקיע")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")
templates.env.globals["is_admin"] = auth.is_admin
templates.env.globals["passkey_enabled"] = passkey.available

# ---- authentication (session cookie + login gate) ----
# /org/fill/* and /org/api/public/* are reached by managers via a WhatsApp magic
# link with no login — access is gated by the secret token in the URL, not a session.
PUBLIC_PREFIXES = ("/login", "/logout", "/static", "/health", "/favicon",
                   "/org/fill", "/org/api/public",
                   "/exp/approve", "/exp/api/public",
                   "/auth/webauthn/login")   # passkey login is pre-auth; register isn't


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if request.session.get("user") or any(path.startswith(p) for p in PUBLIC_PREFIXES):
        return await call_next(request)
    if path.startswith(("/api/", "/org/api/", "/exp/api/")):
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
    config.ensure_dirs()
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


# ==================== החזרי הוצאות / ריכוז אשראי (expense) ====================
# Settings screens below are admin-only. The employee-facing report editor and the
# token-gated approver screen arrive in later phases.

def _forbidden_admin(request: Request) -> JSONResponse | None:
    """None if the caller is an admin, else a 403 JSON body."""
    if auth.is_admin(get_user(request)):
        return None
    return JSONResponse({"error": "פעולה זו מותרת למנהל מערכת בלבד"}, status_code=403)


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
                           bool(body.get("is_active", True)))
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


# ==================== expense: employee report flow ====================

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
        blob = None
        if r["invoice_path"]:
            p = Path(r["invoice_path"])
            if p.exists():
                blob = p.read_bytes()
        d["invoice_bytes"] = blob
        lines.append(d)
    return rep, lines, expense.category_totals(con, report["id"])


def _build_pdf(con, report) -> bytes:
    rep, lines, cats = _load_report_for_pdf(con, report)
    return pdf.build_report_pdf(rep, lines, cats)


def _mail_lines(lines: list[dict]) -> list[dict]:
    return [{"seq": l["seq"], "supplier": l["supplier"], "amount": l["amount"],
             "category": l.get("category")} for l in lines]


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
    data = {
        "user": user,
        "profile": {"display_name": (prof["display_name"] if prof else ""),
                    "department": (prof["department"] if prof else ""),
                    "email": (prof["email"] if prof else "")},
        "reports": reports,
        "approvers": [expense._dict(r, "id", "name", "email")
                      for r in expense.list_approvers(con, active_only=True)],
        "departments": [r["name"] for r in expense.list_departments(con, active_only=True)],
    }
    con.close()
    return data


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
    approver_id = body.get("approver_id") or None
    rid = expense.create_report(con, user, rtype, month, dept, approver_id, title)
    audit(con, user, "exp_reports", str(rid), "create", None, rtype)
    con.commit()
    con.close()
    return {"id": rid}


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
    expense.update_report_fields(
        con, rid, body.get("month", report["month"]),
        body.get("department", report["department"]),
        body.get("approver_id") or None,
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

    supplier, amount, raw, ocr_engine = "", 0.0, "", "none"
    blob = None
    if file is not None:
        blob = await file.read()
        if blob:
            res = ocr_service.extract_invoice(blob)
            supplier, amount, raw, ocr_engine = (res.supplier, res.amount or 0.0,
                                                 res.raw, res.engine)

    lid = expense.add_line(con, rid, supplier=supplier, amount=amount,
                           ocr_raw=raw)
    if blob:
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
                             body.get("amount", 0), body.get("category_id") or None)
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
    if report["status"] not in expense.EDITABLE_STATUSES:
        con.close()
        return JSONResponse({"error": "לא ניתן לערוך דוח בסטטוס זה"}, status_code=409)
    path = expense.delete_line(con, rid, lid)
    total = expense.report_dict(con, expense.get_report(con, rid))["total"]
    con.close()
    if path:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass
    return {"ok": True, "total": total}


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
        # credit summary: compile & mark, no approver e-mail
        expense.set_status(con, rid, "compiled")
        con.close()
        return {"ok": True, "status": "compiled"}

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
    html = mailer.approval_request_html(rep, _mail_lines(ldicts), approve_url)
    result = mailer.send_mail(rep["approver_email"], f"בקשת אישור · {rep['title']}",
                              html, [(_pdf_name(rep), pdf_bytes, "pdf")])
    con.close()
    return {"ok": True, "status": "pending", "mail_sent": result["sent"]}


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
    expense.record_sent(con, rid, email)
    con.close()
    return {"ok": True, "mail_sent": result["sent"]}


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
    return Response(content=data, media_type="application/pdf", headers=_pdf_headers(rep))


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
    rep = expense.report_dict(con, report)
    lines = [expense.line_dict(con, r) for r in expense.get_lines(con, report["id"])]
    con.close()
    # the approver sees names/amounts/status but not tokens or e-mails
    safe = {k: rep[k] for k in ("title", "owner_name", "department", "month",
                                "status", "total", "decision_note", "approver_name")}
    return {"report": safe, "lines": lines}


@app.get("/exp/approve/{token}/pdf")
def exp_approve_pdf(token: str):
    con = connect()
    report = expense.get_report_by_token(con, token)
    if report is None:
        con.close()
        return JSONResponse({"error": "קישור לא תקף"}, status_code=404)
    rep = expense.report_dict(con, report)
    data = _build_pdf(con, report)
    con.close()
    return Response(content=data, media_type="application/pdf", headers=_pdf_headers(rep))


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
    rep = expense.report_dict(con, report)
    prof = expense.get_profile(con, report["owner"])
    con.close()
    # notify the employee of the new status (if we have their e-mail on file)
    if prof and prof["email"]:
        html = mailer.status_update_html(rep, note=note)
        mailer.send_mail(prof["email"], f"עדכון סטטוס · {rep['title']}", html)
    return {"ok": True, "status": new_status}


# ---- small helpers for uploads / pdf naming ----

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


# ==================== passkeys / WebAuthn (optional login) ====================
# Register requires an active session (you add a passkey to your own account);
# login is pre-auth and gated only by the WebAuthn ceremony + a session challenge.

def _passkey_off():
    return JSONResponse({"error": "התחברות ב-Passkey אינה מופעלת"}, status_code=404)


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
