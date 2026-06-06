#!/usr/bin/env python3
"""Rule-§1 lint: student/teacher-facing Cloud-API strings must be German.

AST-scans robotis_ai_setup/cloud_training_api/app/**/*.py for strings bound
to ``detail`` (HTTPException payloads -> React toasts) or ``error_message``
(trainings-table writes -> rendered RAW to students by MyModels.js /
TrainingLiveChart.js) in any of the three shapes the codebase uses:
call keywords (``detail=...``), dict literals (``{"error_message": ...}``)
and subscript assignments (``update_data["error_message"] = ...``).
Two violation classes:

  ENGLISH          string has English stopwords and no German marker
  TRANSLITERATION  German string uses ae/oe/ue/ss instead of literal ä/ö/ü/ß

Why AST instead of grep: the strings span implicit concatenation,
parenthesized multiline blocks and f-strings; and DB-error MATCHERS like
``"User profile not found" in msg`` (which must STAY English — they match
Postgres RAISE text from the migrations) are comparisons, not keywords, so
an AST keyword walk excludes them naturally.

Escape hatch: a ``# english-ok`` comment on the keyword's source line skips
that finding (none needed at introduction time — keep it that way).

Run from the repo root: ``python3 .github/scripts/german_detail_lint.py``
Exit 0 = clean, exit 1 = violations printed as ``file:line``.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

APP_DIR = Path("robotis_ai_setup/cloud_training_api/app")
KEYWORDS = {"detail", "error_message"}

GERMAN_CHARS = re.compile(r"[äöüßÄÖÜ]")
# Unambiguously German tokens only — shared/collision-prone words (in, an,
# die, von, Status, Training, Dataset, ...) are deliberately absent.
GERMAN_WORDS = re.compile(
    r"\b(nicht|kein|keine|keinen|bereits|wurde|wurden|darf|muss|bitte"
    r"|erforderlich|fehlgeschlagen|gefunden|abgebrochen|angefordert"
    r"|erreichbar|gestartet|gesendet|gespeichert|gestoppt|unbekannt"
    r"|fehlt|fehlen|eigene|eigenen|dein|deinem|deiner|werden|konnte"
    r"|verwende|versuche|erneut|wiederherstellen|vorhanden|verbunden"
    r"|abgelaufen|beansprucht|angegeben|aktiv|gerade|wieder)\b",
    re.IGNORECASE,
)
ENGLISH_WORDS = re.compile(
    r"\b(not|found|failed|invalid|unable|cannot|can't|missing|already"
    r"|exists|denied|forbidden|unauthorized|expired|required|requested"
    r"|canceled|cancelled|dispatch|please|should|allowed|unknown|deleted"
    r"|the|this|your|does|doesn't|been|have|must be|too many|try again)\b",
    re.IGNORECASE,
)
# Transliteration patterns inside German-classified strings (superset of the
# grep list in ci.yml's first lint step, stemmed for inflections).
TRANSLITERATIONS = re.compile(
    r"\b(frueh\w*|kuerzer|schliess\w*|luecke\w*|moeglich\w*|ueber\w*"
    r"|laeuft|haeng\w*|pruef\w*|schueler\w*|benoetig\w*|oberflaeche\w*"
    r"|uebersprungen|enthaelt|zusaetzlich\w*|verfuegbar|geaendert"
    r"|beschaedigt|koennte|faehig\w*|aufloesung|loesch\w*|gewaehlt"
    r"|waehl\w*|fuer|zurueck\w*|gueltig\w*|ungueltig\w*|koennen|muessen"
    r"|duerfen|groesse\w*|laenge\w*)\b",
    re.IGNORECASE,
)


def _constant_text(node: ast.AST) -> str:
    """All string-literal fragments under a keyword value, joined.

    Covers plain literals, implicit concatenation (folded by the parser),
    parenthesized multiline blocks, f-string constant parts, conditional
    expressions, and literals passed through helper calls. ``str(e)`` /
    pure-interpolation f-strings contribute nothing and are skipped.
    """
    parts = [
        n.value
        for n in ast.walk(node)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]
    return " ".join(parts)


def _scan_file(path: Path) -> list[tuple[int, str, str]]:
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:  # compileall catches this too; be loud anyway
        return [(exc.lineno or 0, "SYNTAX", str(exc))]

    lines = source.splitlines()
    findings: list[tuple[int, str, str]] = []

    def _check(value_node: ast.AST) -> None:
        text = _constant_text(value_node).strip()
        # Neutral: no literal text (str(e), bare interpolation) or too
        # short to carry language ("OK", "-", placeholder fragments).
        if len(re.sub(r"[^A-Za-zäöüßÄÖÜ]", "", text)) < 4:
            return
        line_no = value_node.lineno
        src_line = lines[line_no - 1] if 0 < line_no <= len(lines) else ""
        if "# english-ok" in src_line:
            return
        is_german = bool(GERMAN_CHARS.search(text) or GERMAN_WORDS.search(text))
        if is_german:
            m = TRANSLITERATIONS.search(text)
            if m:
                findings.append(
                    (line_no, "TRANSLITERATION",
                     f"{text!r} (use literal ä/ö/ü/ß: {m.group(0)!r})")
                )
        elif ENGLISH_WORDS.search(text):
            findings.append((line_no, "ENGLISH", repr(text)))

    def _is_key(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value in KEYWORDS
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg in KEYWORDS:
                    _check(kw.value)
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if key is not None and _is_key(key):
                    _check(value)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Subscript) and _is_key(target.slice):
                    _check(node.value)
                    break
    return findings


def main() -> int:
    if not APP_DIR.is_dir():
        print(f"::error::{APP_DIR} not found — run from the repo root", file=sys.stderr)
        return 2
    total = 0
    for path in sorted(APP_DIR.rglob("*.py")):
        for line_no, kind, detail in _scan_file(path):
            total += 1
            print(f"{path}:{line_no}: {kind}: {detail}")
    if total:
        print(
            f"::error::{total} non-German detail/error_message string(s) — "
            "students read these (Rule §1: German with literal ä/ö/ü/ß)."
        )
        return 1
    print("cloud-api German strings clean (detail/error_message).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
