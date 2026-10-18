"""German sentences of managed ACT training (spec §3.7), defined ONCE.

Every sentence a student, teacher or worker reads about a managed training
lives here: the cloud API writes them into ``trainings.status_note`` /
``trainings.error_message`` through the 049 functions and answers the worker
HTTP API with them. They are passed around as NAMES (``detail=S10``), so
``.github/scripts/german_detail_lint.py`` cannot see them; the umlaut rule is
fenced by ``robotis_ai_setup/tests/test_managed_training_lockstep.py`` (every
``S*`` constant: no ae/oe/ue/ss transliteration, at least one German marker).
S17 is duplicated in the robot's ``chunk_engine.py`` (same test). The worker
defines no German sentence for the database (E11): a host's own text never
reaches ``error_message``.

This module is stdlib-only and imports nothing from the app, so the
lockstep tests load it by path.
"""

from __future__ import annotations

# --- status_note (a managed row's progress line) ---------------------------
S1 = "GPU-Server ausgefallen – das Training wird ab Schritt {step} auf einem anderen Server fortgesetzt."
S2 = "GPU-Server ausgefallen – das Training startet auf einem anderen Server neu."
S3 = "Training fertig – das Modell wird geprüft und veröffentlicht …"

# --- error_message of a failed managed row ----------------------------------
S4 = (
    "Das Training konnte auch nach 4 Versuchen auf GPU-Servern nicht abgeschlossen werden "
    "und wurde abgebrochen. Das Trainingsguthaben wurde erstattet – bitte starte das Training neu."
)
S5 = (
    "Das Training hat das Limit von {hours} Stunden Trainingszeit erreicht und wurde abgebrochen. "
    "Das Trainingsguthaben wurde erstattet."
)
S6 = (
    "Das Training wurde nicht innerhalb von 12 Stunden fertig und wurde abgebrochen. "
    "Das Trainingsguthaben wurde erstattet."
)
S7 = (
    "Das Modell hat die Sicherheitsprüfung nicht bestanden und wurde nicht veröffentlicht. "
    "Das Trainingsguthaben wurde erstattet – bitte starte das Training neu."
)
S8 = (
    "Das Modell konnte nicht auf Hugging Face veröffentlicht werden. "
    "Das Trainingsguthaben wurde erstattet – bitte starte das Training neu."
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
    "Der Cloud-GPU-Server ist nicht nutzbar (GPU oder Speicherplatz fehlt). Das Training wurde "
    "nicht gestartet – das Trainingsguthaben wurde erstattet."
)

# --- robot (duplicated in physical_ai_server/inference/chunk_engine.py) -------
S17 = (
    "Das Modell enthält nicht erlaubte Verarbeitungsschritte und wird aus Sicherheitsgründen "
    "nicht geladen."
)

S18 = (
    "Zurzeit ist keine Cloud-GPU verfügbar. Das Trainingsguthaben wurde erstattet – bitte starte "
    "das Training später neu."
)
S19 = "Das Training ist auf dem GPU-Server abgebrochen. Details stehen im Trainingsprotokoll."
S20 = (
    "Dieser Datensatz kann nicht mit ACT trainiert werden: Kameras, Gelenkdaten oder Aktionen "
    "passen nicht zu dem, was der Roboter senden kann."
)
S21 = (
    "Der Abbruch konnte nicht an den GPU-Server übermittelt werden. Das Trainingsguthaben wurde "
    "erstattet – bitte starte das Training neu."
)

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


def hours_text(cap_s: int) -> str:
    """``MANAGED_ACT_CAP_S / 3600`` for S5: an integer, else one decimal with a comma."""
    hours = cap_s / 3600
    if float(hours).is_integer():
        return str(int(hours))
    return f"{hours:.1f}".replace(".", ",")


def s1(step: int) -> str:
    return S1.format(step=format_step(step))


def s5(cap_s: int) -> str:
    return S5.format(hours=hours_text(cap_s))


def loss_note(resume_step: int) -> str:
    """The status_note of a requeue: S1 with the resume step, or S2 without a checkpoint."""
    return s1(resume_step) if resume_step and resume_step > 0 else S2
