# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.

"""GDPR Art. 17 deletion requests (launch plan step 5, owner decision „teacher confirms").

A student (or a teacher) files a request with ``POST /me/delete``, which records
``users.deletion_requested_at`` (migration 055 makes the column server-only: a
student can no longer clear or back-date it through PostgREST). The teacher of
the student's classroom lists the open requests (``GET /teacher/deletion-requests``)
and confirms one (``POST /teacher/deletion-requests/{id}/confirm``), which runs the
existing full deletion (``routes/teacher.py::erase_student_account``). The admin
sees every open request and may confirm too, so a request never waits on a
teacher who has left. The requester may withdraw an open request
(``POST /me/delete/withdraw``). Nobody declines one (Art. 17(3)'s exceptions are a
legal judgment, not a button).

Art. 12(3): the request is carried out without undue delay and at the latest one
month after it arrived. ``due_by`` is that date, computed once here.
"""

from __future__ import annotations

import calendar
from datetime import datetime, timezone

# Read by the student SPA, the teacher web and the admin page (Rule §1: German).
NOT_OPEN_DE = (
    "Diese Löschanfrage ist nicht mehr offen – sie wurde zurückgezogen oder bereits ausgeführt."
)
NO_REQUESTER_DE = "Konto nicht gefunden."
ADMIN_CANNOT_REQUEST_DE = (
    "Admin-Konten können sich nicht selbst löschen. Bitte einen anderen Admin darum bitten oder "
    "den Plattform-Betreiber kontaktieren."
)
LIST_FAILED_DE = "Die Löschanfragen konnten nicht geladen werden."
WITHDRAW_FAILED_DE = (
    "Die Löschanfrage konnte nicht zurückgezogen werden — bitte erneut versuchen."
)
WITHDRAWN_DE = (
    "Deine Löschanfrage wurde zurückgezogen. Dein Konto bleibt bestehen. Was beim Stellen der "
    "Anfrage sofort wirksam wurde, bleibt so: abgebrochene Trainings laufen nicht weiter, die "
    "Arbeitsgruppen-Zuordnung ist aufgehoben (deine Lehrkraft kann dich wieder hinzufügen) und "
    "ein entferntes Hugging-Face-Token musst du auf der Startseite neu eingeben."
)
NOTHING_TO_WITHDRAW_DE = "Es gibt keine offene Löschanfrage."


def parse_timestamp(value) -> datetime | None:
    """A PostgREST ``timestamptz`` (ISO 8601, any fraction length, ``Z`` or an
    offset) as an aware datetime; None for anything else."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def one_month_after(moment: datetime) -> datetime:
    """The same day and time one calendar month later, clamped to the last day of
    that month (31 January → 28/29 February), the way Art. 12(3)'s „one month"
    is counted (Regulation 1182/71 Art. 3(2)(c))."""
    year = moment.year + (1 if moment.month == 12 else 0)
    month = 1 if moment.month == 12 else moment.month + 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


def due_by(requested_at) -> str | None:
    """The Art. 12(3) deadline of a request recorded at ``requested_at`` (ISO, UTC),
    or None when the timestamp cannot be read."""
    moment = parse_timestamp(requested_at)
    if moment is None:
        return None
    return one_month_after(moment.astimezone(timezone.utc)).isoformat()
