"""One visual system for the EduBotics setup wizard.

Before this module there was no `ttk.Style()` anywhere in the GUI: fonts were
inline literals in three shapes (`("Segoe UI", 18, "bold")`, `("Consolas", 9)`,
`("", 9, "bold")` — an EMPTY family), every helper paragraph repeated
`foreground="gray"`, and „Daten zurücksetzen" — which irreversibly deletes the
student's datasets, models and the Roboter-Studio calibration — was a plain
`ttk.Button` visually identical to „Umgebung starten", separated only by
`pack(side=tk.RIGHT)`. Meanwhile `installer/robotis_ai_setup.iss` sets
`WizardStyle=modern`, so the installer was styled and the app it installs was
not.

Everything student-visible stays in German; this module's names, comments and
docstrings are English (CLAUDE.md Rule §1).

DPI: see `apply_scaling`. This module NEVER makes the process DPI-aware — that
is a decision about every pixel on every student's screen and it needs a
measurement on real Windows first. It only reacts to the awareness the process
already has.
"""

import sys
import tkinter as tk
from tkinter import ttk

# Segoe UI is the Windows 11 system font; Tk falls back cleanly elsewhere, which
# is why the dev-host rendering differs from a student's without breaking.
FONT_FAMILY = "Segoe UI"
TITLE_FONT = (FONT_FAMILY, 18, "bold")
STATUS_FONT = (FONT_FAMILY, 10)
HINT_FONT = (FONT_FAMILY, 8)
BADGE_FONT = (FONT_FAMILY, 9, "bold")
LOG_FONT = ("Consolas", 9)

COLOR_HINT = "gray"
COLOR_OK = "#1e8449"
COLOR_WARN = "#b9770e"
COLOR_ERROR = "#c0392b"

# Step-status styles. Schritt A-D used to be uniformly grey whether they said
# „Nicht gescannt", „Gefunden: …" or „Nicht gefunden" — 17 greys, one green and
# one #0A6 in the whole file, so nothing on screen said which steps were done.
STEP_OK = "StepOk.TLabel"
STEP_PENDING = "StepPending.TLabel"
STEP_ERROR = "StepError.TLabel"


def apply_theme(root: tk.Misc) -> "ttk.Style":
    """Install the named styles. Idempotent; safe to call more than once.

    Picks a base theme EXPLICITLY because ttk's default varies by platform
    ('vista' on Windows, 'aqua' on macOS, 'default' on Linux) and the student
    build is the one that has to look deliberate.
    """
    style = ttk.Style(root)
    for candidate in ("vista", "clam"):
        try:
            if candidate in style.theme_names():
                style.theme_use(candidate)
                break
        except tk.TclError:  # pragma: no cover — a broken Tk theme install
            continue

    style.configure("Title.TLabel", font=TITLE_FONT)
    style.configure("Status.TLabel", font=STATUS_FONT)
    style.configure("Hint.TLabel", font=HINT_FONT, foreground=COLOR_HINT)
    style.configure("Bold.TLabel", font=BADGE_FONT)

    style.configure(STEP_OK, foreground=COLOR_OK)
    style.configure(STEP_PENDING, foreground=COLOR_HINT)
    style.configure(STEP_ERROR, foreground=COLOR_ERROR)

    style.configure("Primary.TButton", font=(FONT_FAMILY, 10, "bold"))
    # The destructive one. A colour is the most a ttk button reliably carries
    # across themes ('vista' ignores background on buttons entirely), so the
    # affordance is foreground + the existing right-hand placement + the
    # existing confirmation dialog — never colour alone.
    style.configure("Danger.TButton", foreground=COLOR_ERROR)
    return style


def step_style_for(text: str) -> str:
    """Which step style a status line deserves, from the line itself.

    Text-driven ON PURPOSE. The three status StringVars are written from
    `_scan_arms._do_scan`, `_try_rehydrate_arms` and `_refresh_hf_token_status`,
    and the first two are SOURCE-EXTRACTED by the test suite against owners of
    hand-built doubles — so a new `self.<something>` reference in either is an
    AttributeError there. Binding a `trace_add` to the variable instead means
    every writer, present and future, is covered with no changes to any of them.

    Pure and total: an unrecognised line is PENDING, never guessed into a green
    „done" or a red „broken".
    """
    line = (text or "").strip().lower()
    if not line:
        return STEP_PENDING
    if line.startswith(("gefunden", "wiederhergestellt", "✓")):
        return STEP_OK
    if line.startswith("nicht gefunden") or "fehlgeschlagen" in line:
        return STEP_ERROR
    return STEP_PENDING


def process_is_dpi_aware() -> bool:
    """True when Windows will NOT bitmap-stretch this process's window.

    `gui/build.spec`'s `EXE(...)` sets no `manifest=`, and CI installs
    `pyinstaller` unpinned — so whether the shipped .exe declares `dpiAware`
    depends on whatever bootloader manifest the latest PyInstaller emits on
    release day. That is exactly why this is PROBED rather than assumed.

    Never raises, and answers False on every non-Windows host and on any
    Windows old enough to lack both APIs.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
    except Exception:  # noqa: BLE001 — a frozen build without ctypes
        return False
    try:
        # Windows 8.1+. 0 = UNAWARE, 1 = SYSTEM_AWARE, 2 = PER_MONITOR_AWARE.
        awareness = ctypes.c_int(0)
        if ctypes.windll.shcore.GetProcessDpiAwareness(
                0, ctypes.byref(awareness)) == 0:
            return awareness.value >= 1
    except Exception:  # noqa: BLE001 — shcore.dll absent (Windows 7/8)
        pass
    try:
        return bool(ctypes.windll.user32.IsProcessDPIAware())
    except Exception:  # noqa: BLE001
        return False


def apply_scaling(root: tk.Misc) -> float | None:
    """Match Tk's layout scale to the monitor DPI — ONLY when it would help.

    Students run Windows 11 at 125-150 %. Tk lays out at 96 DPI regardless, so
    on a DPI-AWARE process every dimension in this GUI comes out physically
    small. Setting `tk scaling` fixes that.

    GATED ON `process_is_dpi_aware()`, and the gate is the whole point: on a
    NON-aware process Windows is already bitmap-stretching the window, so
    scaling Tk on top of it enlarges the text a SECOND time inside an
    already-stretched frame — strictly worse than doing nothing. This module
    deliberately does not make the process aware (that changes every pixel for
    every student and wants a real Windows measurement first, at 100/125/150 %).

    Returns the scaling applied, or None when nothing was changed. Best-effort:
    a failure leaves the previous behaviour exactly as it was.
    """
    if not process_is_dpi_aware():
        return None
    try:
        dpi = root.winfo_fpixels("1i")
        if not dpi or dpi <= 0:
            return None
        scaling = dpi / 72.0
        root.tk.call("tk", "scaling", scaling)
        return scaling
    except Exception:  # noqa: BLE001 — a diagnostic nicety, never a hard fail
        return None
