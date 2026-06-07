"""Ensure a reusable self-signed cert for the phone-camera HTTPS receiver.

getUserMedia needs a secure context, so phone_camera.PhoneCameraServer serves
HTTPS. We mint the cert ONCE with Windows' built-in New-SelfSignedCertificate
(user-scope, NO admin) via new_phone_cert.ps1 and export it to PEM (cert + key)
under %LOCALAPPDATA%\\EduBotics\\phone-cert\\. Subsequent runs reuse the PEMs.

No new GUI dependency: the cert generation is shelled out to PowerShell (the
codebase already shells PowerShell for usbipd/camera-PnP), so we avoid pulling in
`cryptography`. This module only locates/validates the PEMs and invokes the
helper when they are missing.
"""

from __future__ import annotations

import os
import subprocess
import sys

from . import constants

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_SUBPROCESS_KWARGS = {"creationflags": _CREATE_NO_WINDOW} if sys.platform == "win32" else {}


class PhoneCertError(RuntimeError):
    """Cert could not be generated or located (German message for the GUI)."""


def cert_paths(cert_dir: str | None = None) -> tuple[str, str]:
    """Return the ``(cert.pem, key.pem)`` paths under the phone-cert dir."""
    d = cert_dir or constants.PHONE_CERT_DIR
    return os.path.join(d, "cert.pem"), os.path.join(d, "key.pem")


def _pems_usable(cert_path: str, key_path: str) -> bool:
    """True iff both PEMs exist, are non-empty, and look like PEM (BEGIN/END).

    A cheap structural check — enough to avoid re-minting on every run without
    pulling in a crypto library to fully parse them. ssl.load_cert_chain does
    the real validation at server start; a malformed pair surfaces there.
    """
    for path, needle in ((cert_path, "CERTIFICATE"), (key_path, "PRIVATE KEY")):
        try:
            with open(path, encoding="ascii", errors="replace") as fh:
                text = fh.read()
        except OSError:
            return False
        if "-----BEGIN" not in text or needle not in text:
            return False
    return True


def _find_helper_script() -> str | None:
    """Locate new_phone_cert.ps1.

    Dev tree: it sits beside this module under app/scripts/. Installed builds
    that bundle it via build.spec land it next to the frozen exe (sys._MEIPASS
    or the exe dir). Mirrors _resolve_*_script() in gui_app.py.
    """
    candidates = []
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(here, "scripts", "new_phone_cert.ps1"))
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(os.path.join(meipass, "scripts", "new_phone_cert.ps1"))
        candidates.append(os.path.join(meipass, "new_phone_cert.ps1"))
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    candidates.append(os.path.join(exe_dir, "scripts", "new_phone_cert.ps1"))
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def ensure_cert(cert_dir: str | None = None) -> tuple[str, str]:
    """Return ``(cert_path, key_path)``, minting the cert if needed.

    Reuses an existing valid PEM pair. Otherwise shells out to
    new_phone_cert.ps1 (user-scope, no admin). Raises :class:`PhoneCertError`
    (German message) on any failure so the GUI can surface it and skip the
    phone receiver without aborting the whole start.
    """
    d = cert_dir or constants.PHONE_CERT_DIR
    cert_path, key_path = cert_paths(d)

    if _pems_usable(cert_path, key_path):
        return cert_path, key_path

    if sys.platform != "win32":
        # The receiver only runs on the Windows student host; off-Windows there
        # is no New-SelfSignedCertificate. Tests stub this out.
        raise PhoneCertError(
            "Zertifikat-Erstellung wird nur unter Windows unterstützt."
        )

    script = _find_helper_script()
    if script is None:
        raise PhoneCertError(
            "Zertifikat-Skript (new_phone_cert.ps1) wurde nicht gefunden — "
            "bitte EduBotics neu installieren."
        )

    try:
        os.makedirs(d, exist_ok=True)
    except OSError as exc:
        raise PhoneCertError(
            f"Zertifikat-Ordner konnte nicht angelegt werden: {exc}"
        ) from exc

    try:
        result = subprocess.run(
            [
                "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", script, "-OutDir", d,
            ],
            capture_output=True, text=True, timeout=60, **_SUBPROCESS_KWARGS,
        )
    except FileNotFoundError as exc:
        raise PhoneCertError("PowerShell wurde nicht gefunden.") from exc
    except subprocess.TimeoutExpired as exc:
        raise PhoneCertError("Zertifikat-Erstellung hat zu lange gedauert.") from exc

    if result.returncode != 0 or not _pems_usable(cert_path, key_path):
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        tail = detail[-1] if detail else "unbekannter Fehler"
        raise PhoneCertError(
            f"Zertifikat konnte nicht erstellt werden: {tail}"
        )

    return cert_path, key_path
