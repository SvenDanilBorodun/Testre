"""German sentences of managed ACT training (spec §3.7), defined ONCE.

Every sentence a student, teacher or worker reads about a managed training
lives here: the cloud API writes them into ``trainings.status_note`` /
``trainings.error_message`` through the 049 functions and answers the worker
HTTP API with them. They are passed around as NAMES (``detail=S10``), so
``.github/scripts/german_detail_lint.py`` cannot see them; the umlaut rule is
fenced by ``robotis_ai_setup/tests/test_managed_training_lockstep.py`` (every
``S*`` constant: no ae/oe/ue/ss transliteration, at least one German marker).
S17 is duplicated in the robot's ``chunk_engine.py`` (same test). S22–S29 are
the cap, wall and continuation sentences (owner E33–E37, E43–E44); S30–S40 the
stop, give-up and checkpoint-overdue sentences of the training-hour ledger
(migration 052, spec §8.1). Since 052 no sentence claims a refund: a training
is charged its started hours by the settlement trigger, and an hour count is
always ``credit_texts_de.hours_de``'s noun phrase („1 Stunde", review 2 m7).
The worker defines no German sentence for the database (E11): a host's own
text never reaches ``error_message``.

This module imports only the stdlib and its sibling ``credit_texts_de`` (itself
stdlib-only); the lockstep test imports it as ``app.services.training_texts_de``.
"""

from __future__ import annotations

from app.services.credit_texts_de import hours_de

# --- status_note (a managed row's progress line) ---------------------------
S1 = "GPU-Server ausgefallen – das Training wird ab Schritt {step} auf einem anderen Server fortgesetzt."
S2 = "GPU-Server ausgefallen – das Training startet auf einem anderen Server neu."
S3 = "Training fertig – das Modell wird geprüft und veröffentlicht …"

# --- error_message of a failed managed row ----------------------------------
S4 = (
    "Das Training ist auf 4 GPU-Servern immer wieder abgebrochen, bevor ein Zwischenstand gespeichert war. "
    "Wahrscheinlich liegt es am Datensatz – prüfe ihn im Tab Daten, bevor du neu startest. "
    "Berechnet wird nur gespeicherte Trainingszeit."
)
S5 = (
    "Das Limit von {hours} Trainingszeit war erreicht, bevor ein Zwischenstand gespeichert wurde – "
    "das Training wurde ohne Modell beendet. Berechnet wird nur gezählte Trainingszeit."
)
S6 = (
    "Das Training wurde nicht innerhalb von 12 Stunden fertig und hatte noch keinen Zwischenstand – "
    "es wurde ohne Modell beendet. Berechnet wird nur gezählte Trainingszeit."
)
S7 = (
    "Das Modell hat die Sicherheitsprüfung nicht bestanden und wurde nicht veröffentlicht. "
    "Die bisherige Trainingszeit wird berechnet."
)
S8 = (
    "Das Modell konnte nicht auf Hugging Face veröffentlicht werden. "
    "Die Trainingszeit wird berechnet – bitte wende dich an deine Lehrkraft."
)

# --- HTTP answers -------------------------------------------------------------
S9 = "Das Training ist fertig und wird gerade veröffentlicht – Abbrechen ist nicht mehr möglich."
S10 = "Der Trainings-Worker ist für dieses Training nicht mehr berechtigt."
S11 = "Die Dateiliste des Checkpoints ist ungültig."
S12 = "Der Checkpoint hat die Prüfung nicht bestanden und wurde verworfen."
S13 = "Der letzte Checkpoint fehlt – das Training kann noch nicht abgeschlossen werden."
S14 = "Dieser Checkpoint wurde bereits übernommen."
S15 = "Für diesen Checkpoint wurde kein Upload angefordert."

# --- legacy worker host check (written by the worker through the RPC) --------
S16 = (
    "Der Cloud-GPU-Server ist nicht nutzbar (keine passende GPU). Das Training wurde "
    "nicht gestartet – das Trainingsguthaben wurde erstattet."
)

