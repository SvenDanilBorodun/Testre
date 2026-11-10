# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""The Pi's firewall step against routing past nginx (SA-05, 2026-11-13).

A LAN host on the Pi's segment can add a route ``ros_net subnet via <pi>`` and
send packets straight to a container's address (rosbridge, web_video_server,
the dataset sidecar) or to the ros_net GATEWAY address the agent listens on.
Such packets never pass the manager's nginx, so neither its Origin gate nor
the robot login's ``auth_request`` sees them; Docker before 28.0 (the Pi pins
27.5.1) even ACCEPTs them for any published container port. Two rules close
that, both on traffic arriving on a NON-Docker interface only:

* ``DOCKER-USER`` (FORWARD, evaluated before Docker's own rules): a NEW
  connection to the ros_net subnet that was NOT DNATed by a published port is
  dropped. ``-m conntrack ! --ctstate DNAT`` keeps the manager's published
  :80 working (the LAN reaches it on the Pi's own address, DNATed by Docker),
  and replies to connections the containers opened are not NEW.
* ``INPUT``: a packet addressed to the ros_net GATEWAY address is dropped
  unless it arrived on loopback or a Docker bridge (nginx reaches the agent
  from the manager container over the bridge).

The rules live in two chains of our own (``EDUBOTICS-FWD``, ``EDUBOTICS-IN``)
reached by one jump each, so re-applying is idempotent: a jump is inserted only
when ``-C`` does not find it, and a chain whose rules drifted is flushed and
rewritten. Docker (re)creates ``DOCKER-USER`` and re-inserts its own jumps when
it restarts, which can reorder or drop ours, so the agent re-checks
periodically (``agent.AgentApp._firewall_loop``). IPv6 rules are built only
when the ros_net network has IPv6 enabled (it does not by default).

``build_rules`` is pure and unit-tested; ``apply`` drives ``iptables`` /
``ip6tables`` through an injectable runner. A failure is reported in English
for the maintainer and never raises: the agent runs on without it.
"""

from __future__ import annotations

import json
import subprocess
from ipaddress import ip_address, ip_network
from typing import Callable, Optional

CHAIN_FWD = "EDUBOTICS-FWD"
CHAIN_IN = "EDUBOTICS-IN"
DOCKER_USER = "DOCKER-USER"
INPUT = "INPUT"
# Docker's bridge interfaces: the default `docker0` and every user-defined
# network's `br-<id>` (ros_net among them). Everything else is the outside.
DOCKER_IFACE_PATTERNS = ("docker+", "br-+")
BINARIES = {4: "iptables", 6: "ip6tables"}
RUN_TIMEOUT_S = 15


def build_rules(subnet: str, gateway: str, family: int = 4) -> dict:
    """The rule set for ONE address family (pure).

    Returns ``{"binary", "chains": {chain: [args, …]}, "jumps": [(chain, args), …]}``
    where every ``args`` is a tuple of iptables arguments (no chain name, no
    ``-A``/``-I``). Raises ValueError for a subnet or gateway that is not an
    address of ``family``."""
    net = ip_network(str(subnet), strict=False)
    gw = ip_address(str(gateway))
    if family not in BINARIES or net.version != family or gw.version != family:
        raise ValueError("address family mismatch")
    host = f"{gw}/{32 if family == 4 else 128}"
    docker_returns = [("-i", pattern, "-j", "RETURN") for pattern in DOCKER_IFACE_PATTERNS]
    forward = docker_returns + [
        ("-m", "conntrack", "--ctstate", "NEW",
         "-m", "conntrack", "!", "--ctstate", "DNAT", "-j", "DROP"),
    ]
    inbound = [("-i", "lo", "-j", "RETURN")] + docker_returns + [("-j", "DROP")]
    return {
        "binary": BINARIES[family],
        "chains": {CHAIN_FWD: forward, CHAIN_IN: inbound},
        "jumps": [
            (DOCKER_USER, ("-d", str(net), "-j", CHAIN_FWD)),
            (INPUT, ("-d", host, "-j", CHAIN_IN)),
        ],
    }


def ipv6_of_network(inspect_json: str) -> "tuple[Optional[str], Optional[str]]":
    """``(subnet, gateway)`` of the IPv6 half of a ``docker network inspect``
    document, or ``(None, None)`` when IPv6 is not enabled (pure)."""
    try:
        data = json.loads(inspect_json)
    except (TypeError, ValueError):
        return None, None
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict) or data.get("EnableIPv6") is not True:
        return None, None
    for cfg in ((data.get("IPAM") or {}).get("Config") or []):
        if not isinstance(cfg, dict):
            continue
        subnet, gateway = cfg.get("Subnet"), cfg.get("Gateway")
        try:
            if subnet and ip_network(subnet, strict=False).version == 6:
                if not gateway:
                    gateway = str(next(ip_network(subnet, strict=False).hosts()))
                return subnet, gateway
        except ValueError:
            continue
    return None, None


def _default_run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=RUN_TIMEOUT_S, check=False)


def apply(rules: dict, run: Callable = _default_run) -> "tuple[bool, bool, str]":
    """Make the kernel hold ``rules`` (one family), idempotently.

    Returns ``(ok, changed, detail)``: ``changed`` says whether anything had to
    be added or rewritten, ``detail`` is an English reason when ``ok`` is
    False (a missing binary, no ``DOCKER-USER`` chain yet, a refused command).
    Never raises."""
    binary = rules["binary"]
    changed = False

    def call(*args):
        return run([binary, "-w", "-t", "filter", *args])

    try:
        for chain, wanted in rules["chains"].items():
            if call("-L", chain, "-n").returncode != 0:
                if call("-N", chain).returncode != 0:
                    return False, changed, f"{binary}: could not create chain {chain}"
                changed = True
            listing = call("-S", chain)
            present = [line for line in (listing.stdout or "").splitlines()
                       if line.startswith(f"-A {chain} ")]
            intact = (listing.returncode == 0 and len(present) == len(wanted)
                      and all(call("-C", chain, *args).returncode == 0 for args in wanted))
            if not intact:
                if call("-F", chain).returncode != 0:
                    return False, changed, f"{binary}: could not flush chain {chain}"
                for args in wanted:
                    if call("-A", chain, *args).returncode != 0:
                        return False, True, f"{binary}: could not append a rule to {chain}"
                changed = True
        for chain, args in rules["jumps"]:
            if call("-C", chain, *args).returncode == 0:
                continue
            result = call("-I", chain, "1", *args)
            if result.returncode != 0:
                reason = (result.stderr or "").strip().splitlines()[-1:] or ["no detail"]
                return False, changed, f"{binary}: could not insert the jump into {chain}: {reason[0]}"
            changed = True
    except FileNotFoundError:
        return False, changed, f"{binary} is not installed"
    except subprocess.TimeoutExpired:
        return False, changed, f"{binary} did not answer within {RUN_TIMEOUT_S} s"
    except OSError as exc:
        return False, changed, f"{binary} could not run: {type(exc).__name__}"
    return True, changed, ""
