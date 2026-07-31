# -*- coding: utf-8 -*-
"""Passkey / WebAuthn login (optional feature).

Lets a user sign in with the device's Face ID / fingerprint instead of a
password. It is entirely opt-in: the ``webauthn`` library is imported lazily, so
the app runs normally when it isn't installed, and the feature only switches on
when an RP id + origin are configured (see :func:`app.config.passkey_enabled`).

Only public keys and signature counters are stored — never biometric data; that
never leaves the user's device. Credentials live in the ``exp_webauthn`` table.
"""
from __future__ import annotations

import hashlib
import os

from .. import config


def available() -> bool:
    return config.passkey_enabled()


def _user_id(username: str) -> bytes:
    # a stable, non-reversible handle per user (WebAuthn user.id)
    return hashlib.sha256(("arkia:" + username.lower()).encode()).digest()


def get_credentials(con, username: str) -> list:
    return con.execute(
        "SELECT * FROM exp_webauthn WHERE username=? ORDER BY id",
        ((username or "").strip().lower(),)).fetchall()


def has_credentials(con, username: str) -> bool:
    return bool(get_credentials(con, username))


def delete_credential(con, username: str, cred_row_id: int) -> None:
    con.execute("DELETE FROM exp_webauthn WHERE id=? AND username=?",
                (cred_row_id, (username or "").strip().lower()))
    con.commit()


# ---- registration ceremony ----

def registration_options(con, username: str, display_name: str = "") -> tuple[str, str]:
    """Return (options_json_for_browser, challenge_b64_to_stash_in_session)."""
    import webauthn
    from webauthn.helpers import bytes_to_base64url, base64url_to_bytes
    from webauthn.helpers.structs import (
        PublicKeyCredentialDescriptor, AuthenticatorSelectionCriteria,
        ResidentKeyRequirement, UserVerificationRequirement)

    challenge = os.urandom(32)
    exclude = [PublicKeyCredentialDescriptor(id=base64url_to_bytes(c["credential_id"]))
               for c in get_credentials(con, username)]
    opts = webauthn.generate_registration_options(
        rp_id=config.WEBAUTHN_RP_ID,
        rp_name=config.WEBAUTHN_RP_NAME,
        user_name=username,
        user_id=_user_id(username),
        user_display_name=display_name or username,
        challenge=challenge,
        exclude_credentials=exclude,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED),
    )
    return webauthn.options_to_json(opts), bytes_to_base64url(challenge)


def verify_registration(con, username: str, credential_json: str,
                        challenge_b64: str, label: str = "") -> None:
    import webauthn
    from webauthn.helpers import base64url_to_bytes, bytes_to_base64url

    v = webauthn.verify_registration_response(
        credential=credential_json,
        expected_challenge=base64url_to_bytes(challenge_b64),
        expected_rp_id=config.WEBAUTHN_RP_ID,
        expected_origin=config.WEBAUTHN_ORIGIN,
    )
    con.execute(
        "INSERT INTO exp_webauthn (username, credential_id, public_key, sign_count, label) "
        "VALUES (?, ?, ?, ?, ?)",
        ((username or "").strip().lower(), bytes_to_base64url(v.credential_id),
         bytes_to_base64url(v.credential_public_key), v.sign_count, label or ""))
    con.commit()


# ---- authentication ceremony ----

def authentication_options(con, username: str) -> tuple[str, str] | None:
    """Return (options_json, challenge_b64), or None if the user has no passkey."""
    import webauthn
    from webauthn.helpers import bytes_to_base64url, base64url_to_bytes
    from webauthn.helpers.structs import (
        PublicKeyCredentialDescriptor, UserVerificationRequirement)

    creds = get_credentials(con, username)
    if not creds:
        return None
    challenge = os.urandom(32)
    allow = [PublicKeyCredentialDescriptor(id=base64url_to_bytes(c["credential_id"]))
             for c in creds]
    opts = webauthn.generate_authentication_options(
        rp_id=config.WEBAUTHN_RP_ID,
        challenge=challenge,
        allow_credentials=allow,
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    return webauthn.options_to_json(opts), bytes_to_base64url(challenge)


def verify_authentication(con, username: str, credential: dict,
                          challenge_b64: str) -> bool:
    import json
    import webauthn
    from webauthn.helpers import base64url_to_bytes

    cred_id = credential.get("id") or credential.get("rawId")
    row = con.execute(
        "SELECT * FROM exp_webauthn WHERE credential_id=? AND username=?",
        (cred_id, (username or "").strip().lower())).fetchone()
    if row is None:
        return False
    try:
        v = webauthn.verify_authentication_response(
            credential=json.dumps(credential),
            expected_challenge=base64url_to_bytes(challenge_b64),
            expected_rp_id=config.WEBAUTHN_RP_ID,
            expected_origin=config.WEBAUTHN_ORIGIN,
            credential_public_key=base64url_to_bytes(row["public_key"]),
            credential_current_sign_count=row["sign_count"],
        )
    except Exception:
        return False
    con.execute("UPDATE exp_webauthn SET sign_count=? WHERE id=?",
                (v.new_sign_count, row["id"]))
    con.commit()
    return True
