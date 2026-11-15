# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""The password rule for every password the API sets (owner decision 2026-11-10).

A NEW or RESET password is at least ``STAFF_MIN_LENGTH`` (12) characters for a
teacher or an admin and ``STUDENT_MIN_LENGTH`` (8) for a student, at most
``MAX_BYTES`` (72) bytes of UTF-8. Passwords set before the rule keep working
(Supabase Auth checks only what it stores).

The upper bound is BYTES, not characters (2026-11-15): Supabase Auth stores a
bcrypt hash, and bcrypt takes at most 72 bytes. The rule said 128 characters
before, and every password of 73–128 bytes (37 umlauts are already 74) made
Supabase Auth panic („bcrypt: password length exceeds 72 bytes") and the route
answer its generic 500 — measured against GoTrue v2.188.1. An ä, ö, ü or ß is
two bytes, most other non-ASCII characters two to four, which the sentence
says. The check runs by hand, not as a pydantic
``min_length``: pydantic's 422 is English and echoes the password back.

Callers: ``routes/admin.py`` (teacher create + reset) and ``routes/teacher.py``
(student create + reset). ``scripts/bootstrap_admin.py`` (the first admin) and
the teacher web (``physical_ai_manager/src/utils/passwordRules.js``) carry the
same numbers; ``tests/test_password_rule_lockstep.py`` holds all three equal.

Supabase Auth may refuse a password itself (its project-wide minimum, or the
leaked-password check on plans that have it): ``is_weak_password_error``
recognises that refusal so the routes answer ``WEAK_PASSWORD_DE`` instead of
a bare 500.
"""

from __future__ import annotations

STAFF_MIN_LENGTH = 12
STUDENT_MIN_LENGTH = 8
MAX_BYTES = 72
STAFF_ROLES = frozenset({"teacher", "admin"})

WEAK_PASSWORD_DE = (
    "Dieses Passwort wird nicht akzeptiert: Es ist zu leicht zu erraten oder in einem "
    "bekannten Datenleck aufgetaucht. Bitte ein anderes Passwort wählen."
)
TOO_LONG_DE = (
    f"Das Passwort ist zu lang: höchstens {MAX_BYTES} Zeichen, wobei ä, ö, ü, ß und "
    "andere Sonderzeichen mehrfach zählen."
)
# A str the JSON body can carry but UTF-8 cannot (a lone surrogate, "\ud800"):
# no browser types one; refused in German instead of a 500 from .encode().
INVALID_CHARACTER_DE = "Das Passwort enthält ein Zeichen, das nicht erlaubt ist."


def min_length_for(role: str) -> int:
    return STAFF_MIN_LENGTH if role in STAFF_ROLES else STUDENT_MIN_LENGTH


def too_short_de(role: str) -> str:
    who = "Lehrkräfte und Admins" if role in STAFF_ROLES else "Schülerinnen und Schüler"
    return f"Das Passwort muss für {who} mindestens {min_length_for(role)} Zeichen lang sein."


def password_error_de(password, role: str) -> str | None:
    """The German refusal of a password for an account of ``role``, else None."""
    if not isinstance(password, str) or len(password) < min_length_for(role):
        return too_short_de(role)
    try:
        size = len(password.encode("utf-8"))
    except UnicodeEncodeError:
        return INVALID_CHARACTER_DE
    if size > MAX_BYTES:
        return TOO_LONG_DE
    return None


def is_weak_password_error(exc) -> bool:
    """Supabase Auth's own refusal of a password (gotrue's
    ``AuthWeakPasswordError``, code ``weak_password``)."""
    return getattr(exc, "code", None) == "weak_password" or type(exc).__name__ == "AuthWeakPasswordError"
