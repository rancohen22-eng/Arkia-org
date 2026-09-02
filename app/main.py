# -*- coding: utf-8 -*-
"""Arkia org-chart system — full internal app (org tree + expenses).

A self-propagating organisation tree: each manager fills only their own direct
reports, and every manager gets a secret magic-link (shared over WhatsApp) so the
tree builds itself top-down. The expense-reimbursement module is mounted under
``/exp``. Admin screens require login; the manager "fill" and approver pages are
reached by token only (no login).

The route handlers live in :mod:`app.webapp`; this module is just the entrypoint
that assembles the full app. For the expense module as a standalone service (no
org tree, own DB/port), see :mod:`app.exp_app`.
"""
from .webapp import create_app

app = create_app(
    include_org=True,
    include_exp=True,
    title="עץ ארגוני — ארקיע",
    site_name="עץ ארגוני · ארקיע",
    session_cookie="arkia_org_session",
    home_path="/",
)
