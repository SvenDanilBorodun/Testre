"""The Pi's firewall step against routing past nginx (SA-05, 2026-11-13).

``firewall.build_rules`` is pure; ``firewall.apply`` runs against a fake
``iptables`` that keeps real chain state here (the netns harness in
``robotis_ai_setup/tests/test_pi_firewall_netns.py`` runs the same rules in a
real kernel, where root is available). Deps-free.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

SETUP_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SETUP_DIR))

os.environ.setdefault("EDUBOTICS_PI_AUTH_FILE", os.path.join(
    tempfile.mkdtemp(prefix="pi-auth-test-"), "pi_auth.json"))
os.environ.setdefault("EDUBOTICS_PI_CONSOLE", "")

from pi_agent import agent  # noqa: E402
from pi_agent import firewall  # noqa: E402


class FakeIptables:
    """A minimal iptables: filter table, user chains, -L/-N/-S/-C/-F/-A/-I."""

    def __init__(self, chains=("INPUT", "FORWARD", "DOCKER-USER"), binary="iptables"):
        self.binary = binary
        self.chains = {name: [] for name in chains}
        self.calls = []
        self.fail_on = set()

    def __call__(self, cmd):
        self.calls.append(cmd)
        assert cmd[0] == self.binary and cmd[1:4] == ["-w", "-t", "filter"], cmd
        op, chain, *rest = cmd[4:]
        ok = types.SimpleNamespace(returncode=0, stdout="", stderr="")
        bad = types.SimpleNamespace(returncode=1, stdout="", stderr=f"iptables: No chain/target/match by that name.")
        if (op, chain) in self.fail_on:
            return bad
        if op == "-L":
            return ok if chain in self.chains else bad
        if op == "-N":
            if chain in self.chains:
                return bad
            self.chains[chain] = []
            return ok
        if chain not in self.chains:
            return bad
        rules = self.chains[chain]
        if op == "-S":
            lines = [f"-N {chain}"] + [f"-A {chain} " + " ".join(r) for r in rules]
            return types.SimpleNamespace(returncode=0, stdout="\n".join(lines) + "\n", stderr="")
        if op == "-C":
            return ok if tuple(rest) in rules else bad
        if op == "-F":
            rules.clear()
            return ok
        if op == "-A":
            rules.append(tuple(rest))
            return ok
        if op == "-I":
            pos, *args = rest
            rules.insert(int(pos) - 1, tuple(args))
            return ok
        raise AssertionError(cmd)

    def mutations(self):
        return [c for c in self.calls if c[4] in ("-N", "-F", "-A", "-I")]


class TestBuildRules(unittest.TestCase):
    def test_the_ipv4_rule_set(self):
        rules = firewall.build_rules("172.28.0.0/24", "172.28.0.1")
        self.assertEqual(rules["binary"], "iptables")
        self.assertEqual(rules["chains"][firewall.CHAIN_FWD], [
            ("-i", "docker+", "-j", "RETURN"),
            ("-i", "br-+", "-j", "RETURN"),
            ("-m", "conntrack", "--ctstate", "NEW",
             "-m", "conntrack", "!", "--ctstate", "DNAT", "-j", "DROP"),
        ])
        self.assertEqual(rules["chains"][firewall.CHAIN_IN], [
            ("-i", "lo", "-j", "RETURN"),
            ("-i", "docker+", "-j", "RETURN"),
            ("-i", "br-+", "-j", "RETURN"),
            ("-j", "DROP"),
        ])
        self.assertEqual(rules["jumps"], [
            ("DOCKER-USER", ("-d", "172.28.0.0/24", "-j", firewall.CHAIN_FWD)),
            ("INPUT", ("-d", "172.28.0.1/32", "-j", firewall.CHAIN_IN)),
        ])

    def test_a_relocated_subnet_is_followed(self):
        rules = firewall.build_rules("10.231.7.0/24", "10.231.7.1")
        self.assertIn(("DOCKER-USER", ("-d", "10.231.7.0/24", "-j", firewall.CHAIN_FWD)), rules["jumps"])
        self.assertIn(("INPUT", ("-d", "10.231.7.1/32", "-j", firewall.CHAIN_IN)), rules["jumps"])
        # A host address given as the subnet is normalised to its network.
        rules = firewall.build_rules("10.231.7.5/24", "10.231.7.1")
        self.assertEqual(rules["jumps"][0][1][1], "10.231.7.0/24")

    def test_ipv6(self):
        rules = firewall.build_rules("fd00:ed0::/64", "fd00:ed0::1", 6)
        self.assertEqual(rules["binary"], "ip6tables")
        self.assertIn(("INPUT", ("-d", "fd00:ed0::1/128", "-j", firewall.CHAIN_IN)), rules["jumps"])

    def test_a_malformed_or_mixed_input_is_refused(self):
        for subnet, gateway, family in (("x", "172.28.0.1", 4), ("172.28.0.0/24", "x", 4),
                                        ("172.28.0.0/24", "fd00::1", 4),
                                        ("172.28.0.0/24", "172.28.0.1", 6),
                                        ("172.28.0.0/24", "172.28.0.1", 5)):
            with self.subTest(subnet=subnet, gateway=gateway, family=family):
                with self.assertRaises(ValueError):
                    firewall.build_rules(subnet, gateway, family)

    def test_only_traffic_from_outside_docker_is_judged(self):
        """The manager's nginx reaches the agent over the bridge, and the LAN
        reaches the manager's :80 DNATed: neither may meet a DROP."""
        rules = firewall.build_rules("172.28.0.0/24", "172.28.0.1")
        fwd = rules["chains"][firewall.CHAIN_FWD]
        drop = fwd[-1]
        self.assertIn("DNAT", drop)
        self.assertEqual(drop[drop.index("DNAT") - 2], "!")
        self.assertEqual(fwd[-1][-1], "DROP")
        self.assertTrue(all(r[-1] == "RETURN" for r in fwd[:-1]))


