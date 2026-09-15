"""WSL2 command execution bridge — pinned to the EduBotics distro.

Every `wsl` invocation targets the EduBotics distro explicitly so the GUI
behaves the same way regardless of what other distros the user has installed.
"""

import re
import subprocess
import sys
from typing import Optional

from .constants import WSL_DISTRO_NAME

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_SUBPROCESS_KWARGS = {"creationflags": _CREATE_NO_WINDOW} if sys.platform == "win32" else {}


class WSLError(Exception):
    """Raised when a WSL command fails."""


def run(cmd: str, timeout: int = 30, check: bool = True, distro: Optional[str] = None) -> subprocess.CompletedProcess:
    """Execute a command inside the EduBotics WSL2 distribution.

    The script is fed to bash via stdin rather than `bash -c "<script>"`.
    Reason: wsl.exe + bash -c mishandles multi-line scripts whose `$(...)`
    command substitution captures output containing literal `(` and tab-
    indented lines (e.g. `v4l2-ctl --info`). The captured output ends up
    being parsed by bash itself, producing "bash: line N: Card: command
    not found" and an empty stdout — silently breaking camera discovery.
    Piping the script via stdin avoids the argv path entirely. CRLF is
    normalized so Windows-source-file line endings don't reach bash as
    `$'\\r'` tokens.

    Args:
        cmd: Bash command string to execute.
        timeout: Seconds before the command is killed.
        check: If True, raise WSLError on non-zero exit code.
        distro: Override the distro name (defaults to EduBotics).

    Returns:
        CompletedProcess with stdout/stderr as decoded strings.
    """
    target = distro or WSL_DISTRO_NAME
    # Send stdin as raw bytes so Python's text-mode \n→\r\n translation on
    # Windows doesn't reinsert the CRs we just stripped. We still want
    # str-typed stdout/stderr, so decode manually after.
    script_bytes = cmd.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    try:
        result = subprocess.run(
            ["wsl", "-d", target, "--", "bash"],
            input=script_bytes,
            capture_output=True,
            timeout=timeout,
            **_SUBPROCESS_KWARGS,
        )
    except FileNotFoundError:
        raise WSLError("WSL is not installed or not in PATH.")
    except subprocess.TimeoutExpired:
        raise WSLError(f"WSL command timed out after {timeout}s: {cmd}")

    result.stdout = (result.stdout or b"").decode("utf-8", errors="replace")
    result.stderr = (result.stderr or b"").decode("utf-8", errors="replace")

    if check and result.returncode != 0:
        raise WSLError(
            f"WSL command failed in distro {target!r} (exit {result.returncode}):\n"
            f"  cmd: {cmd}\n"
            f"  stderr: {result.stderr.strip()}"
        )
    return result


def is_wsl_available() -> bool:
    """Check whether WSL2 is installed on the host."""
    try:
        result = subprocess.run(
            ["wsl", "--status"],
            capture_output=True,
            text=True,
            timeout=10,
            **_SUBPROCESS_KWARGS,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


# The three answers to "is the EduBotics distro registered?". The twin of
# installer/scripts/wsl_distro_state.ps1::Get-EduBoticsDistroRegistration — same
# rules, same words (lower-cased here), pinned together cell by cell by
# tests/test_installer_pwsh_executed.py::DistroRegistrationExecutedTest.
DISTRO_REGISTERED = "registered"
DISTRO_ABSENT = "absent"
DISTRO_UNRESPONSIVE = "unresponsive"

_LXSS_KEY = r"Software\Microsoft\Windows\CurrentVersion\Lxss"


def _lxss_distro_names() -> tuple[bool, list[str]]:
    """(readable, names) from the per-user WSL registration store.

    ``HKCU\\...\\Lxss\\{GUID}\\DistributionName`` is what ``wsl --list`` itself
    reads. A missing Lxss key means nothing was ever registered for this
    Windows account (readable, no names); any other failure — including not
    running on Windows — means "cannot tell"."""
    try:
        import winreg  # noqa: PLC0415 — Windows-only module
    except ImportError:
        return False, []
    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _LXSS_KEY)
    except FileNotFoundError:
        return True, []
    except OSError:
        return False, []
    names: list[str] = []
    try:
        index = 0
        while True:
            try:
                sub_name = winreg.EnumKey(root, index)
            except OSError:
                break
            index += 1
            try:
                with winreg.OpenKey(root, sub_name) as sub:
                    value, _kind = winreg.QueryValueEx(sub, "DistributionName")
                if value:
                    names.append(str(value))
            except OSError:
                continue
    finally:
        winreg.CloseKey(root)
    return True, names


