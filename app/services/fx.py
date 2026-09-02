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

Provider order:
  1. **Bank of Israel** representative rate (השער היציג) — the official Israeli rate
     used for accounting/tax, via the BOI data portal's SDMX API.
  2. **ECB** reference rates via ``api.frankfurter.app`` — a stable, key-less
     fallback used only when the BOI rate can't be obtained.

Each cached rate records which source produced it, and the PDF note names it, so a
report always says whether it used the official שער יציג or the ECB fallback. For a
per-100-unit quirk (e.g. JPY) the BOI value is sanity-checked against ECB and
rescaled if it is off by ~100×.
"""
from __future__ import annotations

import json
import urllib.request
from datetime import date, datetime, timedelta

from .. import config

_TIMEOUT = 7                        # per request; bounds how long a PDF build waits on a provider

# provider labels stored in exp_fx_rates.source and mapped to Hebrew in the PDF note
SRC_BOI = "BOI"
SRC_ECB = "ECB"


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
    rate, as_of, source = got
    try:
        con.execute(
            "INSERT OR REPLACE INTO exp_fx_rates (currency, rate_date, rate, as_of, source) "
            "VALUES (?,?,?,?,?)", (cur, d, rate, as_of, source))
        con.commit()
    except Exception:
        pass                          # caching is best-effort; still return the rate
    return rate


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={
        "User-Agent": "arkia-exp/1.0",
        "Accept": "application/vnd.sdmx.data+json, application/json",
    })
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read().decode())


def _fetch(cur: str, d: str):
    """ILS-per-1-``cur`` on date ``d`` → (rate, as_of, source) or None. Never raises.

    Bank of Israel (official שער יציג) is tried first; ECB is the fallback."""
    boi = _boi(cur, d)
    if boi is not None:
        rate, as_of = boi
        # JPY (and any per-100-quoted currency) sanity-check against ECB: rescale if
        # the BOI value is off by ~100× so we never emit a 100×-wrong figure.
        if cur == "JPY":
            ecb = _frankfurter(cur, d)
            if ecb and ecb[0] > 0:
                ratio = rate / ecb[0]
                if ratio > 20:
                    rate /= 100.0
                elif ratio < 0.05:
                    rate *= 100.0
        return rate, as_of, SRC_BOI
    ecb = _frankfurter(cur, d)
    if ecb is not None:
        return ecb[0], ecb[1], SRC_ECB
    return None


def _frankfurter(cur: str, d: str):
    """ECB reference rate ILS-per-1-``cur`` on ``d`` → (rate, as_of) or None."""
    try:
        data = _get_json(f"https://api.frankfurter.app/{d}?base={cur}&symbols=ILS")
        rate = (data.get("rates") or {}).get("ILS")
        if rate is None or float(rate) <= 0:
            return None
        return float(rate), str(data.get("date") or d)
    except Exception:
        return None


# Bank of Israel data portal (SDMX). The EXR dataflow's representative-rate series
# for a currency is 'RER_<CUR>_ILS'. We ask a small window ending on the expense
# date and take the latest business-day observation on or before it (BOI has no
# rate on weekends/holidays), matching how שער יציג is applied in practice.
_BOI_HOSTS = ("edge.boi.gov.il", "edge.boi.org.il")


def _boi(cur: str, d: str):
    """Bank of Israel representative rate ILS-per-1-``cur`` on/just-before ``d``
    → (rate, as_of) or None. Never raises."""
    try:
        start = (datetime.strptime(d, "%Y-%m-%d") - timedelta(days=12)).strftime("%Y-%m-%d")
    except Exception:
        start = d
    path = (f"/FusionEdgeServer/sdmx/v2/data/dataflow/BOI.STATISTICS/EXR/1.0/"
            f"RER_{cur}_ILS?startPeriod={start}&endPeriod={d}")
    for host in _BOI_HOSTS:
        try:
            js = _get_json(f"https://{host}{path}")
            got = _parse_sdmx(js, d)
            if got is not None:
                return got
        except Exception:
            continue
    return None


def _parse_sdmx(js: dict, target: str):
    """Extract (rate, as_of) — the latest observation with date ≤ ``target`` — from
    an SDMX-JSON payload. Tolerant of the 1.0 (top-level) and 2.0 (nested) shapes.
    Returns None on any structural mismatch so the caller falls back to ECB."""
    try:
        data = js.get("data") if isinstance(js.get("data"), dict) else js
        datasets = data.get("dataSets") or js.get("dataSets")
        struct = data.get("structure") or js.get("structure")
        if struct is None:
            structs = data.get("structures") or js.get("structures")
            struct = structs[0] if structs else None
        if isinstance(struct, list):
            struct = struct[0]
        if not datasets or not struct:
            return None
        obs_dim = struct["dimensions"]["observation"][0]["values"]  # [{"id": "YYYY-MM-DD"}, ...]
        series = datasets[0].get("series")
        if series:
            observations = next(iter(series.values()))["observations"]
        else:                                   # flat (no series dimension) layout
            observations = datasets[0]["observations"]
        pairs = []
        for idx, arr in observations.items():
            try:
                day = obs_dim[int(idx)]["id"]
                val = float(arr[0])
            except Exception:
                continue
            if val > 0 and str(day) <= target:
                pairs.append((str(day), val))
        if not pairs:
            return None
        pairs.sort()
        return pairs[-1][1], pairs[-1][0]       # latest date ≤ target
    except Exception:
        return None


def convert_lines(con, lines: list[dict]) -> dict:
    """Convert a report's lines to shekels and build the per-category summary.

    ``lines`` are line dicts (amount, currency, date, category). Returns::

        { 'categories':  [(name, ils_total), ...] sorted desc,   # ILS-converted
          'grand_ils':   float,                                   # converted total
          'rates':       [(cur, as_of, rate, source), ...],       # rates actually used
          'unconverted': [cur, ...],                              # missing a rate
          'has_foreign': bool }                                   # any non-ILS line?

    A report that is entirely in shekels comes back with ``has_foreign=False`` and
    totals identical to a naive sum, so the PDF stays exactly as before."""
    cat_ils: dict = {}
    order: list = []
    grand = 0.0
    used: dict = {}                   # (cur, as_of, source) -> rate, de-duplicated for the note
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
                "SELECT as_of, source FROM exp_fx_rates WHERE currency=? AND rate_date=?",
                (cur, _norm_date(ln.get("date") or ""))).fetchone()
            used[(cur, row["as_of"] if row else "", row["source"] if row else "")] = rate
        ils = amt * rate
        if name not in cat_ils:
            cat_ils[name] = 0.0
            order.append(name)
        cat_ils[name] += ils
        grand += ils

    categories = sorted(((n, cat_ils[n]) for n in order), key=lambda t: t[1], reverse=True)
    rates = sorted(((c, a, r, src) for (c, a, src), r in used.items()))
    return {"categories": categories, "grand_ils": grand, "rates": rates,
            "unconverted": sorted(unconverted), "has_foreign": has_foreign}
