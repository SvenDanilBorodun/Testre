"""The Pi's firewall step in a REAL kernel (SA-05, 2026-11-13).

``pi_agent/firewall.py`` is unit-tested against a fake ``iptables``; this proves
the rules do what they claim in netfilter itself. Three network namespaces
stand in for the classroom:

* ``lan``  — a laptop on the school network that adds a route to the ros_net
  subnet via the Pi (the attack);
* ``pi``   — the Orange Pi: a LAN interface, a Docker-style bridge ``br-…``
  carrying the ros_net gateway address, the agent listening on that gateway,
  and Docker 27's own FORWARD/DOCKER/nat rules reproduced by hand — including
  the pre-28 behaviour that ACCEPTs a published container port whatever host
  address the publish is bound to;
* ``ctr``  — a container on ros_net serving the "rosbridge" (:9090) and the
  manager's nginx (:80, published on the Pi's LAN address as :8081).

Before the rules: the laptop reaches the container's :9090 and the agent on
the gateway by routing alone. After ``firewall.apply`` (the very function the
agent calls, its commands run inside the Pi namespace): both are dropped, while
the published (DNATed) port, the container→agent path nginx uses and the
container's own outbound connection still work, and a second apply changes
nothing.

Needs root, ``ip`` and ``iptables`` (skipped otherwise, e.g. on GitHub's
unprivileged runners). Stdlib only.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path

SETUP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SETUP_DIR))

from pi_agent import firewall  # noqa: E402

_SUFFIX = f"{os.getpid() % 10000}"
LAN, PI, CTR = f"edu-lan-{_SUFFIX}", f"edu-pi-{_SUFFIX}", f"edu-ctr-{_SUFFIX}"
SUBNET, GATEWAY, CONTAINER = "172.28.0.0/24", "172.28.0.1", "172.28.0.2"
PI_LAN, LAPTOP = "192.168.77.1", "192.168.77.2"
BRIDGE = "br-edutest"

_SERVER = r"""
import socket, sys
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind((sys.argv[1], int(sys.argv[2]))); s.listen(16)
print("ready", flush=True)
while True:
    c, _ = s.accept()
    try:
        c.sendall(b"ok")
    finally:
        c.close()
"""

_CLIENT = r"""
import socket, sys
try:
    s = socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=1.5)
    data = s.recv(8)
    sys.exit(0 if data == b"ok" else 2)
except OSError:
    sys.exit(1)
