"""Host-side arm scanner + leader/follower disambiguation for the Orange Pi.

This is the HOST orchestrator, ported from ``gui/app/device_manager``'s
``scan_and_identify_arms`` / ``fast_rehydrate_arms`` — run NATIVELY (plain
``docker ...``, no ``wsl -d`` wrapper) and with all usbipd / WSL self-heal
stages removed (on the Pi the arms are directly present on
``/dev/serial/by-id/`` via native udev; there is nothing to attach or heal).

The actual leader-vs-follower ping happens INSIDE a throwaway scanner
container, in the in-image script ``/usr/local/bin/identify_arm.py`` (shipped
in ``open-manipulator-opi``, source: ``docker/open_manipulator/identify_arm.py``)
— it pings Dynamixel servo IDs 1-6 (leader) vs 11-16 (follower) and prints
``leader`` / ``follower`` / ``unknown``. This module NEVER opens a serial port
itself; that must stay in the container that carries ``dynamixel_sdk``.

**Two-identical-arms disambiguation (design decision).** Both OpenMANIPULATOR
arms are OpenRB-150 boards with the SAME VID ``2F5D`` / PIDs ``0103|2202``, so a
static udev VID/PID rule cannot tell leader from follower (unlike the Jetson,
which has ONE arm). We follow the camera precedent and disambiguate by SERVO-ID
PING at scan time, then persist each role to its STABLE
``/dev/serial/by-id/...`` path (the arms have DISTINCT stable serials —
identical-serial is a camera-only problem, per CLAUDE.md). The agent writes
``LEADER_PORT`` / ``FOLLOWER_PORT`` managed ``.env`` keys with those by-id paths.

Chosen over serial-keyed dynamic udev symlinks (``/dev/edubotics-{leader,
follower}``) because it needs NO extra udev machinery: the by-id path is already
stable per physical arm and survives replug, and the compose references
``${FOLLOWER_PORT}`` / ``${LEADER_PORT}`` (student-file style), NOT the Jetson's
hardcoded ``/dev/edubotics-follower``. The static ROBOTIS VID/PID udev rule
(``pi_agent/udev/99-edubotics-robotis.rules``, another stream) remains useful
only for group permissions / a last-resort by-id fallback, not for the role
mapping.

**Three arm FAMILIES.** ``omx`` is the two-OpenRB-150 rig described above;
``edu6`` and ``edu1`` are single-arm Feetech STS rigs on a Waveshare CH343P
bridge, probed with ``--protocol=feetech`` and slotted as the FOLLOWER (they
drive ``FOLLOWER_PORT``; there is no leader on either). The family is chosen by
the caller from the selected robot type — see
``constants.arm_family_for_robot_type``. The two Feetech families are
USB-INDISTINGUISHABLE and are told apart ONLY by their servo count, which the
prober is given as ``--servos=N``.

Rule §1: log lines, comments and docstrings are English (maintainer surface).
The ONE student-facing surface here is ``LAST_SCAN_NOTICE`` — the German
one-sentence diagnosis of the most likely setup mistake, which the agent returns
to the System tab.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from typing import Optional

# ArmDevice is defined in config_generator (the Pi has no device_manager) and the
# scanning modules BUILD it — the config_generator docstring pins this ownership.
# Importing it here (rather than redefining) keeps ONE dataclass shape so
# generate_env_file reads .serial_path off exactly the object we produced.
from .config_generator import ArmDevice
from .constants import ARM_USB_IDS

logger = logging.getLogger("edubotics-pi-agent")

# The scanner container name + the in-image script path (same as the GUI).
SCANNER_CONTAINER_NAME = "robotis_arm_scanner"
IN_CONTAINER_SCRIPT = "/usr/local/bin/identify_arm.py"

# /dev/serial/by-id substring markers PER ARM FAMILY. by-id names are built from
# the USB descriptor strings (usb-<manufacturer>_<product>_<serial>), so the OMX
# OpenRB-150 shows ROBOTIS/OPENRB while the edu6 CH343P shows
# "1a86_USB_Single_Serial" / WCH strings.
#
# ``_FEETECH_BYID_MARKERS`` is COPIED VERBATIM from
# ``gui/app/device_manager.py::_FEETECH_BYID_MARKERS`` — it is a six-way guess,
# not a pin (rig gate R1 records the exact form on real hardware), so the two
# platforms must widen together or a board revision that works on Windows
# silently stays invisible on the Pi. ``find_serial_paths_for_arms`` carries the
# same diagnostic fallback the Windows twin does, which is what makes recording
# the real string possible.
#
# ONE tuple for BOTH Feetech families (edu6, edu1): they sit on the same
# Waveshare Bus Servo Adapter, so their by-id strings, VID:PID and even their
# servo models on ids 1..6 are identical. Only the SERVO COUNT separates them
# (see ``_FAMILY_SERVO_COUNT``), which the in-container prober asserts exactly.
#
# The FTDI legacy path is only a udev fallback and is intentionally not matched.
_FEETECH_BYID_MARKERS = ("1A86", "WCH", "CH343", "USB_SINGLE_SERIAL", "USB SINGLE SERIAL",
                         "USB2.0-SER")

_ARM_MARKERS = {
    "omx":  ("ROBOTIS", "OPENRB"),
    "edu6": _FEETECH_BYID_MARKERS,
    "edu1": _FEETECH_BYID_MARKERS,
}

# Wire protocol the in-image prober must speak for a family.
_FAMILY_PROTOCOL = {"omx": "dxl", "edu6": "feetech", "edu1": "feetech"}

# Servos on a Feetech family's bus, in joint order from id 1. THE ONLY
# discriminator between the two Feetech arms, passed to the prober as
# ``--servos=N`` and asserted EXACTLY there. Absent for ``omx``, whose prober
# keys off leader-vs-follower id ranges instead.
_FAMILY_SERVO_COUNT = {"edu6": 7, "edu1": 6}

# Whether a family's rig has a leader arm at all. NOT the profile-level
# `scan_requires_leader` (that drives start-gating and UI) — this is the
# scanner's own already-implicit fact, stated once: a single Feetech arm lands
# in the FOLLOWER slot and `leader` stays None there. It is used only to decide
# whether a diagnostic sentence is still relevant.
_FAMILY_HAS_LEADER = {"omx": True, "edu6": False, "edu1": False}


def _family_protocol(arm_family: str) -> str:
    """Prober protocol for a family; an unknown family reads as OMX (the
    pre-edu6 default)."""
    return _FAMILY_PROTOCOL.get(arm_family, "dxl")


def _family_servo_count(arm_family: str):
    """Expected Feetech bus length for a family, or ``None`` for a non-Feetech
    (or unknown) family."""
    return _FAMILY_SERVO_COUNT.get(arm_family)

# Candidate list the edu6 diagnostic last reported, so a ten-iteration poll does
# not print the same line ten times into the student-visible Protokoll ring.
_LAST_DIAG_CANDIDATES: "list[str] | None" = None

# Serial-path polling: native udev usually links a plugged arm within ~1 s, but
# a replug right before a scan can lag. Poll a few times before giving up.
_SERIAL_POLL_ATTEMPTS = 10
_SERIAL_POLL_INTERVAL_S = 1.0
_SERIAL_BY_ID_DIR = "/dev/serial/by-id"


def _docker(*args: str) -> list[str]:
    """Build a native ``docker ...`` argv (no ``wsl -d`` wrapper on the Pi)."""
    return ["docker", *args]


def list_serial_by_id() -> list[str]:
    """List full ``/dev/serial/by-id/...`` paths present on the host.

    Native replacement for ``wsl_bridge.list_serial_devices`` — reads the udev
    by-id directory directly. Returns ``[]`` if the directory is absent (no
    serial devices linked yet).
    """
    try:
        entries = sorted(os.listdir(_SERIAL_BY_ID_DIR))
    except OSError:
        return []
    return [f"{_SERIAL_BY_ID_DIR}/{e}" for e in entries]


def serial_path_family_conflict(serial_path: str, arm_family: str) -> bool:
    """POSITIVE evidence that ``serial_path`` is an arm of a DIFFERENT family.

    Twin of ``gui/app/device_manager.py::serial_path_family_conflict`` — same
    body over the same markers, kept in lockstep by
    ``test_arm_family_conflict_twin_lockstep.py``.

    This exists so a robot-type change can INVALIDATE a scan that was made for
    the other family. The evidence is the recorded by-id path itself: the scan
    only ever stores a path that matched ``_ARM_MARKERS[family]``, so the name
    is a durable record of which family it was scanned for. Nothing new has to
    be persisted, and it survives an agent restart for free — the port IS the
    tag, and ``rehydrate_hardware`` reads it straight back out of the ``.env``.

    THREE-VALUED ON PURPOSE, collapsed to a refusal only on proof:

    * matches the family we need            → no conflict (whatever else it
      also matches — an ambiguous name is not evidence AGAINST it);
    * matches some OTHER family and not ours → CONFLICT, the one True;
    * matches nothing at all                 → no conflict.

    That last case is the one worth stating. ``_FEETECH_BYID_MARKERS`` is a
    six-way guess pending rig gate R1, and a hand-edited ``.env`` can name
    anything, so "this path matches no family I know" means the markers are
    incomplete — not that the arm is wrong. Refusing there would brick a rig
    on the strength of a marker list this file's own comments call a guess.
    Same doctrine as ``usb_ids_for_serial_path`` ("``None`` means cannot
    prove, never no match") and as the nav's capability gating (only an
    EXPLICIT false hides anything).
    """
    up = (serial_path or "").upper()
    wanted = _ARM_MARKERS.get(arm_family, ())
    if not up or not wanted:
        # No markers for the family being ASKED about means we cannot prove
        # anything, only observe that the path resembles something else. A new
        # ``ROBOT_PROFILES`` entry whose family is not in this table would
        # otherwise conflict with EVERY arm and make that profile permanently
        # unstartable — turning one forgotten line in an "adding a robot type"
        # checklist into a bricked rig, which is strictly worse than the gap
        # this predicate closes. Fail open at runtime; the gap is caught loudly
        # in CI instead (``test_arm_family_conflict_twin_lockstep.py`` asserts
        # every registry family has markers on BOTH platforms).
        return False
    if any(m in up for m in wanted):
        return False
    return any(any(m in up for m in markers)
               for fam, markers in _ARM_MARKERS.items() if fam != arm_family)


def find_robotis_serial_paths() -> list[str]:
    """Find ``/dev/serial/by-id/`` paths for OMX arms (ROBOTIS/OpenRB).

    Kept as the ``omx``-family shorthand (the GUI keeps
    ``find_serial_paths_for_robotis`` for the same reason): every pre-edu6
    caller reads unchanged and the behaviour is byte-identical.
    """
    return find_serial_paths_for_arms("omx")


def find_serial_paths_for_arms(arm_family: str = "omx") -> list[str]:
    """Family-aware ``/dev/serial/by-id`` discovery (edu6 §4.4).

    Port of ``gui/app/device_manager.py::find_serial_paths_for_arms``. ``omx``
    is byte-identical to the pre-edu6 filter.

    The Feetech leg carries the Windows twin's DIAGNOSTIC FALLBACK: when no
    marker matched but serial devices ARE present, log the candidates.
    ``_FEETECH_BYID_MARKERS`` is a guess until a board is plugged in (rig gate
    R1), and this log line is the only way the real CH343P by-id string gets
    recorded from a classroom Pi — without it the failure is indistinguishable
    from "no arm connected".
    """
    global _LAST_DIAG_CANDIDATES
    all_serial = list_serial_by_id()
    markers = _ARM_MARKERS.get(arm_family, _ARM_MARKERS["omx"])
    hits = [p for p in all_serial if any(m in p.upper() for m in markers)]
    if _family_protocol(arm_family) == "feetech" and not hits and all_serial:
        # ONCE per distinct candidate set. This logger feeds the agent's
        # 800-line Protokoll ring the System tab streams, and the caller polls
        # us ten times per scan — logging per call put eleven identical lines in
        # front of a student for every failed edu6 scan. Keying on the set (not
        # a bool) means a student who plugs something in mid-poll still gets the
        # new list, which is exactly the observation rig gate R1 needs.
        if all_serial != _LAST_DIAG_CANDIDATES:
            _LAST_DIAG_CANDIDATES = list(all_serial)
            # Short and factual: this handler is attached to the ROOT logger, so
            # every line here lands in the Protokoll a student reads (the Windows
            # twin writes the same observation to a diagnostics FILE instead).
            # The candidate list IS the whole payload rig gate R1 needs; the
            # symbol names and the lockstep instruction belong in this comment,
            # not in front of a classroom.
            logger.warning("%s: no arm matched; serial devices present: %r",
                           arm_family, all_serial)
    return hits


# ── USB VID/PID identity (sysfs) ─────────────────────────────────────────────
# The by-id NAME is what the scan discovers with, deliberately broadly: a port
# that merely looks plausible costs one Feetech ping, and the SERVOS then prove
# identity. A claim made to the student has no such downstream proof, so the
# cross-family presence notice must be as strict as the Windows twin, which is
# PID-pinned through ARM_USB_IDS and says why in its own words: „1A86 covers
# every CH34x USB-serial dongle on earth, so an unrelated Arduino clone must not
# be attached/probed as an arm" (gui/app/device_manager.py::list_arm_devices).
# Measured: a plain CH340 (1a86:7523), a CH341 (1a86:5523) and a CH9102 ESP32
# board all match the edu6 by-id markers; none matches the PID.
#
# Windows reads VID/PID from Windows PnP enumeration; the Pi reads it from
# sysfs, which needs no dependency and no elevation.
_SYS_TTY_DIR = "/sys/class/tty"
_USB_ID_WALK_LEVELS = 4


def usb_ids_for_serial_path(by_id_path: str) -> "Optional[tuple[str, str]]":
    """``(VID, PID)`` as uppercase hex for a ``/dev/serial/by-id`` entry.

    ``/sys/class/tty/<tty>/device`` is the USB *interface* for a cdc_acm node;
    ``idVendor``/``idProduct`` live on a parent, so walk up a few levels. Returns
    ``None`` when anything is unreadable — this must never guess, and every
    caller treats ``None`` as "cannot prove", not as "no match".
    """
    try:
        tty = os.path.basename(os.path.realpath(by_id_path))
        base = os.path.join(_SYS_TTY_DIR, tty, "device")
        for _ in range(_USB_ID_WALK_LEVELS):
            vid_f = os.path.join(base, "idVendor")
            pid_f = os.path.join(base, "idProduct")
            if os.path.exists(vid_f) and os.path.exists(pid_f):
                with open(vid_f, encoding="ascii") as f:
                    vid = f.read().strip()
                with open(pid_f, encoding="ascii") as f:
                    pid = f.read().strip()
                if vid and pid:
                    return vid.upper(), pid.upper()
                return None
            base = os.path.join(base, "..")
    except OSError:
        return None
    return None


def find_arm_devices_by_usb_id(arm_family: str) -> list[str]:
    """by-id paths whose USB VID/PID is in ``ARM_USB_IDS[arm_family]``.

    Pi twin of ``gui/app/device_manager.py::list_arm_devices``. A ``None`` PID in
    the table means "any PID under this VID" (the OMX OpenRB-150 ships two).
    A path whose ids cannot be read is SKIPPED, so an unreadable sysfs degrades
    to "no evidence" rather than to a false claim.
    """
    wanted = ARM_USB_IDS.get(arm_family, ())
    hits = []
    for path in list_serial_by_id():
        ids = usb_ids_for_serial_path(path)
        if ids is None:
            continue
        vid, pid = ids
        for want_vid, want_pid in wanted:
            if vid == want_vid.upper() and (want_pid is None
                                            or pid == want_pid.upper()):
                hits.append(path)
                break
    return hits


def start_scanner_container(image: str) -> bool:
    """Start a throwaway privileged container that carries ``dynamixel_sdk`` +
    the in-image ``identify_arm.py``, so we can ping arms without opening the
    serial port on the host. ``--entrypoint sleep ... 120`` keeps it idle so we
    ``exec`` the ping per device (same shape as the GUI scanner)."""
    # Remove any stale scanner first (idempotent).
    subprocess.run(
        _docker("rm", "-f", SCANNER_CONTAINER_NAME),
        capture_output=True, timeout=10,
    )
    try:
        result = subprocess.run(
            _docker("run", "-d",
                    "--name", SCANNER_CONTAINER_NAME,
                    "--privileged",
                    "-v", "/dev:/dev",
                    "--entrypoint", "sleep",
                    image,
                    "120"),
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            logger.warning("scanner container failed to start: %s",
                           (result.stderr or "").strip())
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.warning("scanner container start error: %s", exc)
        return False


def stop_scanner_container() -> None:
    """Remove the throwaway scanner container (best-effort, idempotent)."""
    subprocess.run(
        _docker("rm", "-f", SCANNER_CONTAINER_NAME),
        capture_output=True, timeout=10,
    )


def identify_arm_via_docker(serial_path: str, protocol: str = "dxl",
                            servos: Optional[int] = None) -> str:
    """Run the in-image ``identify_arm.py`` against ``serial_path`` inside the
    running scanner container.

    Returns ``"leader"``, ``"follower"``, ``"unknown"``, ``"partial:N"``,
    ``"bus_too_long:N"``, a family name (``"edu6"`` / ``"edu1"``), a cross-probe
    token (``"omx_arm_found"`` / ``"edu6_arm_found"`` / ``"edu1_arm_found"``),
    the silent-bus token (``"feetech_silent"``), or ``"error:..."`` — exactly
    the GUI contract (``gui/app/device_manager.py::identify_arm_via_docker``).

    ``servos`` is the Feetech bus length the SELECTED family has, forwarded as
    ``--servos=N``; it is what lets the prober tell an edu6 from an edu1.
    ``None`` omits the flag, leaving the prober's own edu6 default.

    THE STDOUT VERDICT WINS OVER THE EXIT CODE. The in-image script PRINTS its
    verdict and then exits NON-ZERO for every result that is not the expected
    arm (feetech: anything but the SELECTED family; dxl: anything but
    leader/follower) — including the informational cross-probe /
    silent-bus / partial tokens. Those tokens are the whole point of the
    diagnosis, so stdout wins whenever it is present; the exit code and stderr
    only matter when stdout is empty (a genuine crash). Keying on
    ``returncode == 0`` instead would collapse a perfectly good
    ``feetech_silent`` or ``partial:3`` into a bare ``"error:"`` and leave every
    scan-notice branch dead.
    """
    extra = ["--protocol=feetech"] if protocol == "feetech" else []
    if protocol == "feetech" and servos is not None:
        extra.append(f"--servos={int(servos)}")
    try:
        result = subprocess.run(
            _docker("exec", SCANNER_CONTAINER_NAME,
                    "python3", IN_CONTAINER_SCRIPT, serial_path, *extra),
            capture_output=True, text=True, timeout=15,
        )
        out = result.stdout.strip()
        if out:
            return out
        return f"error:{result.stderr.strip()}"
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return f"error:{exc}"


def _poll_serial_paths(expected: Optional[set[str]] = None,
                       arm_family: str = "omx") -> list[str]:
    """Poll for this family's arm serial paths. If ``expected`` is given, stop as
    soon as every expected path is present; otherwise stop on the first non-empty
    list.
    """
    paths: list[str] = []
    for _ in range(_SERIAL_POLL_ATTEMPTS):
        paths = find_serial_paths_for_arms(arm_family)
        if expected is not None:
            if expected.issubset(set(paths)):
                return paths
        elif paths:
            return paths
        time.sleep(_SERIAL_POLL_INTERVAL_S)
    return paths


# German notice from the LAST scan, set when the cross-probe (or a pure presence
# check) found the OTHER arm family plugged in, when the Feetech bus answered
# only partially, or when it stayed silent (edu6 §5.4). The agent surfaces it
# after a scan so the most likely setup mistake becomes ONE clear sentence
# instead of the generic „Kein Arm gefunden". Student-facing → German, Rule §1.
LAST_SCAN_NOTICE: str = ""

# The cross-family sentences — copied verbatim from gui/app/device_manager.py
# so both platforms say the same thing.
#
# The omx↔Feetech pair is deliberately GENERIC about which EduBotics arm was
# seen: it is reached from the USB-presence path, and USB cannot distinguish the
# two Feetech arms (one adapter, one VID:PID).
_CROSS_NOTICE_OMX_WHILE_FEETECH = (
    'Es wurde ein OMX-Arm gefunden, aber ein EduBotics-Arm ist '
    'als Robotertyp ausgewählt. Bitte den Robotertyp oben '
    'passend zum angeschlossenen Arm wählen und erneut scannen.'
)
_CROSS_NOTICE_FEETECH_WHILE_OMX = (
    'Es wurde ein EduBotics-Arm gefunden, aber ein '
    'OMX-Robotertyp ist ausgewählt. Bitte den Robotertyp oben '
    'passend zum angeschlossenen Arm wählen und erneut scannen.'
)
# The Feetech↔Feetech pair can ONLY come from the in-container prober, which
# counts servos. Six answering servos are ambiguous by construction — a healthy
# Edu:1 and an EduBotics 6-Achs whose gripper servo has dropped off the chain
# are the same six servos — so this sentence owns that ambiguity.
_CROSS_NOTICE_EDU1_WHILE_EDU6 = (
    'Es antworten genau 6 Servos, ausgewählt ist aber „EduBotics 6-Achs – '
    'Roboter Studio". Zwei Möglichkeiten: entweder ist es ein „Edu:1"-Arm — '
    'dann bitte oben den Robotertyp passend wählen — oder es ist der '
    '6-Achs-Arm, dem das siebte Servo fehlt: dann bitte die Kabelverbindung '
    'zum Greifer prüfen. Danach erneut scannen.'
)
_CROSS_NOTICE_EDU6_WHILE_EDU1 = (
    'Es antworten 7 Servos — das ist ein „EduBotics 6-Achs"-Arm, aber '
    '„Edu:1 – Roboter Studio" ist ausgewählt. Bitte den Robotertyp oben '
    'passend zum angeschlossenen Arm wählen und erneut scannen.'
)

# (found family, selected family) → sentence. An explicit table: with three
# families "the other" is no longer well defined. An unlisted pair yields ""
# (the caller keeps the generic „Kein Arm gefunden").
_CROSS_NOTICES = {
    ("omx", "edu6"): _CROSS_NOTICE_OMX_WHILE_FEETECH,
    ("omx", "edu1"): _CROSS_NOTICE_OMX_WHILE_FEETECH,
    ("edu6", "omx"): _CROSS_NOTICE_FEETECH_WHILE_OMX,
    ("edu1", "omx"): _CROSS_NOTICE_FEETECH_WHILE_OMX,
    ("edu1", "edu6"): _CROSS_NOTICE_EDU1_WHILE_EDU6,
    ("edu6", "edu1"): _CROSS_NOTICE_EDU6_WHILE_EDU1,
}


def cross_family_notice(found_family: str, selected_family: str) -> str:
    """German sentence for "a <found> arm is plugged in, <selected> is chosen".
    Empty string when the pair is not one this scanner can observe."""
    return _CROSS_NOTICES.get((found_family, selected_family), "")


# ── Feetech bus-LENGTH diagnoses ────────────────────────────────────────────
# Both tokens below can only come from the in-container prober, which counts
# servos, and both are rendered against the SELECTED family's own length.
#
# ``bus_too_long:N`` is a token of its own and deliberately NOT a ``partial:``.
# It is the OPPOSITE fault and wants the opposite remedy: ``partial`` means
# servos are MISSING from the chain ("check the connectors"), this means there
# are EXTRA devices on it (a second arm daisy-chained, a duplicated servo id, a
# stray board). Folded into the fraction it printed „Nur 8 von 7 Servos
# antworten" — a fraction greater than one, sending the student to re-seat
# cables on a bus that has too many devices. N is a FLOOR, not a count: the
# prober stops walking one id past the longest supported arm, so the sentence
# says „mindestens".
_TOO_LONG_NOTICE_DE = (
    'Am Bus antworten mindestens {n} Servos — mehr, als dieser Arm hat ({m}). '
    'Vermutlich hängt ein zweites Gerät am selben Bus, oder zwei Servos '
    'haben dieselbe ID. Bitte nur einen Arm anschließen und erneut scannen.'
)
# ``partial:N`` with N >= the selected arm's own length is not a wiring fault
# at all. The CURRENT prober returns the family NAME whenever the bus is
# exactly a supported length, so this combination proves the in-container
# script is OLDER than this host: it ignores ``--servos=`` and measures against
# its own edu6 default, which turns a healthy six-servo Edu:1 into
# „partial:6" — rendered by the old wording as „Nur 6 von 6 Servos antworten",
# i.e. every servo answered and the student is told to check the cabling.
# An image pin (EDUBOTICS_IMAGE_TAG) is a documented rollback path, so this is
# reachable in the field, not only in theory.
_STALE_PROBER_NOTICE_DE = (
    'Der Arm antwortet vollständig ({n} Servos), wird vom Roboter-Abbild aber '
    'nicht erkannt. Das Abbild ist älter als die Software. Bitte die Umgebung '
    'aktualisieren und erneut scannen.'
)
_PARTIAL_NOTICE_DE = (
    'Nur {n} von {m} Servos antworten — bitte die Steckverbindungen zwischen '
    'den Servos am Arm prüfen und erneut scannen.'
)


def _bus_length_token(role: str) -> Optional[int]:
    """The integer in a ``<token>:N`` verdict, or ``None`` if it is not one."""
    if ":" not in role:
        return None
    try:
        return int(role.split(":", 1)[1])
    except ValueError:
        return None


def feetech_bus_length_notice(role: str, servos: Optional[int]) -> str:
    """German sentence for a servo-COUNT verdict (``partial:N`` /
    ``bus_too_long:N``) against the SELECTED family's length ``servos``.

    Returns "" for anything else, and for a count the caller cannot frame
    (no selected length, or an unparsable token) — the caller then keeps the
    generic „Kein Arm gefunden", which is vague but never WRONG.
    """
    n = _bus_length_token(role)
    if n is None or servos is None:
        return ""
    if role.startswith("bus_too_long:"):
        return _TOO_LONG_NOTICE_DE.format(n=n, m=servos)
    if role.startswith("partial:"):
        # `>=`, not `==`: a stale prober measuring against its own LONGER
        # default can report more than this arm has, and „Nur 7 von 6" is the
        # same nonsense as „Nur 8 von 7".
        if n >= servos:
            return _STALE_PROBER_NOTICE_DE.format(n=n)
        return _PARTIAL_NOTICE_DE.format(n=n, m=servos)
    return ""


def _set_cross_family_presence_notice(arm_family: str) -> None:
    """Pi twin of ``device_manager._set_cross_family_presence_notice`` (audit M4).

    The by-id filter is family-scoped, so a wrong-family arm never reaches the
    in-container prober and the probe-token branches in
    ``scan_and_identify_arms`` cannot fire for it in the real flow. When the
    selected family matched NOTHING, check whether the OTHER family's markers
    match something that IS plugged in and surface the same one-sentence hint.

    It asserts a fact to the student („an arm of the OTHER type is plugged in"),
    so it is PID-PINNED through ``ARM_USB_IDS`` — NOT the broad by-id markers the
    discovery path uses. Those markers match every CH34x dongle in a school
    cupboard, and telling a student to change the robot type because an Arduino
    clone is plugged in is worse than the generic message it replaces. Windows
    reaches the same pinning through its PnP enumeration; here it is sysfs.
    """
    global LAST_SCAN_NOTICE
    # "The other" is a two-VALUED question here even with three families: the
    # only boundary USB can see is OMX (2F5D) vs the shared Feetech adapter
    # (1A86:55D3). `edu6` STANDS FOR that adapter in this lookup — the notice it
    # selects is family-generic on purpose (see above).
    other = "omx" if _family_protocol(arm_family) == "feetech" else "edu6"
    try:
        present = find_arm_devices_by_usb_id(other)
    except Exception:  # noqa: BLE001 — a diagnostic must never fail a scan
        # Same breadth as the Windows twin. The scan has already decided its
        # answer by the time we get here; this only decides how to WORD the
        # failure, so it degrades to the generic message rather than turning a
        # clean 404 into a 500.
        return
    if not present:
        return
    LAST_SCAN_NOTICE = cross_family_notice(other, arm_family)
    logger.info("cross-family presence: %s arm at %r while family=%s found none",
                other, present, arm_family)


def scan_and_identify_arms(
    image: str,
    arm_family: str = "omx",
) -> tuple[Optional[ArmDevice], Optional[ArmDevice]]:
    """Full scan workflow: enumerate serial ports, ping each in the scanner
    container, return ``(leader, follower)`` — either may be ``None`` if not
    found. For the single-arm ``edu6`` family the arm lands in the FOLLOWER slot
    (it drives ``FOLLOWER_PORT``); leader is always ``None`` there.

    Native port of ``device_manager.scan_and_identify_arms`` minus the WSL
    self-heal + usbipd attach stages (the Pi has neither). The Dynamixel serial
    bus MUST be free before this runs — the agent stops the robot tier first
    (``docker_manager.ensure_environment_stopped``), exactly as the GUI does,
    because a live 100 Hz controller holds the same ``/dev/serial`` ports the
    ping opens.

    Side effect: sets the module-level ``LAST_SCAN_NOTICE`` (German) — cleared
    on entry, so it always describes THIS scan.
    """
    global LAST_SCAN_NOTICE
    LAST_SCAN_NOTICE = ""
    leader: Optional[ArmDevice] = None
    follower: Optional[ArmDevice] = None

    # 1. Enumerate this family's arm serial ports (poll — udev can lag on a
    #    fresh replug).
    serial_paths = _poll_serial_paths(arm_family=arm_family)
    if not serial_paths:
        # Nothing of THIS family present — if the OTHER family's adapter is
        # plugged in, say so in one sentence (the family-scoped by-id filter
        # means the in-container prober can never diagnose that case).
        _set_cross_family_presence_notice(arm_family)
        logger.warning("no %s arm serial ports found under %s",
                       arm_family, _SERIAL_BY_ID_DIR)
        return None, None

    # 2. Start the throwaway scanner container for the pings.
    if not start_scanner_container(image):
        return None, None

    try:
        time.sleep(1)  # let the container's python come up

        # 3. Identify each serial device (retry once on error/unknown).
        protocol = _family_protocol(arm_family)
        servos = _family_servo_count(arm_family)
        for i, path in enumerate(serial_paths):
            if i > 0:
                time.sleep(1)  # let the USB bus settle between devices
            role = identify_arm_via_docker(path, protocol, servos)
            if role.startswith("error:") or role == "unknown":
                time.sleep(2)
                role = identify_arm_via_docker(path, protocol, servos)

            desc = path.split("/")[-1]
            if role == "leader":
                leader = ArmDevice(serial_path=path, role="leader", description=desc)
            elif role == "follower":
                follower = ArmDevice(serial_path=path, role="follower", description=desc)
            elif role == arm_family and role in _FAMILY_SERVO_COUNT:
                # A Feetech arm of the SELECTED family ("edu6" / "edu1") is a
                # SINGLE arm and drives FOLLOWER_PORT — there is no leader on
                # either.
                follower = ArmDevice(serial_path=path, role="follower", description=desc)
            elif role in _FAMILY_SERVO_COUNT:
                # A family name that is NOT the selected one. The current
                # prober cannot produce this (it is handed --servos= and
                # answers `<other>_arm_found` for a different length), so it
                # means the in-container script is OLDER than this host —
                # reachable in the field, because pinning IMAGE_TAG is a
                # documented rollback. Accepting it as the follower would bring
                # the stack up against the wrong servo bus, so it is a
                # cross-family REFUSAL, worded by the same table.
                LAST_SCAN_NOTICE = cross_family_notice(role, arm_family)
                logger.info("stale prober: reported family=%s at %s while "
                            "family=%s was selected", role, path, arm_family)
            elif role.endswith("_arm_found"):
                # The prober names the family it actually found; the notice
                # table words every observable (found, selected) pair.
                found = role[:-len("_arm_found")]
                LAST_SCAN_NOTICE = cross_family_notice(found, arm_family)
                logger.info("cross-probe: %s arm at %s while family=%s",
                            found, path, arm_family)
            elif (role.startswith("partial:")
                  or role.startswith("bus_too_long:")) and protocol == "feetech":
                # A servo-COUNT verdict. Too few is a mid-chain cable or power
                # fault, precisely diagnosable (audit L3); too many is the
                # opposite fault; and a count that MEETS the selected length is
                # a stale in-container prober, not a wiring problem at all.
                # `feetech_bus_length_notice` owns all three wordings so the
                # two platforms cannot drift.
                LAST_SCAN_NOTICE = feetech_bus_length_notice(role, servos)
                logger.info("feetech bus length at %s: %s (selected family %s "
                            "has %s)", path, role, arm_family, servos)
            elif role == "feetech_silent":
                # Port opened but no servo answered — the arm is there but its
                # 12-V supply is almost certainly off (USB alone enumerates the
                # port; it does NOT power the servos). This is the single most
                # likely Feetech setup mistake, so name it directly.
                LAST_SCAN_NOTICE = (
                    'Der Arm wurde gefunden, aber kein Servo antwortet — ist das '
                    '12-V-Netzteil des Arms eingesteckt und eingeschaltet? Der '
                    'USB-Anschluss allein versorgt die Servos nicht.'
                )
                logger.info("feetech_silent: port %s open but no servo answered", path)
            else:
                logger.info("serial %s did not identify as an arm (%s)", desc, role)
    finally:
        stop_scanner_container()

    # A notice diagnoses a PORT that did not yield an arm, so it is noise once
    # the scan produced everything this family HAS: a stray CH34x dongle answers
    # no Feetech ping and sets the 12-V sentence while the real arm identifies
    # fine two ports later — and the dongle sorts first, so that is the common
    # order. The GUI sidesteps this by returning from its success branch before
    # it ever reads the notice; here the scanner clears it at the source, the
    # only place that knows whether this FAMILY has a leader.
    #
    # That is a family test, and it is deliberately NOT the whole story:
    # `omx_follower` is a leader-less PROFILE inside the two-arm `omx` family,
    # so a successful one-arm scan there is NOT cleared here and the agent
    # clears it a second time against the profile
    # (`agent.py::handle_scan_arms`). The two are meant to disagree — this one
    # cannot see the profile and must not try to.
    if follower is not None and (leader is not None
                                 or not _FAMILY_HAS_LEADER.get(arm_family, True)):
        LAST_SCAN_NOTICE = ""

    return leader, follower


def fast_rehydrate_arms(
    saved_leader_path: str, saved_follower_path: str,
    require_leader: bool = True, arm_family: str = "omx",
) -> tuple[Optional[ArmDevice], Optional[ArmDevice]]:
    """Light revalidation of the previous session's arm mapping.

    THE PARAMETER ORDER IS A TWIN CONTRACT, not a preference: this signature
    must stay identical to ``gui/app/device_manager.py::fast_rehydrate_arms``,
    down to the order of ``require_leader`` and ``arm_family``. They were
    briefly swapped here, which makes a third POSITIONAL argument mean opposite
    things on the two platforms — a bug no type checker and no test on either
    side alone can see. ``tests/test_arm_scan_twin_lockstep.py`` is what fences
    it; every caller passes them by keyword regardless.

    Native port of ``device_manager.fast_rehydrate_arms``: skips the two SLOW
    stages (the throwaway scanner container and the per-device serial pings) and
    only confirms the saved ``/dev/serial/by-id`` paths are present and
    distinct. The path↔role binding is trusted from the saved ``.env``: ROBOTIS
    arms expose DISTINCT stable by-id serials (identical-serial is camera-only,
    CLAUDE.md), so a binding cannot silently swap between sessions — the path
    either reappears as the same physical arm or does not appear at all. ANY
    mismatch returns ``(None, None)`` and the caller falls back to the full
    scan.

    ``arm_family`` scopes the by-id discovery to the right hardware (edu6 §4.4);
    ``omx`` is byte-identical to before.

    ``require_leader`` is False for a follower-only robot type (Roboter-Studio
    kit, edu6): its .env has no ``LEADER_PORT`` at all, so an empty
    ``saved_leader_path`` is LEGAL and the result is ``(None, follower)``. The
    follower is ALWAYS mandatory — a follower mismatch still returns
    ``(None, None)``. A stray leader path left in a hand-edited follower-only
    .env is IGNORED rather than waited on (mirroring the Windows twin's
    ``want_leader`` note): making its presence a precondition would burn the
    whole presence-retry budget and then fall back to a full scan even though
    the follower was right there. The one exception is a leader path EQUAL to
    the follower's — that is a corrupt mapping on either profile, so it bails.
    """
    # The follower is always mandatory.
    if not saved_follower_path:
        return None, None
    if require_leader:
        if not saved_leader_path or saved_leader_path == saved_follower_path:
            return None, None
    elif saved_leader_path and saved_leader_path == saved_follower_path:
        return None, None

    want_leader = require_leader
    expected = {saved_follower_path}
    if want_leader:
        expected.add(saved_leader_path)
    serial_paths = set(_poll_serial_paths(expected=expected, arm_family=arm_family))
    if not expected.issubset(serial_paths):
        return None, None

    def _rebuild(path: str, role: str) -> ArmDevice:
        return ArmDevice(serial_path=path, role=role, description=path.split("/")[-1])

    return (
        _rebuild(saved_leader_path, "leader") if want_leader else None,
        _rebuild(saved_follower_path, "follower"),
    )
