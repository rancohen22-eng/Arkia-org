# -*- coding: utf-8 -*-
"""Authentication for the Arkia org-chart system.

Users are read from (in priority order):
  1. the ARKIA_USERS environment variable  "user1:pass1,user2:pass2"
  2. a users.txt file in the project root   (one  user:password  per line)

Passwords are plain text by default (simple, internal tool) but a line may
instead store a PBKDF2 hash produced by hash_password() — prefix "pbkdf2$".
Never commit users.txt or the secret key: both are covered by .gitignore.
"""
import base64
import hashlib
import hmac
import os
import secrets
from pathlib import Path

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent
USERS_FILE = ROOT / "users.txt"
SECRET_FILE = ROOT / "data" / "secret_key.txt"

# usernames allowed to manage things (reserved for future admin-only screens)
ADMIN_USERS = {u.strip().lower() for u in os.environ.get("ADMIN_USERS", "ranc").split(",") if u.strip()}

_PBKDF2_ITERS = 200_000


def is_admin(username: str) -> bool:
    return (username or "").strip().lower() in ADMIN_USERS


def load_users() -> dict[str, str]:
    """Return {username_lower: stored_password}. Env var wins over the file."""
    users: dict[str, str] = {}
    env = os.environ.get("ARKIA_USERS", "").strip()
    if env:
        for pair in env.split(","):
            if ":" in pair:
                u, p = pair.split(":", 1)
                if u.strip():
                    users[u.strip().lower()] = p.strip()
        return users
    if USERS_FILE.exists():
        for line in USERS_FILE.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            u, p = line.split(":", 1)
            if u.strip():
                users[u.strip().lower()] = p.strip()
    return users


def hash_password(password: str) -> str:
    """Produce a 'pbkdf2$<iters>$<salt_b64>$<hash_b64>' string (for hashed users.txt)."""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERS)
    b = lambda x: base64.b64encode(x).decode()
    return f"pbkdf2${_PBKDF2_ITERS}${b(salt)}${b(dk)}"


def _verify_pbkdf2(stored: str, password: str) -> bool:
    try:
        _tag, iters, salt_b64, hash_b64 = stored.split("$", 3)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(iters))
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


def verify(username: str, password: str) -> bool:
    """Constant-time credential check against the configured users."""
    stored = load_users().get((username or "").strip().lower())
    if stored is None:
        hmac.compare_digest(password, password)
        return False
    if stored.startswith("pbkdf2$"):
        return _verify_pbkdf2(stored, password)
    return hmac.compare_digest(stored, password or "")


def get_secret_key() -> str:
    """Stable secret for signing session cookies.

    Uses SECRET_KEY env var if set (cloud); otherwise a random key persisted to
    data/secret_key.txt so local sessions survive restarts. Never committed.
    """
    env = os.environ.get("SECRET_KEY", "").strip()
    if env:
        return env
    if SECRET_FILE.exists():
        val = SECRET_FILE.read_text(encoding="utf-8").strip()
        if val:
            return val
    key = secrets.token_hex(32)
    SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    SECRET_FILE.write_text(key, encoding="utf-8")
    return key
