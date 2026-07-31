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


def _money(n: float) -> str:
    return f"{(n or 0):,.2f}"


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
                     category_totals: list[tuple[str, float]]) -> bytes:
    """`report` needs title/owner_name/department/month/status/total.
    `lines` need seq/supplier/amount/category/invoice_bytes (bytes or None)."""
    pdf = _PDF()
    pdf.add_page()
    W = pdf.w - pdf.l_margin - pdf.r_margin

    # ---- title band ----
    pdf.set_fill_color(*BLUE_D)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Dejavu", "B", 15)
    pdf.set_x(pdf.l_margin)
    pdf.cell(W, 12, _rtl("ארקיע · " + report["title"]), border=0, align="R", fill=True)
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

    # ---- invoice lines table ----
    pdf.set_font("Dejavu", "B", 12)
    pdf.set_text_color(*BLUE_D)
    pdf.set_x(pdf.l_margin)
    pdf.cell(W, 8, _rtl("ריכוז חשבוניות"), align="R")
    pdf.ln(10)

    widths = [W * 0.12, W * 0.44, W * 0.24, W * 0.20]   # מס׳ | ספק | סיווג | סכום
    aligns = ["C", "R", "R", "L"]
    _rtl_row(pdf, ["מס׳", "ספק", "סיווג", "סכום ₪"], widths, aligns, header=True)
    for ln in lines:
        if pdf.get_y() > pdf.h - 25:
            pdf.add_page()
            _rtl_row(pdf, ["מס׳", "ספק", "סיווג", "סכום ₪"], widths, aligns, header=True)
        _rtl_row(pdf, [str(ln["seq"]), ln.get("supplier", ""),
                       ln.get("category", "") or "", _money(ln.get("amount"))],
                 widths, aligns)

    # grand total row
    _rtl_row(pdf, ["", "", "סה\"כ", _money(report.get("total"))],
             widths, ["C", "R", "R", "L"], header=True)
    pdf.ln(6)

    # ---- subtotals per category ----
    if category_totals:
        pdf.set_font("Dejavu", "B", 12)
        pdf.set_text_color(*BLUE_D)
        pdf.set_x(pdf.l_margin)
        pdf.cell(W, 8, _rtl("סיכום לפי סיווג"), align="R")
        pdf.ln(10)
        cw = [W * 0.5, W * 0.5]
        _rtl_row(pdf, ["סיווג", "סכום ₪"], cw, ["R", "L"], header=True)
        for name, amt in category_totals:
            _rtl_row(pdf, [name or "—", _money(amt)], cw, ["R", "L"])

    # ---- one page per invoice image ----
    for ln in lines:
        pdf.add_page()
        pdf.set_fill_color(*BLUE)
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Dejavu", "B", 12)
        pdf.set_x(pdf.l_margin)
        head = f"מסמך מס׳ {ln['seq']}  ·  {ln.get('supplier','')}  ·  ₪ {_money(ln.get('amount'))}"
        pdf.cell(W, 10, _rtl(pdf._fit(head, W - 4, 12)), border=0, align="R", fill=True)
        pdf.ln(14)
        _place_image(pdf, ln.get("invoice_bytes"))

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
    im.thumbnail((1600, 1600))
    out = io.BytesIO()
    im.save(out, format="JPEG", quality=82)
    out.seek(0)
    return out, im.width, im.height


_STATUS_HE = {"draft": "בהכנה", "pending": "ממתין לאישור",
              "approved": "אושר", "rejected": "נדחה", "compiled": "רוכז"}