class TestIpv6Detection(unittest.TestCase):
    def test_disabled_or_garbage_is_none(self):
        for doc in ("", "not json", "[]", '{"EnableIPv6": false}',
                    '{"EnableIPv6": true, "IPAM": {"Config": [{"Subnet": "172.28.0.0/24"}]}}'):
            with self.subTest(doc=doc):
                self.assertEqual(firewall.ipv6_of_network(doc), (None, None))

    def test_enabled_names_the_v6_half(self):
        doc = ('{"EnableIPv6": true, "IPAM": {"Config": [{"Subnet": "172.28.0.0/24", '
               '"Gateway": "172.28.0.1"}, {"Subnet": "fd00:ed0::/64", "Gateway": "fd00:ed0::1"}]}}')
        self.assertEqual(firewall.ipv6_of_network(doc), ("fd00:ed0::/64", "fd00:ed0::1"))
        doc = '[{"EnableIPv6": true, "IPAM": {"Config": [{"Subnet": "fd00:ed0::/64"}]}}]'
        self.assertEqual(firewall.ipv6_of_network(doc), ("fd00:ed0::/64", "fd00:ed0::1"))


class TestApply(unittest.TestCase):
    def setUp(self):
        self.rules = firewall.build_rules("172.28.0.0/24", "172.28.0.1")
        self.kernel = FakeIptables()

    def test_first_apply_creates_the_chains_and_the_jumps(self):
        ok, changed, detail = firewall.apply(self.rules, run=self.kernel)
        self.assertEqual((ok, changed, detail), (True, True, ""))
        self.assertEqual(self.kernel.chains[firewall.CHAIN_FWD], self.rules["chains"][firewall.CHAIN_FWD])
        self.assertEqual(self.kernel.chains[firewall.CHAIN_IN], self.rules["chains"][firewall.CHAIN_IN])
        self.assertEqual(self.kernel.chains["DOCKER-USER"][0], ("-d", "172.28.0.0/24", "-j", firewall.CHAIN_FWD))
        self.assertEqual(self.kernel.chains["INPUT"][0], ("-d", "172.28.0.1/32", "-j", firewall.CHAIN_IN))
        self.assertTrue(all(c[2] == "-t" and c[1] == "-w" for c in self.kernel.calls))

    def test_re_applying_changes_nothing_and_never_duplicates(self):
        firewall.apply(self.rules, run=self.kernel)
        before = {k: list(v) for k, v in self.kernel.chains.items()}
        self.kernel.calls.clear()
        for _ in range(3):
            self.assertEqual(firewall.apply(self.rules, run=self.kernel), (True, False, ""))
        self.assertEqual(self.kernel.chains, before)
        self.assertEqual(self.kernel.mutations(), [])

    def test_a_docker_restart_that_dropped_our_jump_is_repaired_once(self):
        firewall.apply(self.rules, run=self.kernel)
        # Docker flushes and recreates DOCKER-USER with its own RETURN.
        self.kernel.chains["DOCKER-USER"] = [("-j", "RETURN")]
        ok, changed, _ = firewall.apply(self.rules, run=self.kernel)
        self.assertTrue(ok and changed)
        self.assertEqual(self.kernel.chains["DOCKER-USER"],
                         [("-d", "172.28.0.0/24", "-j", firewall.CHAIN_FWD), ("-j", "RETURN")])
        self.assertEqual(firewall.apply(self.rules, run=self.kernel), (True, False, ""))

    def test_a_drifted_chain_is_rewritten(self):
        firewall.apply(self.rules, run=self.kernel)
        self.kernel.chains[firewall.CHAIN_FWD].append(("-j", "ACCEPT"))
        ok, changed, _ = firewall.apply(self.rules, run=self.kernel)
        self.assertTrue(ok and changed)
        self.assertEqual(self.kernel.chains[firewall.CHAIN_FWD], self.rules["chains"][firewall.CHAIN_FWD])
        self.kernel.chains[firewall.CHAIN_IN] = self.kernel.chains[firewall.CHAIN_IN][:-1]  # DROP lost
        firewall.apply(self.rules, run=self.kernel)
        self.assertEqual(self.kernel.chains[firewall.CHAIN_IN][-1], ("-j", "DROP"))

    def test_no_docker_user_chain_yet_is_an_english_failure_not_an_exception(self):
        del self.kernel.chains["DOCKER-USER"]
        ok, changed, detail = firewall.apply(self.rules, run=self.kernel)
        self.assertFalse(ok)
        self.assertIn("DOCKER-USER", detail)
        self.assertRegex(detail, r"^[ -~]+$")  # plain English ASCII for the maintainer

    def test_a_missing_binary_or_a_hung_one_never_raises(self):
        def missing(cmd):
            raise FileNotFoundError(cmd[0])

        def hung(cmd):
            raise subprocess.TimeoutExpired(cmd, 15)

        self.assertEqual(firewall.apply(self.rules, run=missing)[:2], (False, False))
        self.assertIn("not installed", firewall.apply(self.rules, run=missing)[2])
        self.assertIn("did not answer", firewall.apply(self.rules, run=hung)[2])


