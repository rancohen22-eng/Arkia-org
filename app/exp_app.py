# -*- coding: utf-8 -*-
"""Standalone expense-reimbursement service — no org tree.

This entrypoint mounts **only** the expense module (``/exp`` + the token-gated
approver flow + passkeys) plus the shared login gate. It is meant to run as its
own systemd service, on its own port, against its own SQLite database and its own
user list — fully disconnected from the org-chart app (``app.main``).

Separation is achieved without duplicating any code: the same handlers from
:mod:`app.webapp` are reused, only the org route group is left unmounted. Point it
at a private database by exporting ``ARKIA_DB_PATH`` (e.g. ``data/exp.db``) before
launch; give it its own ``.env`` / ``users.txt`` for a separate user list. Run:

    ARKIA_DB_PATH=data/exp.db uvicorn app.exp_app:app --port 8021
"""
from .webapp import create_app

app = create_app(
    include_org=False,
    include_exp=True,
    title="החזרי הוצאות — ארקיע",
    site_name="החזרי הוצאות · ארקיע",
    session_cookie="arkia_exp_session",
    home_path="/exp",
)
