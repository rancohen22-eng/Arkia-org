# -*- coding: utf-8 -*-
"""Branded PDF for an expense report — pure-Python (fpdf2 + python-bidi).

No headless browser and no system libraries: a single ``pip install`` of fpdf2,
python-bidi and Pillow covers it, and the Hebrew font ships in the repo
(``app/static/fonts/DejaVuSans*.ttf``), so first-time setup stays trivial.

Layout (per spec):
  • page 1  — ריכוז: one row per invoice (running document number, supplier,
              category, amount) + subtotals per category + grand total.
  • page N  — each scanned invoice, headed by the same document number, so the
              summary and the images line up 1..N.
"""
from __future__ import annotations

import io

from fpdf import FPDF

try:
    from bidi import get_display          # python-bidi >= 0.5 (Rust) API
except ImportError:                        # older pure-python API
    from bidi.algorithm import get_display

from .. import config

BLUE = (30, 99, 184)
BLUE_D = (18, 58, 134)
INK = (15, 23, 42)
MUTED = (100, 116, 139)
LINE = (226, 232, 240)


def _rtl(s) -> str:
    """Reorder a logical (Hebrew) string to visual order for LTR rendering."""
    return get_display(str(s)) if s not in (None, "") else ""


class _PDF(FPDF):
    def __init__(self):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.set_auto_page_break(auto=True, margin=15)
        reg = config.FONT_DIR / "DejaVuSans.ttf"
        bold = config.FONT_DIR / "DejaVuSans-Bold.ttf"
        self.add_font("Dejavu", "", str(reg))
        self.add_font("Dejavu", "B", str(bold))
        self.set_font("Dejavu", "", 11)
        self.wm_text = ""            # diagonal watermark, set per report before add_page()
        self.wm_color = MUTED

    def header(self):
        """Draw the status watermark (ממתין לאישור / אושר / נדחה) behind every page."""
        if not self.wm_text:
            return
        self.set_font("Dejavu", "B", 68)
        self.set_text_color(*self.wm_color)
        vis = _rtl(self.wm_text)
        try:
            with self.local_context(fill_opacity=0.20):
                with self.rotation(28, self.w / 2, self.h / 2):
                    self.text(self.w / 2 - self.get_string_width(vis) / 2,
                              self.h / 2 + 8, vis)
        except Exception:
            pass
        self.set_text_color(*INK)
        self.set_font("Dejavu", "", 11)

    def rcell(self, w, h, txt, border=0, align="R", fill=False):
        self.cell(w, h, _rtl(txt), border=border, align=align, fill=fill)

    def _fit(self, txt, width, size):
        """Truncate txt (logical) so its visual width fits `width` mm at `size`."""
        self.set_font_size(size)
        s = str(txt)
        if self.get_string_width(_rtl(s)) <= width:
            return s
        while s and self.get_string_width(_rtl(s + "…")) > width:
            s = s[:-1]
        return (s + "…") if s else ""


# FX provider label (stored code → Hebrew) shown in the conversion note
_FX_SRC_HE = {"BOI": "שער יציג, בנק ישראל", "ECB": "ECB"}


def _money(n: float) -> str:
    return f"{(n or 0):,.2f}"


def _fmt_date(s: str | None) -> str:
    """'YYYY-MM-DD' → 'DD/MM/YYYY' (best-effort; returns the input on any mismatch)."""
    s = (s or "").strip()
    try:
        y, m, d = s[:10].split("-")
        return f"{d}/{m}/{y}"
    except Exception:
        return s


def _amt(ln) -> str:
    """Amount with its currency symbol, e.g. '₪ 320.00' / '$ 45.00'."""
    return config.currency_symbol(ln.get("currency") or "ILS") + " " + _money(ln.get("amount"))


def _rtl_row(pdf: _PDF, cells, widths, aligns, h=8, header=False, size=10):
    """Draw one table row right-to-left: cell 0 sits at the right margin."""
    pdf.set_font("Dejavu", "B" if header else "", size)
    if header:
        pdf.set_fill_color(240, 245, 250)
        pdf.set_text_color(*BLUE_D)
    else:
        pdf.set_text_color(*INK)
    x = pdf.w - pdf.r_margin
    y = pdf.get_y()
    for text, w, al in zip(cells, widths, aligns):
        x -= w
        pdf.set_xy(x, y)
        pdf.set_draw_color(*LINE)
        pdf.cell(w, h, _rtl(pdf._fit(text, w - 3, size)), border=1, align=al, fill=header)
    pdf.set_xy(pdf.l_margin, y + h)


