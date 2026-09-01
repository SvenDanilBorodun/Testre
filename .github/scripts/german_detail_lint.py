#!/usr/bin/env python3
"""Rule-§1 lint: student/teacher-facing Cloud-API + Pi-agent strings must be German.

Two scan scopes (see SCAN_SCOPES), same AST mechanics for both:

  1. robotis_ai_setup/cloud_training_api/app/**/*.py — strings bound to
     ``detail`` (HTTPException payloads -> React toasts) or ``error_message``
     (trainings-table writes -> rendered RAW to students by MyModels.js /
     TrainingLiveChart.js).
  2. robotis_ai_setup/pi_agent/**/*.py — strings bound to ``message``: the
     Orange-Pi agent's management API answers ``{"ok": ..., "message": ...}``
     JSON that the React System tab renders RAW to students — a surface the
     ci.yml grep ([FEHLER]/[WARNUNG]/[STOPP] lines) never covered. tests/ is
     excluded (fixture strings are maintainer surface, not student-facing).

Each scope checks its keywords in any of the three shapes the codebase uses:
call keywords (``detail=...``), dict literals (``{"message": ...}``)
and subscript assignments (``j["message"] = ...``).
Two violation classes:

  TRANSLITERATION  string uses ae/oe/ue/ss instead of literal ä/ö/ü/ß.
                   Checked on EVERY string, not only ones that classify as
                   German: „Lehrer hat noch Klassenzimmer - erst loeschen"
                   has no umlaut and no GERMAN_WORDS token, so gating this
                   on the classifier meant it was never checked at all.
  ENGLISH          string has English stopwords and no German marker

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

# (scan root, keywords whose bound strings students read, subdir names to skip).
# The pi_agent scope skips tests/ — unittest fixtures assert against both
# German production strings and synthetic English stubs; neither is a surface
# a student ever reads.
SCAN_SCOPES: tuple[tuple[Path, frozenset[str], frozenset[str]], ...] = (
    (
        Path("robotis_ai_setup/cloud_training_api/app"),
        frozenset({"detail", "error_message"}),
        frozenset(),
    ),
    (
        Path("robotis_ai_setup/pi_agent"),
        frozenset({"message"}),
        frozenset({"tests"}),
    ),
)

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
# Transliteration patterns, checked on EVERY scanned string (superset of the
# grep list in ci.yml's first lint step, stemmed for inflections).
# The leading boundary is `\b\w*?`, NOT a bare `\b`. A bare `\b` cannot match
# inside an inflected form: in `geloescht` the character before `l` is `e`, so
# `\bloesch\w*` never fires and `detail="Konto konnte nicht geloescht werden"`
# sat in a fully-covered position while this script exited clean (measured
# 2026-08-08). The same hole applied to every stem here that takes a German
# participle or verb prefix — `geprueft`, `angezeigt`, `ausgewaehlt` — and
# `gewaehlt`/`beschaedigt` are in the list only because someone hit them one at
# a time. `\w*?` closes the class rather than the instance. `geaendert` was one
# of those instance entries and is now the STEM `aender\w*`, so „Keine
# Aenderungen" (2026-08-31, two call sites) is covered too — the prefix makes
# `geaendert` still match.
#
# False positives are implausible by construction: every stem carries an
# ae/oe/ue transliteration digraph, which correct German spells with an umlaut
# and English does not produce. Verified against the whole tree after the fix.
TRANSLITERATIONS = re.compile(
    r"\b\w*?(frueh\w*|kuerzer|schliess\w*|luecke\w*|moeglich\w*|ueber\w*"
    r"|laeuft|haeng\w*|pruef\w*|schueler\w*|benoetig\w*|oberflaeche\w*"
    r"|uebersprungen|enthaelt|zusaetzlich\w*|verfuegbar|aender\w*"
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


def _scan_file(path: Path, keywords: frozenset[str]) -> list[tuple[int, str, str]]:
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
        # TRANSLITERATIONS runs UNCONDITIONALLY, not inside the `is_german`
        # branch. Gating it there is why `detail="Lehrer hat noch Klassenzimmer
        # - erst loeschen"` sat in a fully-covered position while this script
        # exited 0 (measured 2026-08-31): the string carries no umlaut, and not
        # one of its words is in GERMAN_WORDS, so it classified as NEITHER
        # German nor English and no check ran at all. `loesch\w*` was already in
        # the stem list — the gap was the classifier, not the list.
        #
        # Every stem carries an ae/oe/ue digraph that correct German spells with
        # an umlaut and English does not produce, so running it on an
        # unclassified (or even English) string cannot fire on real English.
        # Checked first and returning: a transliterated German string that also
        # trips an English stopword is one finding with the actionable message,
        # not two.
        m = TRANSLITERATIONS.search(text)
        if m:
            findings.append(
                (line_no, "TRANSLITERATION",
                 f"{text!r} (use literal ä/ö/ü/ß: {m.group(0)!r})")
            )
            return
        is_german = bool(GERMAN_CHARS.search(text) or GERMAN_WORDS.search(text))
        if not is_german and ENGLISH_WORDS.search(text):
            findings.append((line_no, "ENGLISH", repr(text)))

    def _is_key(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value in keywords
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg in keywords:
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
    total = 0
    for scan_dir, keywords, skip_dirs in SCAN_SCOPES:
        if not scan_dir.is_dir():
            print(f"::error::{scan_dir} not found — run from the repo root", file=sys.stderr)
            return 2
        for path in sorted(scan_dir.rglob("*.py")):
            rel_parts = path.relative_to(scan_dir).parts[:-1]
            if skip_dirs and (set(rel_parts) & skip_dirs):
                continue
            for line_no, kind, detail in _scan_file(path, keywords):
                total += 1
                print(f"{path}:{line_no}: {kind}: {detail}")
    if total:
        print(
            f"::error::{total} non-German detail/error_message/message string(s) — "
            "students read these (Rule §1: German with literal ä/ö/ü/ß)."
        )
        return 1
    print("cloud-api + pi-agent German strings clean (detail/error_message/message).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