class TestTheAgentLoop(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.env = os.path.join(self.dir, ".env")
        self.app = agent.AgentApp(env_file=self.env, auth_file=os.path.join(self.dir, "a.json"))

    def test_the_tick_reads_the_subnet_from_the_env_and_logs_only_on_a_change(self):
        with open(self.env, "w", encoding="utf-8") as f:
            f.write("EDUBOTICS_ROS_NET_SUBNET=10.9.8.0/24\nEDUBOTICS_ROS_NET_GATEWAY=10.9.8.1\n")
        kernel = FakeIptables()
        real_apply = firewall.apply
        with patch.object(agent.firewall, "apply",
                          lambda rules, run=None: real_apply(rules, run=kernel)), \
                patch.object(agent.AgentApp, "_ros_net_ipv6", return_value=(None, None)), \
                self.assertLogs("edubotics-pi-agent", level="INFO") as logs:
            first = self.app._firewall_tick()
            second = self.app._firewall_tick()
        self.assertEqual(first, [("ipv4", True, True, "")])
        self.assertEqual(second, [("ipv4", True, False, "")])
        self.assertIn(("-d", "10.9.8.0/24", "-j", firewall.CHAIN_FWD), kernel.chains["DOCKER-USER"])
        self.assertEqual(len([line for line in logs.output if "firewall ipv4" in line]), 1)

    def test_a_failure_is_logged_once_in_english_and_the_tick_never_raises(self):
        with patch.object(agent.firewall, "apply", return_value=(False, False, "iptables is not installed")), \
                patch.object(agent.AgentApp, "_ros_net_ipv6", return_value=(None, None)), \
                self.assertLogs("edubotics-pi-agent", level="WARNING") as logs:
            self.app._firewall_tick()
            self.app._firewall_tick()
        lines = [line for line in logs.output if "could not apply" in line]
        self.assertEqual(len(lines), 1)
        self.assertIn("iptables is not installed", lines[0])

    def test_ipv6_rules_only_when_ros_net_has_ipv6(self):
        seen = []
        with patch.object(agent.firewall, "apply",
                          side_effect=lambda rules: seen.append(rules["binary"]) or (True, False, "")), \
                patch.object(agent.AgentApp, "_ros_net_ipv6", return_value=("fd00:ed0::/64", "fd00:ed0::1")):
            self.app._firewall_tick()
        self.assertEqual(seen, ["iptables", "ip6tables"])

    def test_boot_starts_the_loop_after_the_manager_is_up(self):
        src = (SETUP_DIR / "pi_agent" / "agent.py").read_text(encoding="utf-8")
        boot = src[src.index("    def boot(self) -> None:"):src.index("    def request_shutdown(self)")]
        self.assertLess(boot.index("docker_manager.start_manager(log=self._log)"),
                        boot.index("target=self._firewall_loop"))


if __name__ == "__main__":
    unittest.main()