def build_report_pdf(report: dict, lines: list[dict],
                     category_totals: list[tuple[str, float]],
                     conversion: dict | None = None) -> bytes:
    """`report` needs title/owner_name/department/month/status/total.
    `lines` need seq/supplier/amount/category/invoice_bytes (bytes or None).
    `conversion` (from ``fx.convert_lines``) carries the ILS-converted per-category
    totals; when it reports foreign currencies the summary is shown in shekels at
    the expense-date rate (with the rates used), instead of naively summing codes."""
    pdf = _PDF()
    # diagonal status watermark on every page (credit compilations aren't approved → none)
    wm = None if report.get("type") == "credit" else _WM.get(report.get("status", ""))
    if wm:
        pdf.wm_text, pdf.wm_color = wm
    pdf.add_page()
    W = pdf.w - pdf.l_margin - pdf.r_margin

    # ---- logo (if bundled) then the blue title band ----
    logo = config.BASE / "static" / "img" / "arkia-logo.png"
    if logo.exists():
        try:
            pdf.image(str(logo), x=pdf.l_margin, y=pdf.get_y(), h=12)   # transparent PNG on white
            pdf.ln(16)
            band_text = report["title"]
        except Exception:
            band_text = "ארקיע · " + report["title"]
    else:
        band_text = "ארקיע · " + report["title"]
    pdf.set_fill_color(*BLUE_D)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Dejavu", "B", 15)
    pdf.set_x(pdf.l_margin)
    pdf.cell(W, 12, _rtl(band_text), border=0, align="R", fill=True)
    pdf.ln(15)

    # ---- meta block (right-aligned label/value pairs) ----
    pdf.set_text_color(*INK)
    meta = [("עובד", report.get("owner_name", "")),
            ("מחלקה", report.get("department", "")),
            ("חודש", report.get("month", "")),
            ("סטטוס", _STATUS_HE.get(report.get("status", ""), report.get("status", "")))]
    pdf.set_font("Dejavu", "", 11)
    for label, value in meta:
        pdf.set_x(pdf.l_margin)
        pdf.set_text_color(*MUTED)
        pdf.cell(W * 0.72, 7, _rtl(str(value)), align="R")
        pdf.set_text_color(*BLUE_D)
        pdf.set_font("Dejavu", "B", 11)
        pdf.cell(W * 0.28, 7, _rtl(label + " :"), align="R")
        pdf.set_font("Dejavu", "", 11)
        pdf.ln(7)
    pdf.ln(3)

    # ---- approval stamp (page 1) — appears once the approver has approved ----
    if report.get("status") == "approved":
        _draw_stamp(pdf, report.get("approver_name") or "", _fmt_dt(report.get("decided_at")))

    # ---- invoice lines table ----
    pdf.set_font("Dejavu", "B", 12)
    pdf.set_text_color(*BLUE_D)
    pdf.set_x(pdf.l_margin)
    pdf.cell(W, 8, _rtl("ריכוז חשבוניות"), align="R")
    pdf.ln(10)

    widths = [W * 0.09, W * 0.19, W * 0.32, W * 0.20, W * 0.20]  # מס׳ | תאריך | ספק | סיווג | סכום
    aligns = ["C", "C", "R", "R", "L"]
    header = ["מס׳", "תאריך", "ספק", "סיווג", "סכום"]
    _rtl_row(pdf, header, widths, aligns, header=True)
    for ln in lines:
        if pdf.get_y() > pdf.h - 25:
            pdf.add_page()
            _rtl_row(pdf, header, widths, aligns, header=True)
        _rtl_row(pdf, [str(ln["seq"]), ln.get("date", "") or "", ln.get("supplier", ""),
                       ln.get("category", "") or "", _amt(ln)],
                 widths, aligns)
        note = (ln.get("note") or "").strip()
        if note:
            pdf.set_font("Dejavu", "", 8.5)
            pdf.set_text_color(90, 100, 120)
            pdf.set_draw_color(*LINE)
            pdf.set_x(pdf.l_margin)
            pdf.cell(W, 6, _rtl(pdf._fit("הערה: " + note, W - 4, 8.5)),
                     border="LRB", align="R")
            pdf.ln(6)

    # grand total — one row per currency (summing across currencies is meaningless)
    cur_tot: dict = {}
    for ln in lines:
        c = ln.get("currency") or "ILS"
        cur_tot[c] = cur_tot.get(c, 0) + (ln.get("amount") or 0)
    for c, amt in (cur_tot or {"ILS": 0}).items():
        _rtl_row(pdf, ["", "", "", "סה\"כ", config.currency_symbol(c) + " " + _money(amt)],
                 widths, ["C", "C", "R", "R", "L"], header=True)
    pdf.ln(6)

    # ---- subtotals per category ----
    conv = conversion or {}
    has_foreign = bool(conv.get("has_foreign"))
    # foreign lines are converted to shekels at the expense-date rate; a pure-ILS
    # report keeps the plain naive subtotals (identical numbers, no clutter).
    summary = conv["categories"] if has_foreign else category_totals
    if summary:
        pdf.set_font("Dejavu", "B", 12)
        pdf.set_text_color(*BLUE_D)
        pdf.set_x(pdf.l_margin)
        pdf.cell(W, 8, _rtl("סיכום לפי סיווג"), align="R")
        pdf.ln(10)
        cw = [W * 0.5, W * 0.5]
        amt_hdr = "סכום ₪ (מומר)" if has_foreign else "סכום ₪"
        _rtl_row(pdf, ["סיווג", amt_hdr], cw, ["R", "L"], header=True)
        for name, amt in summary:
            _rtl_row(pdf, [name or "—", _money(amt)], cw, ["R", "L"])
        if has_foreign:
            _rtl_row(pdf, ["סה\"כ ₪", _money(conv.get("grand_ils", 0))],
                     cw, ["R", "L"], header=True)
        # note: which rates were used, and any currency that couldn't be converted
        if has_foreign:
            pdf.ln(2)
            pdf.set_font("Dejavu", "", 8.5)
            pdf.set_text_color(*MUTED)
            for cur, as_of, rate, src in conv.get("rates", []):
                sym = config.currency_symbol(cur)
                src_he = _FX_SRC_HE.get(src, src or "")
                line = f"המרה: 1 {sym} = {rate:.4f} ₪  ·  {src_he}, {cur}/ILS ליום {_fmt_date(as_of)}"
                pdf.set_x(pdf.l_margin)
                pdf.cell(W, 5, _rtl(pdf._fit(line, W - 2, 8.5)), align="R")
                pdf.ln(5)
            if conv.get("unconverted"):
                pdf.set_text_color(180, 60, 60)
                miss = ", ".join(conv["unconverted"])
                pdf.set_x(pdf.l_margin)
                pdf.cell(W, 5, _rtl(pdf._fit(
                    "לא נמצא שער חליפין ל: " + miss + " — סכומים אלו לא נכללו בהמרה לשקל.",
                    W - 2, 8.5)), align="R")
                pdf.ln(5)
            pdf.set_text_color(*INK)

    # ---- one page per document (a line may carry several) ----
    for ln in lines:
        blobs = ln.get("invoice_bytes_list")
        if blobs is None:
            blobs = [ln["invoice_bytes"]] if ln.get("invoice_bytes") else []
        if not blobs:
            blobs = [None]                      # still emit a "no document" page
        n = len(blobs)
        for i, blob in enumerate(blobs, start=1):
            pdf.add_page()
            pdf.set_fill_color(*BLUE)
            pdf.set_text_color(255, 255, 255)
            pdf.set_font("Dejavu", "B", 12)
            pdf.set_x(pdf.l_margin)
            seq = f"{ln['seq']}" + (f" ({i}/{n})" if n > 1 else "")
            head = f"מסמך מס׳ {seq}  ·  {ln.get('supplier','')}  ·  {_amt(ln)}"
            pdf.cell(W, 10, _rtl(pdf._fit(head, W - 4, 12)), border=0, align="R", fill=True)
            pdf.ln(14)
            _place_image(pdf, blob)

    return bytes(pdf.output())


