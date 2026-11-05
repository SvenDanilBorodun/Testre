"""German sentences of the training-hour ledger (migration 052, spec §8.1), defined ONCE.

Every sentence a student, teacher or admin reads about training hours lives
here; the routes pass them as NAMES (``detail=NO_HOURS_DE``), so
``.github/scripts/german_detail_lint.py`` cannot see them. The umlaut and
language rules are fenced by ``app/tests/test_credit_texts_de.py`` (the lint's
own TRANSLITERATIONS / GERMAN_CHARS / GERMAN_WORDS / ENGLISH_WORDS over every
constant). The managed-training S-sentences stay in ``training_texts_de``.

No sentence here puts a verb or an inflected adjective next to an hour count:
a count is always the noun phrase of ``hours_de`` (review 2 m7), and the React
twin ``physical_ai_manager/src/utils/hoursDe.js::hoursDe`` is held to the same
case table (``src/utils/__tests__/hoursDe.cases.json``, ``app/tests/test_hours_de.py``).

stdlib only, imports nothing from the app (the tests load it by path).
"""

from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal

ROLE_REFUSED_DE = "Nur Schülerinnen und Schüler können Trainings starten."
POLICY_REFUSED_DE = "Nur ACT-Modelle können in der Cloud trainiert werden."
NO_HOURS_DE = "Keine freien Trainingsstunden mehr. Frag deine Lehrkraft nach weiteren Stunden."
MEMBER_LIMIT_DE = "Du hast dein Stundenlimit in der Arbeitsgruppe erreicht. Frag deine Lehrkraft."
DISPATCH_OFF_DE = "Das Training in der Cloud ist gerade abgeschaltet. Bitte versuche es später noch einmal."
REQUEST_REUSED_DE = "Diese Anfrage wurde schon für eine andere Änderung benutzt. Bitte lade die Seite neu."
DELTA_ZERO_DE = "Die Änderung darf nicht 0 sein."
TEACHER_DELTA_RANGE_DE = "Die Änderung muss zwischen -1000 und 1000 Stunden liegen."
ADMIN_DELTA_RANGE_DE = "Die Änderung muss zwischen -10000 und 10000 Stunden liegen."
POOL_TOO_SMALL_DE = "Im Lehrer-Pool sind nicht genug freie Stunden."
RECLAIM_TOO_MUCH_DE = "Zurückholen geht nur mit freien Stunden – reservierte und verbrauchte Stunden bleiben."
GROUPED_STUDENT_DE = (
    "Die Schülerin oder der Schüler ist in einer Arbeitsgruppe – bitte die Stunden über die Gruppe verwalten."
)
REVOKE_TOO_MUCH_DE = "Entziehen geht höchstens bis zum freien Guthaben der Lehrkraft: {hours}."
LIMIT_RANGE_DE = "Das Limit muss zwischen 1 und 1000 Stunden liegen."
REASON_TOO_LONG_DE = "Der Grund darf höchstens 200 Zeichen lang sein."
CREDIT_CHANGE_FAILED_DE = "Die Stunden konnten nicht geändert werden. Bitte versuche es erneut."
STUDENT_CREATED_POOL_SHORT_DE = (
    "Schüler erstellt, aber im Lehrer-Pool sind nicht genug freie Stunden für die Startstunden."
)
HISTORY_FAILED_DE = "Der Stundenverlauf konnte nicht geladen werden."
COMPENSATION_REASON_REQUIRED_DE = "Bitte gib einen Grund für den Ausgleich an."
COMPENSATION_HOURS_RANGE_DE = "Ein Ausgleich muss zwischen 1 und 100 Stunden liegen."
COMPENSATION_TARGET_KIND_DE = "Ein Ausgleich geht nur an eine Schülerin, einen Schüler oder eine Arbeitsgruppe."
COMPENSATION_GROUPED_DE = (
    "Die Schülerin oder der Schüler ist in einer Arbeitsgruppe – bitte den Ausgleich der Gruppe gutschreiben."
)
COMPENSATION_NO_ACCOUNT_DE = (
    "Für dieses Ziel gibt es kein nutzbares Guthaben – es gehört zu keinem Klassenzimmer oder wurde gelöscht."
)
COMPENSATION_TRAINING_MISMATCH_DE = "Dieses Training wurde nicht aus diesem Guthaben bezahlt."
# Not in the spec's list (§8.5 names only the 404): DELETE /admin/credit-reasons/{id} of an id
# that is not a number.
REASON_NOT_FOUND_DE = "Dieser Grund wurde nicht gefunden."

# Existing literals the credit routes reuse unchanged (spec §8.1).
STUDENT_NOT_FOUND_DE = "Schüler nicht gefunden"
WORKGROUP_NOT_FOUND_DE = "Arbeitsgruppe nicht gefunden"
TEACHER_NOT_FOUND_DE = "Lehrer nicht gefunden"

_UNKNOWN = "—"


def _group_de(int_digits: str) -> str:
    """'1234567' -> '1.234.567' (the de-DE thousands dot)."""
    out = []
    for i, ch in enumerate(reversed(int_digits)):
        if i and i % 3 == 0:
            out.append(".")
        out.append(ch)
    return "".join(reversed(out))


def hours_de(value) -> str:
    """An hour count as a German noun phrase: „1 Stunde", „0 Stunden", „2 Stunden",
    „1.000 Stunden"; a non-integer with one decimal and a comma, always plural
    („3,5 Stunden", „1,0 Stunden"). The rounding is on the number's SHORTEST decimal
    spelling (``repr``), half away from zero — exactly the React twin's rule. „—"
    for a value that is not a finite number (unknown is never 0)."""
    if value is None or isinstance(value, bool):
        return _UNKNOWN
    if isinstance(value, int):
        exact = Decimal(value)
    else:
        try:
            x = float(value)
        except (TypeError, ValueError):
            return _UNKNOWN
        if not math.isfinite(x):
            return _UNKNOWN
        exact = Decimal(repr(x))
    sign = "-" if exact < 0 else ""
    a = abs(exact)
    if a == a.to_integral_value():
        n = int(a)
        if n == 0:
            return "0 Stunden"
        return f"{sign}{_group_de(str(n))} {'Stunde' if n == 1 else 'Stunden'}"
    q = a.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    int_part, _, frac = format(q, "f").partition(".")
    return f"{sign}{_group_de(int_part)},{(frac or '0')[0]} Stunden"


def revoke_too_much(free_hours) -> str:
    return REVOKE_TOO_MUCH_DE.format(hours=hours_de(free_hours))
