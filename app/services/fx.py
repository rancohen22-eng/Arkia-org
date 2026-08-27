# -*- coding: utf-8 -*-
"""Foreign-exchange conversion to shekels, by the expense date.

An expense line may be in a foreign currency (USD/EUR/…). To produce a meaningful
per-category summary and a bottom-line total, each foreign amount is converted to
ILS using the exchange rate **on the day of the expense** (``line_date``).

Rates are fetched on demand from a public provider and cached in ``exp_fx_rates``
keyed by (currency, date), so the same report never re-hits the network and past
reports stay reproducible. Only the standard library is used (``urllib``), like
:mod:`app.services.ocr`. Everything degrades gracefully: if a rate can't be
fetched (offline / provider down) the line is left unconverted and clearly flagged
in the PDF, never silently mis-summed.

Provider: the ECB reference rates via ``api.frankfurter.app`` — a stable, key-less,
historical daily source that includes the shekel and auto-falls-back to the most
recent business day for weekends/holidays.
"""
from __future__ import annotations

import json
import urllib.request
from datetime import date, datetime

from .. import config

_TIMEOUT = 8                        # bound how long a PDF build can wait on the FX provider
_SOURCE = "ECB"                      # provider label stored alongside each cached rate


def _norm_date(s: str) -> str:
    """Coerce free input to 'YYYY-MM-DD'; fall back to today for empty/bad input."""
    s = (s or "").strip()
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").strftime("%Y-%m-%d")
    except Exception:
        return date.today().strftime("%Y-%m-%d")


def rate_to_ils(con, currency: str, when: str):
    """ILS per 1 unit of ``currency`` on ``when`` (an expense date).

    Returns ``1.0`` for ILS/empty, a positive float for a known foreign rate
    (from cache or freshly fetched), or ``None`` when it can't be determined
    (unknown currency, or offline with nothing cached)."""
    cur = (currency or "ILS").strip().upper()
    if cur in ("", "ILS"):
        return 1.0
    if cur not in config.CURRENCIES:
        return None
    d = _norm_date(when)
    row = con.execute(
        "SELECT rate FROM exp_fx_rates WHERE currency=? AND rate_date=?", (cur, d)).fetchone()
    if row and row["rate"]:
        return float(row["rate"])
    got = _fetch(cur, d)
    if got is None:
        return None
    rate, as_of = got
    try:
        con.execute(
            "INSERT OR REPLACE INTO exp_fx_rates (currency, rate_date, rate, as_of, source) "
            "VALUES (?,?,?,?,?)", (cur, d, rate, as_of, _SOURCE))
        con.commit()
    except Exception:
        pass                          # caching is best-effort; still return the rate
    return rate


def _fetch(cur: str, d: str):
    """Fetch ILS-per-1-``cur`` on date ``d`` → (rate, as_of) or None. Never raises."""
    try:
        url = f"https://api.frankfurter.app/{d}?base={cur}&symbols=ILS"
        req = urllib.request.Request(url, headers={"User-Agent": "arkia-exp/1.0"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
        rate = (data.get("rates") or {}).get("ILS")
        if rate is None or float(rate) <= 0:
            return None
        return float(rate), str(data.get("date") or d)
    except Exception:
        return None


def convert_lines(con, lines: list[dict]) -> dict:
    """Convert a report's lines to shekels and build the per-category summary.

    ``lines`` are line dicts (amount, currency, date, category). Returns::

        { 'categories':  [(name, ils_total), ...] sorted desc,   # ILS-converted
          'grand_ils':   float,                                   # converted total
          'rates':       [(cur, as_of, rate), ...],               # rates actually used
          'unconverted': [cur, ...],                              # missing a rate
          'has_foreign': bool }                                   # any non-ILS line?

    A report that is entirely in shekels comes back with ``has_foreign=False`` and
    totals identical to a naive sum, so the PDF stays exactly as before."""
    cat_ils: dict = {}
    order: list = []
    grand = 0.0
    used: dict = {}                   # (cur, as_of) -> rate, de-duplicated for the note
    unconverted: set = set()
    has_foreign = False

    for ln in lines:
        cur = (ln.get("currency") or "ILS").upper()
        amt = float(ln.get("amount") or 0)
        name = ln.get("category") or "—"
        if cur != "ILS":
            has_foreign = True
        rate = rate_to_ils(con, cur, ln.get("date") or "")
        if rate is None:
            unconverted.add(cur)
            continue                  # can't convert → leave out of the ILS total
        if cur != "ILS":
            row = con.execute(
                "SELECT as_of FROM exp_fx_rates WHERE currency=? AND rate_date=?",
                (cur, _norm_date(ln.get("date") or ""))).fetchone()
            used[(cur, row["as_of"] if row else "")] = rate
        ils = amt * rate
        if name not in cat_ils:
            cat_ils[name] = 0.0
            order.append(name)
        cat_ils[name] += ils
        grand += ils

    categories = sorted(((n, cat_ils[n]) for n in order), key=lambda t: t[1], reverse=True)
    rates = sorted(((c, a, r) for (c, a), r in used.items()))
    return {"categories": categories, "grand_ils": grand, "rates": rates,
            "unconverted": sorted(unconverted), "has_foreign": has_foreign}
