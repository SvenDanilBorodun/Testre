"""Resolve `usbipd.exe` robustly on Windows, independent of the inherited PATH.

Why this exists
---------------
The EduBotics installer installs `usbipd-win.msi`, which appends
``C:\\Program Files\\usbipd-win`` to the *system* PATH. But the GUI is
launched from Inno Setup's ``[Run]`` postinstall step in the same install
session — and Inno Setup's process environment was captured from
``explorer.exe`` BEFORE the MSI ran. So the GUI inherits the stale PATH.

CPython on Windows additionally ignores ``env={"PATH": ...}`` for
executable resolution (Python issue #105889), so refreshing PATH inside
``os.environ`` does not help ``subprocess.run(["usbipd", ...])``. The
robust fix is to invoke usbipd by *absolute path*.

Resolution order
----------------
1. ``shutil.which("usbipd")`` — works after reboot or if PATH happens to
   be current.
2. Known install paths (Program Files, Program Files (x86), %ProgramFiles%).
3. HKLM Uninstall registry key written by the MSI.
4. Re-read PATH from the registry, prepend to ``os.environ["PATH"]``, retry
   ``shutil.which``. Also propagates to any subprocess we spawn (which is
   why we patch ``os.environ`` even though Python ignores ``env`` for
   exec-resolution).

Returns ``None`` when usbipd genuinely is not installed. The GUI shows
the student a "Reparieren" button in that case.

This module never raises on import — it can be imported on Linux for
unit tests; the resolver simply returns ``None`` off-Windows.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

_CACHED_PATH: Optional[str] = None
_RESOLVED_ONCE: bool = False


# Well-known install locations for usbipd-win MSI 5.x. The MSI's default
# is %ProgramFiles%\usbipd-win\usbipd.exe; older 4.x snapshots used a few
# variants. Probe all of them.
def _candidate_install_paths() -> list[Path]:
    candidates: list[Path] = []
    seen: set[str] = set()

    def push(p: Optional[str]) -> None:
        if not p:
            return
        path = Path(p) / "usbipd-win" / "usbipd.exe"
        key = str(path).lower()
        if key not in seen:
            seen.add(key)
            candidates.append(path)

    push(os.environ.get("ProgramFiles"))
    push(os.environ.get("ProgramW6432"))
    push(os.environ.get("ProgramFiles(x86)"))
    # Hard-coded backstops in case the env vars are missing (very stripped
    # PowerShell harness or unusual SYSTEM contexts).
    push(r"C:\Program Files")
    push(r"C:\Program Files (x86)")
    return candidates


def _from_registry_uninstall() -> Optional[str]:
    """Read the MSI's InstallLocation from the Uninstall registry hive.

    The MSI writes the install directory to
      HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\<ProductCode>
    under values "InstallLocation" or "DisplayIcon". This survives even if
    PATH is broken.
    """
    if sys.platform != "win32":
        return None
    try:
        import winreg  # type: ignore[import-untyped]
    except ImportError:
        return None

    hives = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for hive, subkey in hives:
        try:
            with winreg.OpenKey(hive, subkey) as parent:
                i = 0
                while True:
                    try:
                        name = winreg.EnumKey(parent, i)
                    except OSError:
                        break
                    i += 1
                    try:
                        with winreg.OpenKey(parent, name) as k:
                            try:
                                display, _ = winreg.QueryValueEx(k, "DisplayName")
                            except OSError:
                                continue
                            if not display or "usbipd" not in str(display).lower():
                                continue
                            for value_name in ("InstallLocation", "DisplayIcon"):
                                try:
                                    val, _ = winreg.QueryValueEx(k, value_name)
                                except OSError:
                                    continue
                                if not val:
                                    continue
                                val_str = str(val).strip().strip('"')
                                # DisplayIcon often points at usbipd.exe directly
                                candidate = Path(val_str)
                                if candidate.is_file() and candidate.name.lower() == "usbipd.exe":
                                    return str(candidate)
                                # InstallLocation is a directory
                                if candidate.is_dir():
                                    exe = candidate / "usbipd.exe"
                                    if exe.is_file():
                                        return str(exe)
                    except OSError:
                        continue
        except OSError:
            continue
    return None


def _refreshed_path_from_registry() -> Optional[str]:
    """Read the system + user PATH from the registry and return the merged value.

    The MSI updates the registry but in-flight processes keep their old
    environment. Reading it fresh lets us reconstruct what a brand-new
    cmd.exe would see, without needing a reboot or a new logon.
    """
    if sys.platform != "win32":
        return None
    try:
        import winreg  # type: ignore[import-untyped]
    except ImportError:
        return None

    parts: list[str] = []
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        ) as k:
            val, _ = winreg.QueryValueEx(k, "Path")
            if val:
                parts.append(str(val))
    except OSError:
        pass
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            val, _ = winreg.QueryValueEx(k, "Path")
            if val:
                parts.append(str(val))
    except OSError:
        pass
    if not parts:
        return None
    return ";".join(parts)


def find_usbipd(force_refresh: bool = False) -> Optional[str]:
    """Return the absolute path to usbipd.exe, or None if not installed.

    Result is cached across calls. Pass ``force_refresh=True`` after
    running the repair flow to invalidate the cache.
    """
    global _CACHED_PATH, _RESOLVED_ONCE

    if _RESOLVED_ONCE and not force_refresh:
        return _CACHED_PATH

    if sys.platform != "win32":
        _RESOLVED_ONCE = True
        _CACHED_PATH = shutil.which("usbipd")  # honor a Linux dev override
        return _CACHED_PATH

    # 1. Current process PATH
    found = shutil.which("usbipd")
    if found:
        _CACHED_PATH = found
        _RESOLVED_ONCE = True
        _inject_into_path(Path(found).parent)
        return found

    # 2. Known install paths
    for candidate in _candidate_install_paths():
        if candidate.is_file():
            _CACHED_PATH = str(candidate)
            _RESOLVED_ONCE = True
            _inject_into_path(candidate.parent)
            return _CACHED_PATH

    # 3. Registry uninstall key
    reg_path = _from_registry_uninstall()
    if reg_path and Path(reg_path).is_file():
        _CACHED_PATH = reg_path
        _RESOLVED_ONCE = True
        _inject_into_path(Path(reg_path).parent)
        return _CACHED_PATH

    # 4. Refresh PATH from registry and retry
    refreshed = _refreshed_path_from_registry()
    if refreshed:
        # Don't blow away whatever else is in os.environ["PATH"] — append.
        existing = os.environ.get("PATH", "")
        merged = ";".join(p for p in (refreshed, existing) if p)
        os.environ["PATH"] = merged
        found = shutil.which("usbipd")
        if found:
            _CACHED_PATH = found
            _RESOLVED_ONCE = True
            return found

    _CACHED_PATH = None
    _RESOLVED_ONCE = True
    return None


def _inject_into_path(directory: Path) -> None:
    """Prepend usbipd's install dir to os.environ[PATH] for this process.

    Child processes we spawn (PowerShell helpers etc.) inherit this, so
    even shell-based callers like configure_usbipd.ps1 invoked from the
    GUI will find usbipd without a reboot.
    """
    if sys.platform != "win32":
        return
    dir_str = str(directory)
    existing = os.environ.get("PATH", "")
    parts = existing.split(os.pathsep) if existing else []
    if not any(p.lower() == dir_str.lower() for p in parts):
        os.environ["PATH"] = os.pathsep.join([dir_str] + parts)


def usbipd_cmd(*args: str) -> list[str]:
    """Build a subprocess argv that starts with the resolved usbipd path.

    Raises ``UsbipdNotFoundError`` (a subclass of ``FileNotFoundError`` so
    existing ``except FileNotFoundError`` blocks keep working) when usbipd
    is genuinely not installed.
    """
    exe = find_usbipd()
    if not exe:
        raise UsbipdNotFoundError(
            "usbipd.exe was not found on PATH, in Program Files, in the "
            "registry, or in the registry-refreshed PATH. The user likely "
            "needs to (re-)run the EduBotics installer or the Reparieren "
            "button."
        )
    return [exe, *args]


class UsbipdNotFoundError(FileNotFoundError):
    """Raised when usbipd.exe cannot be located anywhere.

    Subclasses ``FileNotFoundError`` so callers that previously caught
    ``FileNotFoundError`` from ``subprocess.run(["usbipd", ...])`` still
    catch this path.
    """


def reset_cache_for_tests() -> None:
    """Test hook — forget the cached resolution result."""
    global _CACHED_PATH, _RESOLVED_ONCE
    _CACHED_PATH = None
    _RESOLVED_ONCE = False
