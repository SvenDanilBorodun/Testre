"""A student's own Hugging Face token, stored with the account (migration 042).

The student pastes the token once on the Startseite. This router proves it with
the Hub (`whoami`), refuses read-only tokens and fine-grained tokens that cannot
write to the student's own repositories, stores it AES-256-GCM encrypted in
the service-role-only `user_hf_credentials` table (the proven account name also
becomes `users.hf_username`, atomically, inside the `store_user_hf_credential`
RPC) and hands the plaintext back ONLY to its owner, through `POST /reveal`, so
the SPA can relay it to the robot.

Conventions that are easy to undo by accident (see CLAUDE.md, Cloud stack):
  * Token-semantics errors are 422, never 401/403: the SPA signs the student out
    on those from `/me`. 502 = Hub unreachable, 503 = no encryption key on this
    server, 404 = nothing stored.
  * `PUT` takes its body as an untyped `Body(default=None)` and reads the token by
    hand: FastAPI's default 422 echoes the offending `input`, i.e. the token, for
    ANY typed model and for a non-object body (a JSON string or array, text/plain).
    The shape is checked by hand, and no handler formats, logs or raises the
    token, its fingerprint or its ciphertext.
  * Plain `def` handlers (the Hub call blocks for up to HUB_TIMEOUT_S, so they
    belong in the threadpool, like routes/datasets.py).
  * Every success carries `Cache-Control: no-store`. No route takes a user id:
    the row is always the JWT-verified caller's own, so there is no IDOR to
    assert (Rule §4 holds by construction).
  * Every German `detail=` literal is bound at its raise site on purpose:
    `german_detail_lint.py` only reads keyword arguments.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import JSONResponse

from app.auth import get_current_profile
from app.services import hf_credentials
from app.services.hf_identity import is_denied_author, normalize_hf_username
from app.services.supabase_client import get_supabase

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/me/hf-token", tags=["hf-token"])

_TABLE = "user_hf_credentials"
_COLUMNS_STATUS = "token_ciphertext, token_fp, token_hint, hf_username, token_role, validated_at"
_COLUMNS_SECRET = "token_ciphertext, token_fp"


def _ok(content: dict) -> JSONResponse:
    return JSONResponse(content=content, headers={"Cache-Control": "no-store"})


def _key_missing() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail="Die Token-Speicherung ist auf diesem Server noch nicht eingerichtet.",
    )


def _undecryptable() -> HTTPException:
    return HTTPException(
        status_code=422,
        detail="Dein gespeichertes Token kann nicht mehr entschlüsselt werden. Bitte speichere es neu.",
    )


def _status_unreadable() -> HTTPException:
    return HTTPException(
        status_code=500, detail="Der Token-Status konnte nicht geladen werden."
    )


def _require_student(profile) -> None:
    if profile["role"] != "student":
        raise HTTPException(
            status_code=403,
            detail="Nur Schüler können ein Hugging-Face-Token hinterlegen.",
        )


def _require_key() -> None:
    if not hf_credentials.configured():
        raise _key_missing()


def _require_shape(token) -> None:
    if not hf_credentials.valid_shape(token):
        raise HTTPException(
            status_code=422,
            detail=(
                "Das sieht nicht nach einem Hugging-Face-Token aus. Ein Token "
                "beginnt mit „hf_“ und enthält keine Leerzeichen."
            ),
        )


def _read_rows(uid: str, columns: str) -> list:
    """The caller's credential row as a list (`user_id` is the primary key, so
    at most one). A database failure is a German 500 that names no cause."""
    try:
        return get_supabase().table(_TABLE).select(columns).eq("user_id", uid).execute().data or []
    except Exception as exc:  # noqa: BLE001 - never log str(exc)
        logger.error("hf-token read failed user=%s: %s", uid, type(exc).__name__)
        raise _status_unreadable() from None


def _read_secret_row(uid: str) -> dict:
    rows = _read_rows(uid, _COLUMNS_SECRET)
    if not rows:
        raise HTTPException(status_code=404, detail="Es ist kein Hugging-Face-Token hinterlegt.")
    return rows[0]


def _decrypt_own(uid: str, row: dict) -> str:
    """Decrypt the caller's stored token and prove it is the one the stored
    fingerprint was computed from (a mismatch is an inconsistent row, never a
    token to hand out)."""
    try:
        token = hf_credentials.decrypt(uid, row["token_ciphertext"])
    except hf_credentials.HfCredentialError as exc:
        if exc.code == "key_missing":
            raise _key_missing() from None
        raise _undecryptable() from None
    if hf_credentials.fingerprint(token) != row["token_fp"]:
        raise _undecryptable()
    return token


def _hub_checks(token: str, profile, *, stored: bool = False):
    """Ask the Hub who owns `token` and apply the account rules.

    Returns (name, role, account_changed). `stored` only picks the sentence for
    a Hub refusal (a token saved earlier vs. one just pasted).
    """
    try:
        info = hf_credentials.validate_with_hub(token)
    except hf_credentials.HfCredentialError as exc:
        if exc.code == "rejected" and stored:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Hugging Face hat dein gespeichertes Token abgelehnt "
                    "(widerrufen oder abgelaufen). Bitte speichere ein neues."
                ),
            ) from None
        if exc.code == "rejected":
            raise HTTPException(
                status_code=422,
                detail=(
                    "Hugging Face hat dieses Token abgelehnt. Prüfe, ob du es "
                    "vollständig kopiert hast und ob es nicht widerrufen wurde."
                ),
            ) from None
        raise HTTPException(
            status_code=502,
            detail="Hugging Face ist gerade nicht erreichbar. Bitte versuche es gleich noch einmal.",
        ) from None

    name = normalize_hf_username(info["name"])
    if name is None:
        raise HTTPException(status_code=422, detail="Der Kontoname dieses Tokens ist ungültig.")
    if is_denied_author(name):
        raise HTTPException(
            status_code=422,
            detail="Dieses Konto ist nicht zulässig (System- oder Beispielkonto).",
        )
    # Deny-only: every role other than "read" (an unknown one included) passes,
    # except a fine-grained token whose scopes PROVABLY grant no write to the
    # student's own repositories (hf_credentials.fine_grained_repo_write: an
    # unrecognised scope block is None and passes - fail open).
    if info["role"] == "read":
        raise HTTPException(
            status_code=422,
            detail=(
                "Dieses Token hat nur Leserechte. Zum Hochladen braucht EduBotics "
                "ein Token mit Schreibrechten. Erstelle auf huggingface.co unter "
                "„Settings“ und „Access Tokens“ ein neues Token vom Typ „Write“ "
                "und füge es hier ein."
            ),
        )
    if info["role"] == hf_credentials.FINE_GRAINED_ROLE and info.get("repo_write") is False:
        raise HTTPException(
            status_code=422,
            detail=(
                "Dieses Token darf nicht in deine eigenen Repositories schreiben. "
                "Zum Hochladen braucht EduBotics Schreibrechte. Erstelle auf "
                "huggingface.co unter „Settings“ und „Access Tokens“ ein neues "
                "Token vom Typ „Write“ – oder erlaube bei deinem Token unter "
                "„Repositories“ den Schreibzugriff („Write access“) auf alle "
                "Repositories in deinem eigenen Konto – und füge es hier ein."
            ),
        )
    previous = profile.get("hf_username")
    account_changed = bool(previous) and previous.lower() != name.lower()
    return name, info["role"], account_changed


def _store(uid: str, token: str, name: str, role: str, account_changed: bool) -> JSONResponse:
    """Encrypt under the CURRENT key and write through the one RPC."""
    try:
        ciphertext = hf_credentials.encrypt(uid, token)
    except hf_credentials.HfCredentialError:
        raise _key_missing() from None
    fp = hf_credentials.fingerprint(token)
    hint = hf_credentials.hint(token)
    try:
        res = get_supabase().rpc(
            "store_user_hf_credential",
            {
                "p_user_id": uid,
                "p_ciphertext": ciphertext,
                "p_fp": fp,
                "p_hint": hint,
                "p_hf_username": name,
                "p_role": role,
            },
        ).execute()
    except Exception as exc:  # noqa: BLE001 - classified below; never log str(exc)
        if "P0002" in str(exc) or "Benutzer nicht gefunden" in str(exc):
            raise HTTPException(status_code=404, detail="Benutzerkonto nicht gefunden.") from None
        logger.error("hf-token store failed user=%s: %s", uid, type(exc).__name__)
        raise HTTPException(
            status_code=500, detail="Das Token konnte nicht gespeichert werden."
        ) from None
    data = getattr(res, "data", None)
    validated_at = data.get("validated_at") if isinstance(data, dict) else None
    return _ok(
        {
            "stored": True,
            "usable": True,
            "hf_username": name,
            "hint": hint,
            "fp": fp,
            "role": role,
            "validated_at": validated_at or datetime.now(timezone.utc).isoformat(),
            "account_changed": account_changed,
        }
    )


@router.get("")
def get_hf_token_status(profile=Depends(get_current_profile)):
    """Status only (never the secret): is a token stored, for which account, and
    can this server's key still decrypt it."""
    _require_student(profile)
    _require_key()
    rows = _read_rows(str(profile["id"]), _COLUMNS_STATUS)
    if not rows:
        return _ok(
            {
                "stored": False,
                "usable": False,
                "hf_username": None,
                "hint": None,
                "fp": None,
                "role": None,
                "validated_at": None,
            }
        )
    row = rows[0]
    return _ok(
        {
            "stored": True,
            "usable": hf_credentials.usable(row["token_ciphertext"]),
            "hf_username": row["hf_username"],
            "hint": row["token_hint"],
            "fp": row["token_fp"],
            "role": row["token_role"],
            "validated_at": row["validated_at"],
        }
    )


