# -*- coding: utf-8 -*-
"""E-mail delivery for the expense module.

Sends through a plain SMTP relay with STARTTLS — configured for Oracle OCI Email
Delivery by default (``smtp.email.<region>.oci.oraclecloud.com:587``), but any
SMTP server works. Credentials come from the environment only (see app.config).

When SMTP isn't configured the message is written to ``data/outbox/`` as a .eml
file instead of being sent, so the approval / status / free-send flows can be
tested end-to-end before any credentials exist. Uses only the standard library.
"""
from __future__ import annotations

import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path

from .. import config

BLUE = "#1e63b8"
BLUE_D = "#123a86"


def send_mail(to: str | list[str], subject: str, html: str,
              attachments: list[tuple[str, bytes, str]] | None = None) -> dict:
    """Send (or dry-run) a branded HTML e-mail. ``attachments`` is a list of
    (filename, bytes, mime_subtype) e.g. ("report.pdf", b"...", "pdf").
    Returns {"sent": bool, "path": <outbox file if dry-run>}."""
    recipients = [to] if isinstance(to, str) else list(to)
    recipients = [r.strip() for r in recipients if r and r.strip()]
    if not recipients:
        raise ValueError("no recipient")

    def _build(html_body, atts):
        m = EmailMessage()
        m["Subject"] = subject
        m["From"] = formataddr((config.SMTP_FROM_NAME, config.SMTP_FROM))
        m["To"] = ", ".join(recipients)
        m["Message-ID"] = make_msgid()
        m.set_content("הודעה זו דורשת תצוגת HTML.")
        m.add_alternative(html_body, subtype="html")
        for filename, blob, subtype in (atts or []):
            maintype = "application" if subtype in ("pdf", "octet-stream") else "image"
            m.add_attachment(blob, maintype=maintype, subtype=subtype, filename=filename)
        return m

    msg = _build(html, attachments)
    # OCI Email Delivery (and most relays) cap a message at ~2 MB. If the PDF pushes
    # us over, send the mail without the attachment + a note, rather than fail (552).
    if attachments and len(bytes(msg)) > 1_900_000:
        note = ("<p style='background:#fff7ed;border:1px solid #fed7aa;color:#9a3412;"
                "border-radius:8px;padding:10px 12px;font-size:13px'>הקובץ המצורף (PDF) "
                "הושמט בשל מגבלת גודל של שרת הדואר. ניתן לצפות/להוריד את הדוח המלא במערכת "
                "או בקישור שבמייל.</p>")
        msg = _build(note + html, None)

    if not config.mail_configured():
        return {"sent": False, "path": _spool_to_outbox(msg)}

    # never raise to the caller — a mail hiccup must not break submit / send flows
    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as s:
            s.ehlo()
            if config.SMTP_STARTTLS:
                s.starttls(context=ctx)
                s.ehlo()
            if config.SMTP_USERNAME:
                s.login(config.SMTP_USERNAME, config.SMTP_PASSWORD)
            s.send_message(msg)
        return {"sent": True, "path": None}
    except Exception as e:
        return {"sent": False, "path": _spool_to_outbox(msg), "error": str(e)}


def _spool_to_outbox(msg: EmailMessage) -> str:
    config.ensure_dirs()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    path = config.OUTBOX / f"{stamp}.eml"
    path.write_bytes(bytes(msg))
    return str(path)


# ==================== branded HTML templates ====================

def _logo_url() -> str:
    """Absolute URL of the Arkia logo, but only if the file is actually present
    (and a public base URL is configured) — otherwise the header falls back to text."""
    if config.APP_BASE_URL and (config.BASE / "static" / "img" / "arkia-logo.png").exists():
        return f"{config.APP_BASE_URL}/static/img/arkia-logo.png"
    return ""