def resolve_distro_registration(list_ran: bool, list_returncode: int,
                                list_text: str, registry_readable: bool,
                                registry_names: list[str],
                                distro: str = WSL_DISTRO_NAME) -> str:
    """Pure three-way decision over already-gathered facts.

    * listed by ``wsl --list --quiet``            -> registered
    * the listing answered (rc 0) without it      -> absent
    * wsl said it has NO distributions at all
      (store WSL's ``WSL_E_DEFAULT_DISTRO_NOT_FOUND`` token)  -> absent
    * no listing, but the registration store is readable:
      names the distro -> unresponsive, otherwise -> absent
    * no listing and no readable store            -> unresponsive

    The store is only the TIE-BREAKER: a fresh PC's zero-distro listing can exit
    non-zero with a LOCALIZED sentence and no token, which by exit code alone
    looks exactly like a WSL that is not answering."""
    text = list_text or ""
    # CASE-INSENSITIVE, like WSL's own distro names and like the PowerShell
    # twin's -eq / -contains.
    wanted = distro.casefold()
    if list_ran and any(line.replace("\x00", "").strip().casefold() == wanted
                        for line in text.splitlines()):
        return DISTRO_REGISTERED
    if list_ran and list_returncode == 0:
        return DISTRO_ABSENT
    flat = "".join(text.split()).replace("\x00", "").upper()
    if "WSL_E_DEFAULT_DISTRO_NOT_FOUND" in flat:
        return DISTRO_ABSENT
    if registry_readable:
        named = any(str(n).casefold() == wanted for n in registry_names)
        return DISTRO_UNRESPONSIVE if named else DISTRO_ABSENT
    return DISTRO_UNRESPONSIVE


def distro_registration(attempts: int = 1, delay_s: float = 5.0) -> str:
    """"registered" / "absent" / "unresponsive" for the EduBotics distro.

    "unresponsive" means WSL did not answer and the distro may exist — it must
    never be reported as missing, routed into a (re)import or answered with
    „Installer erneut ausführen". ``attempts`` > 1 re-asks, ``delay_s`` apart,
    ONLY while the answer is unresponsive: a WSL service that is still starting
    after boot blocks `wsl --list` for a few seconds."""
    answer = DISTRO_UNRESPONSIVE
    for attempt in range(max(1, attempts)):
        if attempt:
            import time  # noqa: PLC0415
            time.sleep(delay_s)
        try:
            result = subprocess.run(
                ["wsl", "--list", "--quiet"],
                capture_output=True, text=True, timeout=10,
                **_SUBPROCESS_KWARGS,
            )
            list_ran, rc = True, result.returncode
            text = (result.stdout or "") + (result.stderr or "")
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            list_ran, rc, text = False, -1, ""
        readable, names = _lxss_distro_names()
        answer = resolve_distro_registration(list_ran, rc, text, readable, names)
        if answer != DISTRO_UNRESPONSIVE:
            break
    return answer


def is_edubotics_distro_registered() -> bool:
    """Return True iff the EduBotics WSL2 distro is registered."""
    return distro_registration() == DISTRO_REGISTERED


# Printed by a command INSIDE the distro. Seeing it is the only proof that the
# distro's VM started — an exit code is not (wsl.exe exits non-zero for its own
# failures AND passes a Linux command's status through). The installer's stamp
# probe uses the same word: import_edubotics_wsl.ps1::$STAMP_PROBE_VM_UP and
# robotis_ai_setup.iss::ProbeDistroStamp, pinned equal by a test.
DISTRO_STARTED_SENTINEL = "EDUBOTICS_VM_UP"


def _decode_wsl_bytes(data) -> str:
    """wsl.exe writes its OWN messages as BOM-less UTF-16LE to a pipe, while a
    Linux command's output is UTF-8. Dropping the NULs turns the ASCII half of
    UTF-16LE back into ASCII — the error CODE is ASCII, which is all a caller
    classifies; umlauts in wsl's sentence may garble."""
    if not data:
        return ""
    if isinstance(data, str):
        return data.replace("\x00", "")
    return bytes(data).replace(b"\x00", b"").decode("utf-8", errors="replace")