# --- robot (duplicated in physical_ai_server/inference/chunk_engine.py) -------
S17 = (
    "Das Modell enthält nicht erlaubte Verarbeitungsschritte und wird aus Sicherheitsgründen "
    "nicht geladen."
)

S18 = (
    "Zurzeit ist keine Cloud-GPU verfügbar und es gibt noch keinen Zwischenstand – das Training wurde beendet. "
    "Berechnet wird nur gespeicherte Trainingszeit, die übrigen reservierten Stunden sind wieder frei."
)
S19 = "Das Training ist auf dem GPU-Server abgebrochen. Details stehen im Trainingsprotokoll."
S20 = (
    "Dieser Datensatz kann nicht mit ACT trainiert werden: Kameras, Gelenkdaten oder Aktionen "
    "passen nicht zu dem, was der Roboter senden kann."
)
S21 = (
    "Der Abbruch konnte nicht an den GPU-Server übermittelt werden; das Training wurde beendet. "
    "Berechnet wird die Trainingszeit bis zum Abbruch."
)

# --- the cap, the wall and the continuation (owner E33–E37, E43–E44) --------
S22 = "Zeitlimit erreicht – der letzte Zwischenstand wird geprüft und veröffentlicht …"
S23 = "Nach {hours} bei Schritt {step} von {total} gestoppt – du kannst das Training fortsetzen."
S24 = (
    "Der gespeicherte Zwischenstand ist abgelaufen oder nicht mehr nutzbar – das Training kann nicht mehr "
    "fortgesetzt werden. Bitte starte ein neues Training."
)
S25 = "Dieses Training wird bereits fortgesetzt."
S26 = "Dieses Training kann zurzeit nicht fortgesetzt werden."
S27 = (
    "Der gespeicherte Zwischenstand ist nicht mehr nutzbar – die Fortsetzung wurde beendet. "
    "Berechnet wird nur ihre tatsächliche Trainingszeit."
)
S28 = "Fortsetzung ab Schritt {step} – ein GPU-Server wird dafür gesucht …"
S29 = (
    "Das Zeitlimit von 12 Stunden seit dem Start wurde bei Schritt {step} von {total} erreicht – "
    "du kannst das Training fortsetzen."
)

# --- the training-hour ledger: stop, give-up publishes, overdue checkpoints (052) ---
S30 = "Training gestoppt – der letzte Zwischenstand wird geprüft und veröffentlicht …"
S31 = "Bei Schritt {step} von {total} gestoppt – du kannst das Training fortsetzen."
S32 = "Von deiner Lehrkraft gestoppt – der letzte Zwischenstand wird geprüft und veröffentlicht …"
S33 = "Von deiner Lehrkraft bei Schritt {step} von {total} gestoppt – du kannst das Training fortsetzen."
S34 = "Das Training kann nicht weiterlaufen – der letzte Zwischenstand wird geprüft und veröffentlicht …"
S35 = (
    "Nach 4 Versuchen auf GPU-Servern wurde das Training bei Schritt {step} von {total} beendet – "
    "du kannst es fortsetzen."
)
S36 = (
    "Zurzeit ist keine Cloud-GPU frei – das Training wurde bei Schritt {step} von {total} beendet. "
    "Du kannst es später fortsetzen."
)
S37 = (
    "Das Training konnte auch nach 4 Versuchen nicht abgeschlossen werden, weil die GPU-Server ausgefallen sind. "
    "Berechnet wird nur gespeicherte Trainingszeit – bitte starte das Training neu."
)
S38 = (
    "Ein späterer Zwischenstand hat die Sicherheitsprüfung nicht bestanden – veröffentlicht wurde der letzte "
    "gültige bei Schritt {step} von {total}. Du kannst das Training fortsetzen."
)
S39 = (
    "Der GPU-Server hat zu lange keinen Zwischenstand gespeichert – das Training wird ab Schritt {step} "
    "auf einem anderen Server fortgesetzt."
)
S40 = "Der GPU-Server hat zu lange keinen Zwischenstand gespeichert – das Training startet auf einem anderen Server neu."