@router.put("")
def put_hf_token(body: Any = Body(default=None), profile=Depends(get_current_profile)):
    """Prove the pasted token with the Hub, then store it encrypted."""
    _require_student(profile)
    _require_key()
    token = body.get("token") if isinstance(body, dict) else None
    _require_shape(token)
    name, role, account_changed = _hub_checks(token, profile)
    uid = str(profile["id"])
    response = _store(uid, token, name, role, account_changed)
    logger.info("hf-token stored user=%s", uid)
    return response


@router.delete("")
def delete_hf_token(profile=Depends(get_current_profile)):
    """Remove the stored token. Idempotent, and it works with no key configured
    (a student must always be able to take their token away). The proven
    `users.hf_username` stays: it is an ordinary dataset anchor from here on."""
    _require_student(profile)
    uid = str(profile["id"])
    try:
        get_supabase().table(_TABLE).delete().eq("user_id", uid).execute()
    except Exception as exc:  # noqa: BLE001
        logger.error("hf-token delete failed user=%s: %s", uid, type(exc).__name__)
        raise HTTPException(
            status_code=500,
            detail="Das Token konnte nicht entfernt werden. Bitte versuche es noch einmal.",
        ) from None
    logger.info("hf-token deleted user=%s", uid)
    return _ok({"stored": False})