"""


def _usable() -> str:
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return "needs root"
    for tool in ("ip", "iptables"):
        if shutil.which(tool) is None:
            return f"no {tool}"
    probe = f"edu-probe-{_SUFFIX}"
    made = subprocess.run(["ip", "netns", "add", probe], capture_output=True)
    if made.returncode != 0:
        return "cannot create a network namespace"
    subprocess.run(["ip", "netns", "del", probe], capture_output=True)
    return ""


_SKIP = _usable()


def sh(*cmd, ns=None, check=True):
    full = (["ip", "netns", "exec", ns] if ns else []) + list(cmd)
    proc = subprocess.run(full, capture_output=True, text=True, timeout=30)
    if check and proc.returncode != 0:
        raise AssertionError(f"{' '.join(full)} failed: {proc.stderr.strip()}")
    return proc


@unittest.skipIf(_SKIP, _SKIP or "")
class FirewallInARealKernel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.servers = []
        try:
            cls._build()
        except Exception:
            cls._teardown()
            raise

    @classmethod
    def tearDownClass(cls):
        cls._teardown()

    @classmethod
    def _teardown(cls):
        for proc in getattr(cls, "servers", []):
            proc.kill()
            proc.wait(timeout=10)
        for ns in (LAN, PI, CTR):
            subprocess.run(["ip", "netns", "del", ns], capture_output=True)

    @classmethod
    def _build(cls):
        for ns in (LAN, PI, CTR):
            sh("ip", "netns", "add", ns)
            sh("ip", "link", "set", "lo", "up", ns=ns)
        # laptop ⟷ Pi (the school LAN)
        sh("ip", "link", "add", "eth0", "netns", LAN, "type", "veth", "peer", "name", "eth0", "netns", PI)
        sh("ip", "addr", "add", f"{LAPTOP}/24", "dev", "eth0", ns=LAN)
        sh("ip", "link", "set", "eth0", "up", ns=LAN)
        sh("ip", "addr", "add", f"{PI_LAN}/24", "dev", "eth0", ns=PI)
        sh("ip", "link", "set", "eth0", "up", ns=PI)
        # the Pi's ros_net bridge and one container on it
        sh("ip", "link", "add", BRIDGE, "type", "bridge", ns=PI)
        sh("ip", "addr", "add", f"{GATEWAY}/24", "dev", BRIDGE, ns=PI)
        sh("ip", "link", "set", BRIDGE, "up", ns=PI)
        sh("ip", "link", "add", "vethctr", "netns", PI, "type", "veth", "peer", "name", "eth0", "netns", CTR)
        sh("ip", "link", "set", "vethctr", "master", BRIDGE, ns=PI)
        sh("ip", "link", "set", "vethctr", "up", ns=PI)
        sh("ip", "addr", "add", f"{CONTAINER}/24", "dev", "eth0", ns=CTR)
        sh("ip", "link", "set", "eth0", "up", ns=CTR)
        sh("ip", "route", "add", "default", "via", GATEWAY, ns=CTR)
        sh("sysctl", "-qw", "net.ipv4.ip_forward=1", ns=PI)
        # the attack: the laptop routes ros_net via the Pi
        sh("ip", "route", "add", SUBNET, "via", PI_LAN, ns=LAN)
        # Docker 27's rules, by hand (filter + nat)
        ipt = lambda *a: sh("iptables", "-w", *a, ns=PI)  # noqa: E731
        ipt("-N", "DOCKER-USER")
        ipt("-A", "DOCKER-USER", "-j", "RETURN")
        ipt("-N", "DOCKER")
        ipt("-P", "FORWARD", "DROP")
        ipt("-A", "FORWARD", "-j", "DOCKER-USER")
        ipt("-A", "FORWARD", "-o", BRIDGE, "-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED", "-j", "ACCEPT")
        ipt("-A", "FORWARD", "-o", BRIDGE, "-j", "DOCKER")
        ipt("-A", "FORWARD", "-i", BRIDGE, "!", "-o", BRIDGE, "-j", "ACCEPT")
        ipt("-A", "FORWARD", "-i", BRIDGE, "-o", BRIDGE, "-j", "ACCEPT")
        # Docker < 28: a published port's ACCEPT names the CONTAINER, not the
        # host address the publish is bound to (127.0.0.1:9090:9090 included).
        for port in ("9090", "80"):
            ipt("-A", "DOCKER", "-d", f"{CONTAINER}/32", "!", "-i", BRIDGE, "-o", BRIDGE,
                "-p", "tcp", "--dport", port, "-j", "ACCEPT")
        ipt("-t", "nat", "-A", "PREROUTING", "-d", PI_LAN, "-p", "tcp", "--dport", "8081",
            "-j", "DNAT", "--to-destination", f"{CONTAINER}:80")
        ipt("-t", "nat", "-A", "POSTROUTING", "-s", SUBNET, "!", "-o", BRIDGE, "-j", "MASQUERADE")
        # servers: the container's rosbridge + nginx, the agent on the gateway,
        # and a service on the laptop the container connects out to
        for ns, host, port in ((CTR, CONTAINER, 9090), (CTR, CONTAINER, 80),
                               (PI, GATEWAY, 8769), (LAN, LAPTOP, 7000)):
            proc = subprocess.Popen(["ip", "netns", "exec", ns, sys.executable, "-c", _SERVER, host, str(port)],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            cls.servers.append(proc)
            if proc.stdout.readline().strip() != "ready":
                raise AssertionError(f"server {host}:{port} did not start")

    def reach(self, ns, host, port) -> bool:
        return sh(sys.executable, "-c", _CLIENT, host, str(port), ns=ns, check=False).returncode == 0

    def run_in_pi(self, cmd):
        return sh(*cmd, ns=PI, check=False)

    def test_routing_past_nginx_is_dropped_and_everything_legitimate_still_works(self):
        # Before: the bypass is real.
        self.assertTrue(self.reach(LAN, CONTAINER, 9090), "the harness does not reproduce the bypass")
        self.assertTrue(self.reach(LAN, GATEWAY, 8769), "the harness does not reproduce the gateway path")
        self.assertTrue(self.reach(LAN, PI_LAN, 8081))

        rules = firewall.build_rules(SUBNET, GATEWAY)
        self.assertEqual(firewall.apply(rules, run=self.run_in_pi), (True, True, ""))

        # After: routed to a container or to the agent's gateway address: dropped.
        self.assertFalse(self.reach(LAN, CONTAINER, 9090))
        self.assertFalse(self.reach(LAN, CONTAINER, 80))
        self.assertFalse(self.reach(LAN, GATEWAY, 8769))
        # The published port (DNATed by "Docker") still works.
        self.assertTrue(self.reach(LAN, PI_LAN, 8081))
        # nginx in a container still reaches the agent on the gateway address.
        self.assertTrue(self.reach(CTR, GATEWAY, 8769))
        # A container's own outbound connection (and its replies) still work.
        self.assertTrue(self.reach(CTR, LAPTOP, 7000))

        # Idempotent in the real kernel: nothing changes, nothing duplicates.
        self.assertEqual(firewall.apply(rules, run=self.run_in_pi), (True, False, ""))
        listing = sh("iptables", "-w", "-S", ns=PI).stdout
        self.assertEqual(listing.count(f"-j {firewall.CHAIN_FWD}"), 1, listing)
        self.assertEqual(listing.count(f"-j {firewall.CHAIN_IN}"), 1, listing)

        # A "Docker restart" that recreates DOCKER-USER loses our jump: the
        # next apply (the agent's periodic re-check) restores it.
        sh("iptables", "-w", "-F", "DOCKER-USER", ns=PI)
        sh("iptables", "-w", "-A", "DOCKER-USER", "-j", "RETURN", ns=PI)
        self.assertTrue(self.reach(LAN, CONTAINER, 9090))
        self.assertEqual(firewall.apply(rules, run=self.run_in_pi), (True, True, ""))
        self.assertFalse(self.reach(LAN, CONTAINER, 9090))
        time.sleep(0)  # keep the order of the probes explicit


if __name__ == "__main__":
    unittest.main()
