#!/usr/bin/env python3
"""Write the licence notices of the Python environment the GUI was bundled from.

``release-installer.yml`` runs this with the SAME interpreter that ran
PyInstaller, right after the build, and writes the result into the PyInstaller
dist folder (``robotis_ai_setup/gui/dist/EduBotics/THIRD_PARTY_PYTHON.txt``).
The installer's ``[Files]`` entry for that folder then ships it as
``{app}\\gui\\THIRD_PARTY_PYTHON.txt``.

The output covers:

* every installed distribution: name, version, the licence it declares
  (``License-Expression``, ``License``, the ``License ::`` classifiers) and the
  text of every licence file in its metadata (PEP 639 ``licenses/`` and the
  legacy ``LICENSE*`` / ``LICENCE*`` / ``COPYING*`` / ``NOTICE*`` files);
* the interpreter's own ``LICENSE.txt``. On a python.org Windows build it also
  carries the licences of the libraries the runtime bundles (OpenSSL, Tcl/Tk,
  libffi, SQLite, zlib, bzip2, xz).

The build environment may hold more than PyInstaller bundled; listing a
superset is deliberate. Exit status 2 when no distribution was found, so a
broken environment fails the build instead of shipping an empty notice.

Standard library only.
"""

from __future__ import annotations

import argparse
import importlib.metadata as md
import os
import re
import sys
import sysconfig
from pathlib import Path
from typing import Iterable

_LICENSE_FILE = re.compile(r"^(licen[cs]e|copying|notice)([._-].*)?$", re.IGNORECASE)
_RULE = "=" * 78
# A ``License`` field this long is a pasted licence text, not a name.
_LONG_LICENSE_FIELD = 200


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _license_files(dist: md.Distribution) -> list[tuple[str, str]]:
    found = []
    for f in dist.files or ():
        parts = f.parts
        in_meta = any(p.endswith((".dist-info", ".egg-info")) for p in parts[:-1])
        if not in_meta:
            continue
        if not (_LICENSE_FILE.match(f.name) or "licenses" in parts[:-1]):
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            try:
                text = f.read_text(encoding="latin-1")
            except OSError:
                continue
        if text and text.strip():
            found.append(("/".join(parts), text.strip()))
    return sorted(found)


def collect(dists: Iterable[md.Distribution]) -> list[dict]:
    """One entry per distribution name, sorted case-insensitively."""
    entries: dict[str, dict] = {}
    for dist in dists:
        meta = dist.metadata
        name = (meta.get("Name") or "").strip()
        if not name or _norm(name) in entries:
            continue
        declared = []
        expression = (meta.get("License-Expression") or "").strip()
        if expression:
            declared.append(expression)
        license_field = (meta.get("License") or "").strip()
        long_field = ""
        if license_field and license_field.upper() != "UNKNOWN":
            if len(license_field) > _LONG_LICENSE_FIELD:
                long_field = license_field
            elif license_field not in declared:
                declared.append(license_field)
        for classifier in meta.get_all("Classifier") or ():
            if classifier.startswith("License ::"):
                label = classifier.split("::")[-1].strip()
                if label not in declared:
                    declared.append(label)
        files = _license_files(dist)
        if long_field and not files:
            files = [("METADATA License field", long_field)]
        entries[_norm(name)] = {
            "name": name,
            "version": (meta.get("Version") or "").strip(),
            "declared": declared,
            "files": files,
        }
    return sorted(entries.values(), key=lambda e: e["name"].lower())


def python_license() -> tuple[str, str] | None:
    """The running interpreter's own LICENSE.txt, wherever this build keeps it."""
    candidates = [
        Path(sys.base_prefix) / "LICENSE.txt",
        Path(sysconfig.get_paths()["stdlib"]) / "LICENSE.txt",
    ]
    for path in candidates:
        if path.is_file():
            return str(path), path.read_text(encoding="utf-8", errors="replace").strip()
    return None


def render(entries: list[dict], interpreter: tuple[str, str] | None) -> str:
    out = [
        "EduBotics – Python-Bestandteile der Windows-Anwendung",
        "Open-source components of the EduBotics Windows application",
        "",
        "Diese Datei wird beim Bau des Installationsprogramms automatisch aus der",
        "Python-Umgebung erzeugt, aus der die Anwendung gebaut wurde. Für jedes",
        "Paket stehen Name, Version, die erklärte Lizenz und die Lizenztexte aus",
        "seinen Metadaten. Weitere Hinweise: ..\\THIRD_PARTY_NOTICES.txt",
        "",
        f"Python {sys.version.split()[0]} ({sys.platform}), {len(entries)} Pakete / packages",
        "",
    ]
    if interpreter is not None:
        where, text = interpreter
        out += [_RULE, f"Python (CPython) {sys.version.split()[0]} — {os.path.basename(where)}", _RULE, text, ""]
    for e in entries:
        out += [_RULE, f"{e['name']} {e['version']}".rstrip()]
        out.append("Lizenz / License: " + ("; ".join(e["declared"]) if e["declared"] else "nicht angegeben / not declared"))
        out.append(_RULE)
        if not e["files"]:
            out += ["(Keine Lizenzdatei in den Paketmetadaten / no licence file in the package metadata)", ""]
            continue
        for path, text in e["files"]:
            out += [f"--- {path}", text, ""]
    return "\n".join(out).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True, help="file to write")
    args = parser.parse_args(argv)
    entries = collect(md.distributions())
    if not entries:
        print("third_party_python_notices: no installed distribution found", file=sys.stderr)
        return 2
    text = render(entries, python_license())
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    without = sum(1 for e in entries if not e["files"])
    print(f"wrote {out} ({len(entries)} distributions, {without} without a licence file, {len(text)} chars)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
