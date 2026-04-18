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