def _shell(title: str, body: str) -> str:
    """Arkia look & feel: logo/blue header, white card. dir=rtl is set on the
    content divs (Gmail strips it from <html>, which flips the Hebrew tables)."""
    logo = _logo_url()
    if logo:
        header = (
            f'<div style="background:#fff;text-align:center;padding:14px 22px;'
            f'border-radius:14px 14px 0 0;border:1px solid #e2e8f0;border-bottom:0">'
            f'<img src="{logo}" alt="arkia" style="height:34px"></div>'
            f'<div style="background:{BLUE_D};color:#fff;padding:12px 22px;'
            f'font-size:17px;font-weight:700">{title}</div>')
    else:
        header = (f'<div style="background:{BLUE_D};color:#fff;padding:16px 22px;'
                  f'border-radius:14px 14px 0 0;font-size:18px;font-weight:700">'
                  f'ארקיע · {title}</div>')
    return f"""\
<!DOCTYPE html><html lang="he" dir="rtl"><head><meta charset="utf-8"></head>
<body style="margin:0;background:#f1f5f9;font-family:Arial,'Segoe UI',sans-serif;color:#0f172a">
  <div dir="rtl" style="max-width:640px;margin:0 auto;padding:20px;text-align:right">
    {header}
    <div dir="rtl" style="background:#fff;padding:22px;border-radius:0 0 14px 14px;
                border:1px solid #e2e8f0;border-top:0;text-align:right">{body}</div>
    <div style="color:#94a3b8;font-size:12px;text-align:center;padding:14px">
      הודעה זו נשלחה ממערכת החזרי ההוצאות של ארקיע.</div>
  </div>
</body></html>"""


def _btn(href: str, label: str, color: str = BLUE) -> str:
    return (f'<a href="{href}" style="display:inline-block;background:{color};color:#fff;'
            f'text-decoration:none;padding:11px 22px;border-radius:9px;font-weight:700;'
            f'font-size:15px;margin:6px 6px 6px 0">{label}</a>')


def _fmt(n: float) -> str:
    return f"{n:,.2f}"


def _lines_table(lines: list[dict]) -> str:
    th = "padding:8px;border:1px solid #e2e8f0;font-size:13px;text-align:right"
    head = ("<tr style='background:#f8fafc'>"
            f"<th style='{th};text-align:center'>מס׳</th>"
            f"<th style='{th}'>ספק</th>"
            f"<th style='{th}'>סיווג</th>"
            f"<th style='{th};text-align:left'>סכום ₪</th></tr>")
    rows = ""
    for ln in lines:
        note = _esc(ln.get("note") or "")
        note_html = (f"<div style='color:#64748b;font-size:12px;margin-top:2px'>{note}</div>"
                     if note else "")
        rows += (
            "<tr>"
            f"<td style='padding:8px;border:1px solid #e2e8f0;text-align:center'>{ln['seq']}</td>"
            f"<td style='padding:8px;border:1px solid #e2e8f0;text-align:right'>{_esc(ln['supplier'])}{note_html}</td>"
            f"<td style='padding:8px;border:1px solid #e2e8f0;text-align:right'>{_esc(ln.get('category') or '')}</td>"
            f"<td style='padding:8px;border:1px solid #e2e8f0;text-align:left'>{_fmt(ln['amount'])}</td>"
            "</tr>")
    return (f"<table dir='rtl' style='border-collapse:collapse;width:100%;margin:10px 0'>"
            f"{head}{rows}</table>")


def _fmt_dt(s) -> str:
    """'YYYY-MM-DD HH:MM:SS' → 'DD/MM/YYYY HH:MM' (best-effort)."""
    s = (s or "").strip()
    if not s:
        return ""
    try:
        d, _, t = s.partition(" ")
        y, m, dd = d.split("-")
        hm = ":".join(t.split(":")[:2]) if t else ""
        return f"{dd}/{m}/{y}" + (f" {hm}" if hm else "")
    except Exception:
        return s


def _approval_note(report: dict) -> str:
    """Green banner stating who approved the report and when (only once approved)."""
    if report.get("status") != "approved":
        return ""
    who = _esc(report.get("approver_name") or "המנהל המאשר")
    when = _fmt_dt(report.get("decided_at"))
    when_txt = f" בתאריך {when}" if when else ""
    return (f"<div dir='rtl' style='background:#e8f5ec;border:1px solid #b7e0c4;color:#166a3b;"
            f"border-radius:10px;padding:12px 14px;margin:12px 0;font-size:14px;text-align:right'>"
            f"✓ דוח הוצאות זה <b>אושר</b> על ידי <b>{who}</b>{when_txt}.</div>")


