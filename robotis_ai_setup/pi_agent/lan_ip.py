"""LAN IP discovery for the Orange Pi agent.

The System window's Pi-IP-Anzeige (see the deploy plan §6) reads this off the
``/status`` endpoint so the teacher can write the reserved IP onto the case
label and reach the wizard at ``http://<ip>/`` wherever mDNS is GPO-disabled.

Ported verbatim from ``jetson_agent/agent.py::_detect_lan_ip`` (agent.py:504) —
same UDP-connect trick, same OSError→"" contract — so the two agents report the
Pi/Jetson address identically. Kept in its own module because the Pi agent
consumes it from more places (status endpoint + the Netzwerk-Check hints) than
the single Jetson heartbeat call site.
"""

from __future__ import annotations

import socket
import subprocess


def detect_lan_ip() -> str:
    """Best-effort LAN IP discovery. Uses the trick of opening a UDP
    socket to a known public address — Linux selects the LAN interface
    that would route there. No actual packet is sent.

    Returns the dotted-quad string. When there is NO default route (the
    UDP-connect trick raises OSError — e.g. a Tier-3 direct-ethernet link with
    only a link-local address, exactly the mode the Pi-IP-Anzeige serves), fall
    back to enumerating a non-loopback interface IPv4 before giving up with
    ``""`` (a truly network-less host, or a CI runner without ``ip``).
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(1.0)
            s.connect(("8.8.8.8", 53))
            return s.getsockname()[0]
    except OSError:
        return _fallback_ipv4()


def _run_ip_addr() -> str:
    """Best-effort ``ip -o addr show`` stdout; ``""`` when ``ip`` is absent
    (macOS / a CI runner) or errors. Isolated so tests can mock it."""
    try:
        out = subprocess.run(
            ["ip", "-o", "addr", "show"],
            capture_output=True, text=True, timeout=3,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""
    return out.stdout or ""


def list_interface_ips() -> list[str]:
    """Every IPv4/IPv6 address on a non-loopback interface, best-effort via
    ``ip -o addr show``. Empty where ``ip`` is unavailable (macOS/CI). Loopback
    (``lo``) rows are skipped and a scope-id suffix (``fe80::1%eth0``) is
    stripped. Shared by :func:`detect_lan_ip`'s route-less fallback and the Pi
    agent's Host/Origin own-address allowlist."""
    ips: list[str] = []
    for line in _run_ip_addr().splitlines():
        toks = line.split()
        # `2: eth0    inet 192.168.1.5/24 brd ... scope global eth0`
        if len(toks) >= 2 and toks[1] == "lo":
            continue
        for i, tok in enumerate(toks):
            if tok in ("inet", "inet6") and i + 1 < len(toks):
                addr = toks[i + 1].split("/", 1)[0].split("%", 1)[0]
                if addr:
                    ips.append(addr)
    return ips


def _fallback_ipv4() -> str:
    """A non-loopback interface IPv4 for the no-default-route case: prefer a
    routable address, else a link-local ``169.254.x.x`` (the direct-ethernet
    Tier-3 mode). ``""`` when none is found (e.g. ``ip`` absent on CI)."""
    routable = ""
    link_local = ""
    for addr in list_interface_ips():
        if ":" in addr or addr.startswith("127."):
            continue  # skip IPv6 + loopback
        if addr.startswith("169.254."):
            link_local = link_local or addr
        else:
            routable = routable or addr
    return routable or link_local
