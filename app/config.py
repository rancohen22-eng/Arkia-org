# -*- coding: utf-8 -*-
"""Central configuration for the expense module, read from environment (.env).

Every secret lives here and only here — never in code, templates, or the repo.
All values are optional: with nothing configured the app still runs. OCR falls
back to manual entry, and outgoing mail is written to ``data/outbox/`` instead of
being sent, so the whole flow is exercisable before any credentials exist.
"""
import os
from pathlib import Path

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent
DATA = ROOT / "data"
UPLOADS = DATA / "uploads"      # invoice photos (gitignored, never in the repo)
OUTBOX = DATA / "outbox"        # dry-run mail sink when SMTP isn't configured
FONT_DIR = BASE / "static" / "fonts"


def _b(name: str, default: bool = False) -> bool:
    return os.environ.get(name, "1" if default else "").strip().lower() in ("1", "true", "yes", "on")


# ---- public base URL, for building approver magic-links in e-mails ----
APP_BASE_URL = os.environ.get("APP_BASE_URL", "").rstrip("/")

# ---- mail (OCI Email Delivery relay by default; any SMTP works) ----
SMTP_HOST = os.environ.get("SMTP_HOST", "").strip()
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587") or "587")
SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_STARTTLS = _b("SMTP_STARTTLS", default=True)
SMTP_FROM = os.environ.get("SMTP_FROM", "arkiapnr@gmail.com").strip()
SMTP_FROM_NAME = os.environ.get("SMTP_FROM_NAME", "ארקיע · החזרי הוצאות")


def mail_configured() -> bool:
    return bool(SMTP_HOST and SMTP_FROM)


# ---- OCR (Google Vision by default, via REST API key) ----
OCR_ENGINE = os.environ.get("OCR_ENGINE", "google").strip().lower()
GOOGLE_VISION_API_KEY = os.environ.get("GOOGLE_VISION_API_KEY", "").strip()


def ocr_configured() -> bool:
    return OCR_ENGINE == "google" and bool(GOOGLE_VISION_API_KEY)


# ---- WebAuthn / passkeys (optional feature) ----
WEBAUTHN_RP_ID = os.environ.get("WEBAUTHN_RP_ID", "").strip()          # e.g. "expenses.arkia.co.il"
WEBAUTHN_RP_NAME = os.environ.get("WEBAUTHN_RP_NAME", "ארקיע").strip()
WEBAUTHN_ORIGIN = os.environ.get("WEBAUTHN_ORIGIN", "").strip()        # e.g. "https://expenses.arkia.co.il"


def passkey_enabled() -> bool:
    """Passkeys are on only when the optional lib is installed AND an RP is set."""
    if not (WEBAUTHN_RP_ID and WEBAUTHN_ORIGIN):
        return False
    try:
        import webauthn  # noqa: F401
        return True
    except Exception:
        return False


def ensure_dirs() -> None:
    for d in (DATA, UPLOADS, OUTBOX):
        d.mkdir(parents=True, exist_ok=True)