def _place_image(pdf: _PDF, blob: bytes | None) -> None:
    if not blob:
        pdf.set_text_color(*MUTED)
        pdf.set_font("Dejavu", "", 11)
        pdf.set_x(pdf.l_margin)
        pdf.cell(pdf.w - pdf.l_margin - pdf.r_margin, 10,
                 _rtl("לא צורף מסמך סרוק לשורה זו."), align="R")
        return
    try:
        buf, iw, ih = _normalize_image(blob)
    except Exception:
        pdf.set_text_color(*MUTED)
        pdf.set_x(pdf.l_margin)
        pdf.cell(0, 10, _rtl("לא ניתן להציג את קובץ המסמך."), align="R")
        return
    avail_w = pdf.w - pdf.l_margin - pdf.r_margin
    avail_h = pdf.h - pdf.get_y() - 15
    ratio = min(avail_w / iw, avail_h / ih)
    w, h = iw * ratio, ih * ratio
    x = (pdf.w - w) / 2
    pdf.image(buf, x=x, y=pdf.get_y(), w=w, h=h)


def _normalize_image(blob: bytes):
    """Fix EXIF orientation, flatten to RGB, cap size — keeps the PDF small."""
    from PIL import Image, ImageOps
    im = Image.open(io.BytesIO(blob))
    im = ImageOps.exif_transpose(im)
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    im.thumbnail((1240, 1240))          # smaller → keeps the e-mailed PDF under the relay's ~2 MB cap
    out = io.BytesIO()
    im.save(out, format="JPEG", quality=72, optimize=True)
    out.seek(0)
    return out, im.width, im.height