# --- other API answers of the managed path (not numbered in the spec) -------
TRAINING_NOT_FOUND_DE = "Training nicht gefunden."
TRAINING_NOT_ACTIVE_DE = "Training ist nicht aktiv."
HF_UNREACHABLE_DE = "HuggingFace Hub ist vorübergehend nicht erreichbar. Bitte gleich erneut versuchen."
WORKER_BAD_REQUEST_DE = "Die Anfrage des Trainings-Workers ist ungültig."
WORKER_UNAVAILABLE_DE = "Der Trainingsdienst ist gerade nicht erreichbar. Bitte gleich erneut versuchen."
WORKER_UNKNOWN_ACTION_DE = "Diese Aktion gibt es für den Trainings-Worker nicht."
WORKER_BODY_TOO_LARGE_DE = "Die Anfrage des Trainings-Workers ist zu groß."


def format_step(step: int) -> str:
    """A step count with a `.` thousands separator (S1): 12500 -> "12.500"."""
    return f"{int(step):,}".replace(",", ".")


def s1(step: int) -> str:
    return S1.format(step=format_step(step))


def s5(limit_s: int) -> str:
    """S5 with the training's own limit (reserved hours × 3600, a pre-052 row 12 600 s)."""
    return S5.format(hours=hours_de(limit_s / 3600))


def s23(limit_s: int, step: int, total: int) -> str:
    return S23.format(hours=hours_de(limit_s / 3600), step=format_step(step), total=format_step(total))


def s28(step: int) -> str:
    return S28.format(step=format_step(step))


def s29(step: int, total: int) -> str:
    return S29.format(step=format_step(step), total=format_step(total))


def _step_total(template: str, step: int, total: int) -> str:
    return template.format(step=format_step(step), total=format_step(total))


def s31(step: int, total: int) -> str:
    return _step_total(S31, step, total)


def s33(step: int, total: int) -> str:
    return _step_total(S33, step, total)


def s35(step: int, total: int) -> str:
    return _step_total(S35, step, total)


def s36(step: int, total: int) -> str:
    return _step_total(S36, step, total)


def s38(step: int, total: int) -> str:
    return _step_total(S38, step, total)


def s39(step: int) -> str:
    return S39.format(step=format_step(step))


CHECKPOINT_OVERDUE = "checkpoint_overdue"

# §8.1 S4 vs S37 [Q-B, N3]: the loss reasons that point at the dataset.
DATASET_CLASS_LOSSES = frozenset({
    "stalled", "host_unfit:lerobot_crash", "host_unfit:resume_crash",
    "host_unfit:lerobot_error_unconfirmed", "host_unfit:content_unconfirmed",
    # 052 fix round 2 (owner): an S19 claim that could not lead to the waiver is still a
    # claim that LeRobot failed before any step, so it points at the dataset as well
    "host_unfit:lerobot_error_not_fresh", "host_unfit:lerobot_error_waiver_refused",
})


def loss_note(resume_step: int, reason: str | None = None) -> str:
    """The status_note of a requeue: S1 with the resume step, or S2 without a checkpoint;
    a ``checkpoint_overdue`` loss reads S39 / S40, chosen the same way (R3-3)."""
    resumed = bool(resume_step and resume_step > 0)
    if reason == CHECKPOINT_OVERDUE:
        return s39(resume_step) if resumed else S40
    return s1(resume_step) if resumed else S2


def attempts_out_message(lost_reasons) -> str:
    """S4 when at least 2 of the training's losses are dataset-class, else S37."""
    reasons = [r for r in (lost_reasons or []) if isinstance(r, str)]
    return S4 if sum(1 for r in reasons if r in DATASET_CLASS_LOSSES) >= 2 else S37