@router.post("/reveal")
def reveal_hf_token(profile=Depends(get_current_profile)):
    """The plaintext, to its owner only, so the SPA can relay it to the robot.
    No body and no id parameter: it can only ever be the caller's own token."""
    _require_student(profile)
    _require_key()
    uid = str(profile["id"])
    row = _read_secret_row(uid)
    token = _decrypt_own(uid, row)
    logger.info("hf-token reveal user=%s", uid)
    return _ok({"token": token, "fp": row["token_fp"]})


@router.post("/verify")
def verify_hf_token(profile=Depends(get_current_profile)):
    """Re-ask the Hub about the stored token ("Erneut prüfen"): refreshes
    `validated_at` and re-encrypts it under the CURRENT key (the way a rotated
    key gets adopted, token by token)."""
    _require_student(profile)
    _require_key()
    uid = str(profile["id"])
    row = _read_secret_row(uid)
    token = _decrypt_own(uid, row)
    name, role, account_changed = _hub_checks(token, profile, stored=True)
    # The Hub call above takes up to HUB_TIMEOUT_S. A student who removed the token
    # or saved another one meanwhile must not get the OLD one written back by the
    # blind upsert in _store: re-read the fingerprint and stand down on a change.
    current = _read_rows(uid, "token_fp")
    if not current or current[0].get("token_fp") != row.get("token_fp"):
        raise HTTPException(
            status_code=409,
            detail="Dein Token wurde inzwischen geändert oder entfernt. Bitte lade die Seite neu.",
        )
    response = _store(uid, token, name, role, account_changed)
    logger.info("hf-token verified user=%s", uid)
    return response
