"""USB hot-plug detection for the EduBotics GUI.

Two strategies, tried in order:

1. **Event-driven** via Windows' `RegisterDeviceNotification` + the
   `WM_DEVICECHANGE` message. Zero CPU when idle, fires within ~50 ms
   of a USB arrival/removal. Requires `pywin32` to be available; the
   installer's PyInstaller bundle includes it.

2. **Polling fallback** — every 3 s, ask `usbipd list` for the set of
   currently-Connected BUSIDs and diff against the previous snapshot.
   Works without pywin32 (e.g. when the GUI is launched from a non-
   bundled Python interpreter for development). Roughly 0.3 % CPU.

Both strategies converge on the same `Callable[[Event], None]` callback.
Callers register a single handler that fires with an `Event(kind=...,
busid=..., vid_pid=...)` payload. The handler runs on a worker thread
and must marshal back to the Tk mainloop itself (use `root.after(0,
fn)` or `queue.Queue`) — the listener does not touch Tk.
"""

from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass(frozen=True)
class Event:
    """USB device hotplug event surfaced to listeners."""
    kind: str            # "arrival" | "removal"
    busid: str = ""      # usbipd busid like "1-6" (may be empty on Windows-only events)
    vid_pid: str = ""    # "0c45:6367"


Listener = Callable[[Event], None]


class HotplugWatcher:
    """Owns the background thread that watches USB hotplug events.

    Usage:
        watcher = HotplugWatcher()
        watcher.subscribe(lambda evt: print(evt))
        watcher.start()
        # ... later ...
        watcher.stop()

    The watcher tries the pywin32 message-only window first; if pywin32
    is missing or the message loop fails to spin up within 2 s, it
    transparently falls back to polling. The listener API is identical
    in both cases.
    """

    def __init__(self) -> None:
        self._subscribers: list[Listener] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Snapshot of (busid, vid_pid) tuples observed last poll. Used
        # by both backends to suppress repeated arrival events for the
        # same device.
        self._seen: set[tuple[str, str]] = set()

    def subscribe(self, listener: Listener) -> None:
        with self._lock:
            self._subscribers.append(listener)

    def _notify(self, event: Event) -> None:
        with self._lock:
            subs = list(self._subscribers)
        for sub in subs:
            try:
                sub(event)
            except Exception:
                # A buggy listener should not kill the watcher thread.
                # In production the GUI's tkinter `after` shim swallows
                # exceptions back into the main thread's error handler.
                pass

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="EduBotics-HotplugWatcher", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    # ─── thread body ────────────────────────────────────────────────────────

    def _run(self) -> None:
        # Seed the snapshot once so the very first poll doesn't fire
        # an "arrival" event for every device that was already attached
        # when the GUI launched.
        self._seen = self._snapshot()

        if sys.platform == "win32" and self._try_win32_message_loop():
            return  # message loop ran to completion / stop()
        # Fallback: polling loop.
        self._polling_loop()

    def _try_win32_message_loop(self) -> bool:
        """Attempt to run a WM_DEVICECHANGE message pump.

        Returns True if the loop ran (and is now exiting because stop()
        was called), False if pywin32 isn't available or the loop
        couldn't be set up. The polling loop runs in the False case.
        """
        try:
            import win32api  # type: ignore
            import win32con  # type: ignore
            import win32gui  # type: ignore
        except ImportError:
            return False

        DBT_DEVICEARRIVAL = 0x8000
        DBT_DEVICEREMOVECOMPLETE = 0x8004
        WM_DEVICECHANGE = 0x0219

        def wnd_proc(hwnd, msg, wparam, lparam):
            if msg == WM_DEVICECHANGE:
                if wparam == DBT_DEVICEARRIVAL:
                    # Re-snapshot to identify which device arrived.
                    new = self._snapshot()
                    added = new - self._seen
                    self._seen = new
                    for busid, vid_pid in added:
                        self._notify(Event("arrival", busid, vid_pid))
                elif wparam == DBT_DEVICEREMOVECOMPLETE:
                    new = self._snapshot()
                    gone = self._seen - new
                    self._seen = new
                    for busid, vid_pid in gone:
                        self._notify(Event("removal", busid, vid_pid))
            return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

        try:
            wc = win32gui.WNDCLASS()
            wc.lpszClassName = "EduBoticsHotplugWnd"
            wc.lpfnWndProc = wnd_proc
            class_atom = win32gui.RegisterClass(wc)
            hwnd = win32gui.CreateWindowEx(
                0, class_atom, "EduBoticsHotplug",
                0, 0, 0, 0, 0,
                win32con.HWND_MESSAGE, 0, 0, None,
            )
        except Exception:
            return False

        # PumpWaitingMessages in a loop so we can honour stop() without
        # blocking on PumpMessages forever.
        while not self._stop.is_set():
            win32gui.PumpWaitingMessages()
            time.sleep(0.1)

        try:
            win32gui.DestroyWindow(hwnd)
            win32gui.UnregisterClass(class_atom, None)
        except Exception:
            pass
        return True

    def _polling_loop(self) -> None:
        # 3 s strikes a balance: imperceptible to a student who plugs in
        # a camera and clicks "Scannen", and low enough CPU overhead
        # that we can run the watcher 24/7 in the GUI background.
        while not self._stop.is_set():
            new = self._snapshot()
            added = new - self._seen
            gone = self._seen - new
            for busid, vid_pid in added:
                self._notify(Event("arrival", busid, vid_pid))
            for busid, vid_pid in gone:
                self._notify(Event("removal", busid, vid_pid))
            self._seen = new
            self._stop.wait(3.0)

    def _snapshot(self) -> set[tuple[str, str]]:
        """Return current set of (busid, vid_pid) tuples from usbipd."""
        # Lazy import — avoids circulars if hotplug is imported before
        # device_manager is fully loaded (e.g. during GUI bootstrap).
        from . import device_manager
        try:
            devs = device_manager.list_usb_devices()
        except Exception:
            return set()
        return {(d.busid, d.vid_pid.lower()) for d in devs}


# Module-level singleton — the GUI only needs one watcher per process.
_WATCHER: Optional[HotplugWatcher] = None


def get_watcher() -> HotplugWatcher:
    """Return the process-wide singleton HotplugWatcher (lazy-created)."""
    global _WATCHER
    if _WATCHER is None:
        _WATCHER = HotplugWatcher()
    return _WATCHER