def probe_distro_start(timeout: int = 30) -> tuple[bool, str]:
    """Start the EduBotics distro once and report ``(started, wsl_output)``.

    ``started`` is True only when the sentinel came back from inside the
    distro. ``wsl_output`` is everything wsl printed (NULs dropped), for
    ``classify_wsl_failure`` and for the Protokoll. Never raises: a missing
    wsl.exe or a timeout is ``(False, <what was captured>)``. ``--exec`` with an
    absolute path runs the command without a shell, so nothing here depends on
    quoting."""
    cmd = ["wsl", "-d", WSL_DISTRO_NAME, "--exec", "/bin/echo", DISTRO_STARTED_SENTINEL]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=timeout, **_SUBPROCESS_KWARGS)
        text = _decode_wsl_bytes(result.stdout) + _decode_wsl_bytes(result.stderr)
    except subprocess.TimeoutExpired as exc:
        text = _decode_wsl_bytes(exc.stdout) + _decode_wsl_bytes(exc.stderr)
    except (FileNotFoundError, OSError):
        return False, ""
    started = any(line.strip() == DISTRO_STARTED_SENTINEL for line in text.splitlines())
    return started, text


# ── Why WSL2 did not start — the twin of virtualization_ready.ps1 ────────────
# classify_wsl_failure is the Python twin of Get-WslFailureClass +
# Get-WslFailureCode (installer/scripts/virtualization_ready.ps1), which carries
# the reasoning and the WSL sources. The rules, restated only as far as needed
# to read this code:
#   * code: one of the two HCS codes (symbolic or legacy hex) found anywhere in
#     a whitespace/NUL-stripped copy, normalised to its symbolic name; else the
#     last segment of the first `Wsl/...` path in the NUL-stripped text
#     (symbolic names upper-case, hex as 0x + upper-case digits); else "".
#   * class: "hypervisor" for those two codes, "disk" for a full-disk code;
#     else "disk" for a MountVhd / AttachDisk / MountDisk stage, "hypervisor"
#     for a CreateVm stage; else "".
# tests/test_installer_pwsh_executed.py::ClassifierExecutedTest runs the .ps1
# over a corpus and asserts THIS function returns the same pair on every row.
WSL_CLASS_HYPERVISOR = "hypervisor"
WSL_CLASS_DISK = "disk"

_HCS_CODE_TOKENS = (
    ("HCS_E_HYPERV_NOT_INSTALLED", "HCS_E_HYPERV_NOT_INSTALLED"),
    ("0X80370102", "HCS_E_HYPERV_NOT_INSTALLED"),
    ("HCS_E_SERVICE_NOT_AVAILABLE", "HCS_E_SERVICE_NOT_AVAILABLE"),
    ("0X80370114", "HCS_E_SERVICE_NOT_AVAILABLE"),
)
_HYPERVISOR_CODES = frozenset({"HCS_E_SERVICE_NOT_AVAILABLE", "HCS_E_HYPERV_NOT_INSTALLED"})
_DISK_FULL_CODES = frozenset({"ERROR_DISK_FULL", "0X80070070", "ERROR_HANDLE_DISK_FULL", "0X80070027"})
_DISK_STAGES = ("/MOUNTVHD/", "/ATTACHDISK/", "/MOUNTDISK/")
_VM_STAGE = "/CREATEVM/"
_WSL_PATH_RE = re.compile(r"(?<![A-Za-z0-9_])[Ww][Ss][Ll][Gg]?/(?:[A-Za-z0-9_]+/)*([A-Za-z0-9_]+)")
_HEX_CODE_RE = re.compile(r"0[xX][0-9A-Fa-f]{8}")


def _wsl_failure_code(text: str) -> str:
    no_nul = text.replace("\x00", "")
    flat = "".join(no_nul.split()).upper()
    for token, name in _HCS_CODE_TOKENS:
        if token in flat:
            return name
    m = _WSL_PATH_RE.search(no_nul)
    if not m:
        return ""
    raw = m.group(1)
    if _HEX_CODE_RE.fullmatch(raw):
        return "0x" + raw[2:].upper()
    return raw.upper()