def _esc(s) -> str:
    s = "" if s is None else str(s)
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def approval_request_html(report: dict, lines: list[dict], approve_url: str,
                          pixel_url: str = "") -> str:
    pixel = (f'<img src="{pixel_url}" width="1" height="1" alt="" '
             f'style="display:none">') if pixel_url else ""
    body = f"""
      <p style="font-size:15px">שלום {_esc(report.get('approver_name') or '')},</p>
      <p style="font-size:15px">הוגש לאישורך דוח <b>{_esc(report['title'])}</b>.</p>
      <table style="font-size:14px;margin:6px 0">
        <tr><td style="padding:3px 10px 3px 0;color:#64748b">עובד</td><td><b>{_esc(report['owner_name'])}</b></td></tr>
        <tr><td style="padding:3px 10px 3px 0;color:#64748b">מחלקה</td><td>{_esc(report['department'])}</td></tr>
        <tr><td style="padding:3px 10px 3px 0;color:#64748b">חודש</td><td>{_esc(report['month'])}</td></tr>
        <tr><td style="padding:3px 10px 3px 0;color:#64748b">סכום ההחזר</td>
            <td style="font-size:17px;font-weight:700;color:{BLUE_D}">₪ {_fmt(report['total'])}</td></tr>
      </table>
      {_lines_table(lines)}
      <p style="font-size:14px;color:#475569">ריכוז החשבוניות והמסמכים הסרוקים מצורפים כקובץ PDF.</p>
      <div style="margin-top:16px">
        {_btn(approve_url, 'פתיחת הדוח לאישור / דחייה')}
      </div>
      <p style="font-size:12px;color:#94a3b8;margin-top:14px">
        אם הכפתור אינו עובד, העתק את הקישור לדפדפן:<br>{_esc(approve_url)}</p>
      {pixel}
    """
    return _shell("בקשת אישור דוח החזרי הוצאות", body)


STATUS_HE = {"draft": "בהכנה", "pending": "ממתין לאישור",
             "approved": "אושר", "rejected": "נדחה", "compiled": "רוכז"}
STATUS_COLOR = {"approved": "#16a34a", "rejected": "#dc2626",
                "pending": "#a16207", "compiled": BLUE}


def status_update_html(report: dict, note: str = "") -> str:
    st = report["status"]
    color = STATUS_COLOR.get(st, BLUE_D)
    note_html = (f"<p style='font-size:14px'><b>הערת המאשר:</b> {_esc(note)}</p>"
                 if note else "")
    body = f"""
      <p style="font-size:15px">שלום {_esc(report['owner_name'])},</p>
      <p style="font-size:15px">סטטוס הדוח <b>{_esc(report['title'])}</b> עודכן:</p>
      <p style="font-size:22px;font-weight:800;color:{color};margin:6px 0">
        {_esc(STATUS_HE.get(st, st))}</p>
      {_approval_note(report)}
      <table style="font-size:14px;margin:6px 0">
        <tr><td style="padding:3px 10px 3px 0;color:#64748b">מחלקה</td><td>{_esc(report['department'])}</td></tr>
        <tr><td style="padding:3px 10px 3px 0;color:#64748b">חודש</td><td>{_esc(report['month'])}</td></tr>
        <tr><td style="padding:3px 10px 3px 0;color:#64748b">סכום</td><td>₪ {_fmt(report['total'])}</td></tr>
      </table>
      {note_html}
    """
    return _shell("עדכון סטטוס דוח", body)


def plain_report_html(report: dict, lines: list[dict], sender_name: str = "") -> str:
    """For the free 'send to any e-mail' action (both report types)."""
    intro = (f"<p style='font-size:15px'>מצורף הטופס <b>{_esc(report['title'])}</b>"
             + (f" מאת {_esc(sender_name)}" if sender_name else "") + ".</p>")
    body = f"""
      {intro}
      {_approval_note(report)}
      <table style="font-size:14px;margin:6px 0">
        <tr><td style="padding:3px 10px 3px 0;color:#64748b">חודש</td><td>{_esc(report['month'])}</td></tr>
        <tr><td style="padding:3px 10px 3px 0;color:#64748b">סכום כולל</td>
            <td style="font-weight:700;color:{BLUE_D}">₪ {_fmt(report['total'])}</td></tr>
      </table>
      {_lines_table(lines)}
      <p style="font-size:14px;color:#475569">ריכוז החשבוניות המלא והמסמכים הסרוקים מצורפים כ-PDF.</p>
    """
    return _shell(_esc(report['title']), body)