_STATUS_HE = {"draft": "בהכנה", "pending": "ממתין לאישור",
              "approved": "אושר", "rejected": "נדחה", "compiled": "רוכז"}

# watermark text + colour per status ("still not approved" covers draft/pending)
_WM = {
    "draft":    ("ממתין לאישור", (200, 145, 20)),
    "pending":  ("ממתין לאישור", (200, 145, 20)),
    "rejected": ("נדחה", (200, 45, 45)),
    "approved": ("אושר", (22, 130, 70)),
}


def _fmt_dt(s: str | None) -> str:
    """'YYYY-MM-DD HH:MM:SS' → 'DD/MM/YYYY  HH:MM' (best-effort)."""
    s = (s or "").strip()
    if not s:
        return ""
    try:
        date_part, _, time_part = s.partition(" ")
        y, m, d = date_part.split("-")
        hm = ":".join(time_part.split(":")[:2]) if time_part else ""
        return f"{d}/{m}/{y}" + (f"  {hm}" if hm else "")
    except Exception:
        return s


def _draw_stamp(pdf: _PDF, name: str, when: str) -> None:
    """Rotated green 'approved' stamp with approver + approval time, top-left of page 1."""
    x, y, w, h = pdf.l_margin + 1, 30, 64, 27
    green = (22, 130, 70)
    y0 = pdf.get_y()
    try:
        with pdf.rotation(-7, x + w / 2, y + h / 2):
            pdf.set_draw_color(*green)
            pdf.set_line_width(1.0); pdf.rect(x, y, w, h)
            pdf.set_line_width(0.3); pdf.rect(x + 1.8, y + 1.8, w - 3.6, h - 3.6)
            pdf.set_text_color(*green)

            def centered(txt, yy, size, style="B"):
                pdf.set_font("Dejavu", style, size)
                v = _rtl(txt)
                pdf.text(x + w / 2 - pdf.get_string_width(v) / 2, yy, v)

            centered("אושר ✓", y + 11, 18)
            if name:
                centered(name, y + 18, 10.5)
            if when:
                centered(when, y + 24, 8.5, style="")
    except Exception:
        pass
    pdf.set_line_width(0.2); pdf.set_draw_color(*LINE)
    pdf.set_text_color(*INK); pdf.set_font("Dejavu", "", 11)
    pdf.set_y(y0)