def classify_wsl_failure(text) -> tuple[str, str]:
    """``(class, code)`` for what wsl.exe printed when it failed — see the block
    comment above. Never raises; ``None``/blank text is ``("", "")``."""
    if not text or not str(text).strip():
        return "", ""
    text = str(text)
    code = _wsl_failure_code(text)
    if code in _HYPERVISOR_CODES:
        return WSL_CLASS_HYPERVISOR, code
    if code.upper() in _DISK_FULL_CODES:
        return WSL_CLASS_DISK, code
    flat = "".join(text.replace("\x00", "").split()).upper()
    if any(stage in flat for stage in _DISK_STAGES):
        return WSL_CLASS_DISK, code
    if _VM_STAGE in flat:
        return WSL_CLASS_HYPERVISOR, code
    return "", code


def list_serial_devices() -> list[str]:
    """List /dev/serial/by-id/ paths visible inside the EduBotics distro."""
    try:
        result = run("ls /dev/serial/by-id/ 2>/dev/null", check=False)
        if result.returncode != 0 or not result.stdout.strip():
            return []
        return [
            f"/dev/serial/by-id/{line.strip()}"
            for line in result.stdout.strip().splitlines()
            if line.strip()
        ]
    except WSLError:
        return []


def _resolve_to_by_path(real_path: str) -> Optional[str]:
    """Return a `/dev/v4l/by-path/...` symlink that resolves to ``real_path``.

    Used as a fallback when two cameras share a colliding by-id symlink
    (the Innomaker SN0001 case — see ``list_video_devices`` for context).
    by-path is anchored to the USB topology, so even identical-serial
    cameras get distinct symlinks. Returns ``None`` if no by-path entry
    resolves back to the device.
    """
    if not real_path:
        return None
    cmd = (
        f"udevadm info -q symlink -n {real_path} 2>/dev/null | tr ' ' '\\n' | "
        f"grep 'v4l/by-path' | while read s; do "
        f"  if [ \"$(readlink -f /dev/$s 2>/dev/null)\" = \"{real_path}\" ]; then "
        f"    echo /dev/$s; break; "
        f"  fi; "
        f"done"
    )
    try:
        res = run(cmd, timeout=5, check=False)
    except WSLError:
        return None
    line = res.stdout.strip().splitlines()
    if line and line[0].strip():
        return line[0].strip()
    return None


