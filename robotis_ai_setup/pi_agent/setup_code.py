# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""Show a NEW setup code for this Orange Pi (root, on the Pi itself).

    sudo sh -c 'cd /opt/edubotics && python3 -m pi_agent.setup_code'

The setup code is stored only as a hash, so an existing code can never be shown
again: this command asks the running agent (over its loopback-only route
``POST /auth/setup-code``) to replace the code with a fresh one and prints it.
It works only while the Pi is NOT yet set up; on a claimed Pi a new code comes
only with a factory reset (System → Zurücksetzen).

``--label`` prints the code inside the case-label frame setup.sh uses.

Exit codes: 0 printed a code · 3 the Pi is already set up · 1 anything else.
Teacher-facing output, so German with literal umlauts (Rule §1).
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

from .constants import PORT_AGENT

_URL = f"http://127.0.0.1:{PORT_AGENT}/auth/setup-code"
_TIMEOUT_S = 15.0


def request_new_code(url: "str | None" = None, timeout: float = _TIMEOUT_S) -> "tuple[int, dict]":
    """POST to the agent's loopback route; ``(status, payload)``, status 0 when
    the agent did not answer at all."""
    req = urllib.request.Request(url or _URL, data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return int(r.status), json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return int(e.code), json.loads(e.read().decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return int(e.code), {}
    except (urllib.error.URLError, OSError, ValueError):
        return 0, {}


def render(code: str, hostname: str, label: bool = False) -> str:
    host = hostname or "edubotics-NN"
    lines = []
    if label:
        lines.append("============================================================")
        lines.append("  EduBotics Orange Pi — Einrichtungscode")
        lines.append("============================================================")
    lines.extend([
        "",
        f"  Einrichtungscode:  {code}",
        "",
        "  Einmalig: Eine Lehrkraft oder ein Admin öffnet",
        f"  http://{host}.local/ (oder http://<IP>/), meldet sich an und",
        "  gibt diesen Code ein. Danach kann sich jedes EduBotics-Konto",
        "  an diesem Roboter anmelden.",
        "  Der Code wird nur jetzt angezeigt. Ein neuer Aufruf dieses",
        "  Befehls ersetzt ihn.",
        "",
    ])
    if label:
        lines.append("============================================================")
    return "\n".join(lines)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    label = "--label" in argv
    status, payload = request_new_code()
    if status == 200 and isinstance(payload.get("setup_code"), str):
        print(render(payload["setup_code"], str(payload.get("hostname") or ""), label=label))
        return 0
    if status == 409:
        print("Dieser Roboter ist bereits eingerichtet — es wird kein neuer "
              "Einrichtungscode gebraucht. Ein neuer Code entsteht erst mit dem "
              "Zurücksetzen (System → Zurücksetzen).")
        return 3
    if status == 0:
        print("Der EduBotics-Agent antwortet nicht. Läuft er? "
              "(sudo systemctl status edubotics-pi)", file=sys.stderr)
        return 1
    message = payload.get("message") if isinstance(payload, dict) else None
    print(f"Kein Einrichtungscode erzeugt: {message or f'HTTP {status}'}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
