#!/usr/bin/env python3
"""EduBotics — Windows GUI entry point.

Dual-mode entry:
  * Default: launches the tkinter setup GUI (`gui_app.run()`).
  * With `--webview --url <URL>`: runs the pywebview main loop in the current
    process. This mode is spawned as a child process by `webview_window` so
    that pywebview has its own main thread (pywebview 6 enforces this).
"""

import sys
import os

# Ensure the gui package is importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _refresh_path_for_subprocess_helpers() -> None:
    """Best-effort: inject usbipd's install dir into PATH at process start.

    The Inno Setup postinstall step launches this EXE before Windows has
    broadcast WM_SETTINGCHANGE, so our PATH is whatever Inno Setup inherited
    from explorer.exe at install start — i.e. without the usbipd-win MSI's
    install dir. Resolve it now so PowerShell helpers we shell out to (e.g.
    elevated configure_usbipd.ps1) find ``usbipd`` without a reboot.

    On non-Windows this is a no-op. The resolver caches the path so this
    is also a cheap no-op on subsequent imports.
    """
    if sys.platform != "win32":
        return
    try:
        from app.usbipd_resolver import find_usbipd
        find_usbipd()
    except Exception:
        # Never block GUI startup on a resolver bug. The device_manager
        # path will re-attempt resolution lazily and surface a clear
        # German error if it still cannot find usbipd.
        pass


_refresh_path_for_subprocess_helpers()


def _dispatch_webview() -> int:
    """Handle the `--webview ...` subprocess invocation."""
    import argparse
    from app.webview_window import run_in_process, WEBVIEW_FLAG

    parser = argparse.ArgumentParser(prog="EduBotics --webview", add_help=False)
    parser.add_argument(WEBVIEW_FLAG, dest="webview", action="store_true")
    parser.add_argument("--url", required=True)
    parser.add_argument("--icon", default="")
    parser.add_argument("--debug", default="0")
    args = parser.parse_args()
    return run_in_process(args.url, args.icon, args.debug == "1")


if __name__ == "__main__":
    if "--webview" in sys.argv[1:]:
        sys.exit(_dispatch_webview())

    from app.gui_app import run
    run()
