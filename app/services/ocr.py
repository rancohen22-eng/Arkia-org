# -*- coding: utf-8 -*-
"""Invoice OCR — reads supplier name + amount from a photographed receipt.

Pluggable by design: the engine is chosen in ``config.OCR_ENGINE`` (default
``google`` → Google Cloud Vision via its REST API with an API key). If no key is
configured the module degrades gracefully — it returns an empty guess so the user
just types the two fields by hand, and the rest of the flow is unaffected.

Only the standard library is used (``urllib``), so nothing extra is installed for
OCR. Swapping in another engine (Claude/Azure/Tesseract) means adding one branch
to :func:`extract_invoice`.
"""
from __future__ import annotations

import base64
import json
import re
import urllib.request
from dataclasses import dataclass, asdict

from .. import config


@dataclass
class OcrResult:
    supplier: str = ""
    amount: float | None = None
    raw: str = ""
    engine: str = "none"

    def as_dict(self) -> dict:
        return asdict(self)


def extract_invoice(image_bytes: bytes) -> OcrResult:
    """Best-effort {supplier, amount} from an invoice image. Never raises for a
    bad image or a missing key — returns an empty result for manual entry."""
    if config.OCR_ENGINE == "google" and config.GOOGLE_VISION_API_KEY:
        try:
            text = _google_vision_text(image_bytes)
            return _parse_receipt(text, engine="google")
        except Exception:
            # a network/API hiccup must not block the user — fall through to manual
            return OcrResult(engine="google-error")
    return OcrResult(engine="none")


# ---- Google Cloud Vision REST (DOCUMENT_TEXT_DETECTION) ----

def _google_vision_text(image_bytes: bytes) -> str:
    url = ("https://vision.googleapis.com/v1/images:annotate?key="
           + config.GOOGLE_VISION_API_KEY)
    payload = {
        "requests": [{
            "image": {"content": base64.b64encode(image_bytes).decode()},
            "features": [{"type": "DOCUMENT_TEXT_DETECTION"}],
            "imageContext": {"languageHints": ["he", "en"]},
        }]
    }
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())
    r = (data.get("responses") or [{}])[0]
    return (r.get("fullTextAnnotation") or {}).get("text", "") or ""


# ---- heuristics: turn raw receipt text into a supplier + amount guess ----

# numbers like 1,234.56 / 1234 / 12.90 — capture the amount, drop thousands commas
_AMOUNT_RE = re.compile(r"(?<!\d)(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?(?!\d)")
# lines that signal the grand total — we prefer amounts near these words
_TOTAL_HINTS = ("סה\"כ", "סה״כ", "סהכ", "לתשלום", "total", "amount due", "סכום")


def _to_float(whole: str, frac: str | None) -> float:
    val = whole.replace(",", "")
    return float(val + ("." + frac if frac else ""))


def _parse_receipt(text: str, engine: str) -> OcrResult:
    if not text.strip():
        return OcrResult(engine=engine)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    # supplier: first line that looks like a business name (has letters, not just
    # digits/punctuation, and isn't an obvious header like a date or receipt no.)
    supplier = ""
    for ln in lines[:6]:
        letters = re.sub(r"[\d\s.,:/\\\-#*]", "", ln)
        if len(letters) >= 2 and not re.search(r"חשבונית|קבלה|מס['׳\"]?\s*עוסק|ח\.פ", ln):
            supplier = ln
            break
    if not supplier and lines:
        supplier = lines[0]

    # amount: prefer the largest number on a line mentioning a total; otherwise the
    # largest number overall (the grand total is nearly always the biggest figure).
    def amounts_in(s: str) -> list[float]:
        return [_to_float(m.group(1), m.group(2)) for m in _AMOUNT_RE.finditer(s)]

    total_line_amounts: list[float] = []
    for ln in lines:
        if any(h in ln.lower() for h in (h.lower() for h in _TOTAL_HINTS)):
            total_line_amounts += amounts_in(ln)
    all_amounts = amounts_in(text)
    pool = total_line_amounts or all_amounts
    amount = max(pool) if pool else None

    return OcrResult(supplier=supplier[:120], amount=amount, raw=text, engine=engine)