def list_video_devices() -> list[dict]:
    """List /dev/video* capture devices with friendly names.

    Returns list of dicts: [{"path": "/dev/video0", "name": "Logitech C920"}, ...]

    Audit F20: `/dev/videoN` is NOT stable across hotplug — the kernel
    may reassign on replug (`/dev/video0` → `/dev/video2`). Resolve to
    the udev `/dev/v4l/by-id/...` symlink when available so the env
    file survives a replug. Mirrors the existing `/dev/serial/by-id/`
    pattern used for the arms.

    Two-cameras-same-VID:PID quirk: when a classroom plugs in two
    identical UVC cameras and only one of them exposes a USB serial
    string, udev generates a `usb-..._SN0001-...` symlink only for the
    serialed one — but `udevadm info -q symlink` returns that same
    by-id name for BOTH devices' v4l capture nodes. Without de-dup the
    function would return two CameraDevices pointing at the same path.
    We emit `$d` (the kernel-assigned /dev/videoN) as a third column so
    we can de-dup by real video node, not by stable path.
    """
    try:
        # Filter to real capture nodes only — UVC cameras expose
        # secondary /dev/videoN entries for metadata / extended-control
        # channels that also report `Type: Video Capture` but enumerate
        # zero image formats. Keep only the ones with at least one
        # `[0]: 'FOURCC' ...` format entry, otherwise the student sees
        # twice as many cameras as they plugged in.
        # Pick the most stable path udev offers for each camera, in this
        # preference order:
        #   1. /dev/v4l/by-id/...    — serial-anchored; survives replug
        #                              into ANY port (camera must expose
        #                              a USB serial)
        #   2. /dev/v4l/by-path/...  — USB-topology-anchored; survives
        #                              replug into the SAME port across
        #                              reboot / wsl shutdown. Used for
        #                              the un-serialed Innomaker.
        #   3. /dev/videoN           — kernel-assigned; reshuffles on
        #                              every enumeration. Last resort.
        # Whichever is picked must also resolve back to $d via readlink,
        # otherwise we'd hand the same symlink name to both cameras
        # when udev's name-generation collides (no-serial case).
        cmd = r"""
for d in /dev/video*; do
    formats=$(v4l2-ctl --device="$d" --list-formats 2>/dev/null)
    echo "$formats" | grep -qE '^[[:space:]]+\[0\]:' || continue
    info=$(v4l2-ctl --device="$d" --info 2>/dev/null)
    name=$(echo "$info" | grep 'Card type' | sed 's/.*Card type[[:space:]]*:[[:space:]]*//')
    bus=$(echo "$info" | grep 'Bus info' | sed 's/.*Bus info[[:space:]]*:[[:space:]]*//')
    symlinks=$(udevadm info -q symlink -n "$d" 2>/dev/null | tr ' ' '\n')
    path="$d"
    chosen=""
    for candidate in $(echo "$symlinks" | grep 'v4l/by-id'); do
        if [ -e "/dev/$candidate" ] && [ "$(readlink -f "/dev/$candidate")" = "$d" ]; then
            chosen="/dev/$candidate"
            break
        fi
    done
    if [ -z "$chosen" ]; then
        for candidate in $(echo "$symlinks" | grep 'v4l/by-path'); do
            if [ -e "/dev/$candidate" ] && [ "$(readlink -f "/dev/$candidate")" = "$d" ]; then
                chosen="/dev/$candidate"
                break
            fi
        done
    fi
    if [ -n "$chosen" ]; then
        path="$chosen"
    fi
    echo "$path|$name|$d|$bus"
done
"""
        result = run(cmd, timeout=15, check=False)
        if not result.stdout.strip():
            return []

        # Dedup by physical camera. `Bus info` from v4l2 is unique per
        # physical USB port (e.g. `usb-vhci_hcd.0-1` vs `usb-vhci_hcd.0-2`),
        # so we key on that to guarantee one entry per camera even when
        # both expose the same by-id symlink.
        raw_rows: list[dict] = []
        seen_bus: set[str] = set()
        for line in result.stdout.strip().splitlines():
            parts = line.split("|", 3)
            if len(parts) < 3:
                continue
            path = parts[0].strip()
            name = parts[1].strip()
            real_path = parts[2].strip()
            bus = parts[3].strip() if len(parts) == 4 else ""
            key = bus or real_path
            if key in seen_bus:
                continue
            seen_bus.add(key)
            raw_rows.append({
                "path": path,
                "name": name or path,
                "real_path": real_path,
                "bus": bus,
            })

        # Audit Gap-D 2026-05-23: when TWO physical cameras report the
        # IDENTICAL USB serial (e.g. the Innomaker U20CAM-720P pair both
        # report SN0001), udev's by-id symlink generation collides: each
        # device gets the SAME `/dev/v4l/by-id/usb-..._SN0001-...` name,
        # pointing at whichever device enumerated last. The shell loop
        # above picks the by-id path for each row independently, so two
        # rows can carry the same by-id `path` while differing in `bus`.
        # If we hand those colliding by-id paths to .env, the entrypoint
        # opens the same /dev/videoN twice → silent gripper↔scene swap.
        #
        # Fix: detect by-id collisions across the de-duped row set and
        # downgrade the colliding rows to their by-path equivalent.
        # by-path is anchored to the physical USB topology
        # (`usb-vhci_hcd.0-1` vs `-2`), so it survives reboot without
        # colliding even on identical-serial cameras. We re-query
        # `udevadm symlink` for each affected row.
        if len(raw_rows) > 1:
            path_counts: dict[str, int] = {}
            for row in raw_rows:
                if "/by-id/" in row["path"]:
                    path_counts[row["path"]] = path_counts.get(row["path"], 0) + 1
            colliding = {p for p, c in path_counts.items() if c > 1}
            if colliding:
                for row in raw_rows:
                    if row["path"] in colliding:
                        by_path = _resolve_to_by_path(row["real_path"])
                        if by_path:
                            row["path"] = by_path

        return [{"path": r["path"], "name": r["name"]} for r in raw_rows]
    except WSLError:
        return []
