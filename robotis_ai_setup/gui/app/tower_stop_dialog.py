# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""„Stoppen" on the V1 under the camera tower asks first where the limp arm falls.

Spec 40 WP-GUISTOP, 52 §5.3 (c) and §6, owner decision D56 (c). Stopping the
environment de-energises the arm, and a V1 whose limp arm falls BACK falls
toward the camera tower. So on ``v1_studio`` with the tower kit (``tower_v1``)
the GUI reads the driver's verdict once before anything stops
(``docker_manager.read_tower_fall``): ``back``, or ``unknown`` (no answer),
shows this dialog — the limp fall rendered from the real V1 URDF (WP-ART's
frames) and two buttons, „Abbrechen" (stops nothing) and „Trotzdem stoppen"
(the stop as today); ``forward`` or ``none`` (no arm container) stops at once.
Every other robot type and the legacy camera never ask.

The dialog's texts are the Pi's (``TowerHint/towerHintCopy.js``), byte for
byte: both platforms say the same thing (``tests/test_gui_tower_stop.py``).

tkinter is imported inside :func:`ask` only, so the decisions and the texts
stay importable where Tk is absent (the deps-free test suites).
"""

import ctypes
import os
import sys

from .scene_split import PROFILE_TOWER, normalise_profile

# The robot type whose driver publishes the limp-fall verdict
# (== feetech_arm_node.ARM_PROFILE).
ARM_PROFILE = "v1_studio"
# read_tower_fall's answers that ask before the stop.
FALLS_THAT_ASK = ("back", "unknown")

# The dialog, byte-equal to towerHintCopy.js (TOWER_HINT_STOP_TITLE_DE,
# TOWER_HINT_STOP_DE, TOWER_CANCEL_DE, TOWER_STOP_ANYWAY_DE).
TITLE_DE = "Achtung: Beim Stoppen wird der Arm weich"
TEXT_DE = (
    "Beim Stoppen wird der Arm weich. Aus dieser Lage würde er nach hinten zum "
    "Kameraturm fallen. Fahre ihn zuerst in die Grundstellung (Roboter Studio → "
    "Steuern → Grundstellung) oder halte ihn beim Stoppen fest."
)
CANCEL_DE = "Abbrechen"
STOP_ANYWAY_DE = "Trotzdem stoppen"
# The status bar and the Protokoll around it.
CHECKING_DE = "Vor dem Stoppen wird geprüft, wohin der Arm fällt …"
UNKNOWN_DE = (
    "[INFO] Wohin der Arm beim Stoppen fällt, konnte nicht gelesen werden — "
    "deshalb die Rückfrage."
)
CANCELLED_DE = "Stoppen abgebrochen — die Umgebung läuft weiter."
DIALOG_FAILED_DE = (
    "[WARNUNG] Die Rückfrage vor dem Stoppen konnte nicht angezeigt werden — "
    "die Umgebung wird trotzdem gestoppt."
)

# WP-ART's limp-fall frames (320 x 236 RGBA), shipped by build.spec's
# ('assets', 'assets'): 0-11 fall back from the observation pose, 12-23 fall
# forward from HOME; the last one is the safe pose.
FRAME_DIR = "v1_tower_hint"
FRAME_FILES = tuple(f"fall-{index:02d}.png" for index in range(24))
SAFE_FRAME = len(FRAME_FILES) - 1
# The Pi's 4.8 s loop (towerHint.css): the first frame held 10 %, 23 equal
# steps over 62 %, the last frame held 28 %.
_HOLD_FIRST_MS = 480
_HOLD_LAST_MS = 1344
_STEP_MS = round((4800 - _HOLD_FIRST_MS - _HOLD_LAST_MS) / SAFE_FRAME)
FRAME_DELAYS_MS = ((_HOLD_FIRST_MS + _STEP_MS,) + (_STEP_MS,) * (SAFE_FRAME - 1)
                   + (_HOLD_LAST_MS,))

# Windows' „Animationen in Windows anzeigen", which WebView2 reads as
# prefers-reduced-motion.
SPI_GETCLIENTAREAANIMATION = 0x1042
# theme.FONT_FAMILY; theme imports tkinter at module top, so it is not imported here.
_TITLE_FONT = ("Segoe UI", 12, "bold")
_WRAP_PX = 420


def applies(robot_type, scene_profile) -> bool:
    """Whether „Stoppen" reads the limp-fall verdict first: the V1 with the
    tower kit only."""
    return robot_type == ARM_PROFILE and normalise_profile(scene_profile) == PROFILE_TOWER


def asks_first(fall) -> bool:
    """Whether ``fall`` (``docker_manager.read_tower_fall``) asks before the stop."""
    return fall in FALLS_THAT_ASK


def frame_paths() -> list:
    """The frames' absolute paths, in the source tree and in the frozen exe
    (``sys._MEIPASS``), as ``gui_app._asset_path`` resolves an asset."""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))
    return [os.path.join(base, "assets", FRAME_DIR, name) for name in FRAME_FILES]


def animations_enabled() -> bool:
    """Whether Windows shows animations. Off: the dialog shows the safe frame
    still, as the Pi's page does under prefers-reduced-motion. Another OS or a
    setting that cannot be read animates."""
    if sys.platform != "win32":
        return True
    try:
        enabled = ctypes.c_int(1)
        if not ctypes.windll.user32.SystemParametersInfoW(
                SPI_GETCLIENTAREAANIMATION, 0, ctypes.byref(enabled), 0):
            return True
        return bool(enabled.value)
    except Exception:  # noqa: BLE001 — a setting nobody can read animates
        return True


def _load_frames(tk):
    """Every frame as a PhotoImage, or ``[]`` when one cannot be read (the
    dialog then shows its text and buttons only)."""
    try:
        return [tk.PhotoImage(file=path) for path in frame_paths()]
    except (tk.TclError, OSError):
        return []


def ask(parent) -> bool:
    """Ask over ``parent`` and wait for the answer: True for „Trotzdem
    stoppen", False for „Abbrechen", the window's close button or Esc. Modal
    (a local grab), on the Tk main thread; „Abbrechen" has the focus."""
    import tkinter as tk
    from tkinter import ttk

    answer = []
    timer = []
    win = tk.Toplevel(parent)
    win.title(TITLE_DE)
    win.transient(parent)
    win.resizable(False, False)

    def close(stop):
        if not answer:
            answer.append(stop)
        if timer:
            win.after_cancel(timer[-1])
        win.destroy()

    body = ttk.Frame(win, padding=16)
    body.pack(fill=tk.BOTH, expand=True)
    frames = _load_frames(tk)
    if frames:
        animate = animations_enabled()
        picture = ttk.Label(body, image=frames[0 if animate else SAFE_FRAME])
        picture.images = frames  # a PhotoImage nobody references is blanked
        picture.pack(pady=(0, 8))
        if animate:
            shown = [0]

            def step():
                shown[0] = (shown[0] + 1) % len(frames)
                picture.configure(image=frames[shown[0]])
                timer.append(win.after(FRAME_DELAYS_MS[shown[0]], step))

            timer.append(win.after(FRAME_DELAYS_MS[0], step))
    ttk.Label(body, text=TITLE_DE, font=_TITLE_FONT, wraplength=_WRAP_PX).pack(pady=(0, 6))
    ttk.Label(body, text=TEXT_DE, wraplength=_WRAP_PX, justify=tk.LEFT).pack(pady=(0, 12))
    buttons = ttk.Frame(body)
    buttons.pack()
    cancel = ttk.Button(buttons, text=CANCEL_DE, command=lambda: close(False))
    cancel.pack(side=tk.LEFT, padx=5)
    ttk.Button(buttons, text=STOP_ANYWAY_DE, style="Danger.TButton",
               command=lambda: close(True)).pack(side=tk.LEFT, padx=5)
    win.protocol("WM_DELETE_WINDOW", lambda: close(False))
    win.bind("<Escape>", lambda _event: close(False))

    win.update_idletasks()
    x = parent.winfo_rootx() + (parent.winfo_width() - win.winfo_reqwidth()) // 2
    y = parent.winfo_rooty() + (parent.winfo_height() - win.winfo_reqheight()) // 2
    win.geometry(f"+{max(x, 0)}+{max(y, 0)}")
    try:
        win.grab_set()
    except tk.TclError:
        pass
    cancel.focus_set()
    parent.wait_window(win)
    return bool(answer and answer[0])
