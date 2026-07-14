"""Cross-platform unit tests for pi_agent.lan_ip.

Deps-free: the one network call is exercised both for real (returns SOME
string) and mocked (deterministic IP + OSError→""), mirroring the Jetson
agent's ``TestDetectLanIp``. Runs on Linux CI and macOS dev hosts.
"""

from __future__ import annotations

import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# The pi_agent modules use package-relative imports (`from .constants import`
# …), matching the landed config_generator/docker_manager, so they load as
# `pi_agent.<mod>`. Put pi_agent's PARENT (robotis_ai_setup) on sys.path and
# import via the package — works under `cd pi_agent && unittest discover -s tests`.
_ROOT = Path(__file__).resolve().parents[2]  # robotis_ai_setup
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from pi_agent import lan_ip  # noqa: E402


class TestDetectLanIp(unittest.TestCase):
    def test_returns_string(self):
        # Real socket call — should return SOMETHING (LAN IP or empty). On a
        # network-less CI host the UDP socket trick raises OSError and we
        # return "". On a normal host it returns the LAN IP.
        result = lan_ip.detect_lan_ip()
        self.assertIsInstance(result, str)

    def test_returns_getsockname_ip(self):
        mock_sock = MagicMock()
        mock_sock.__enter__ = MagicMock(return_value=mock_sock)
        mock_sock.__exit__ = MagicMock(return_value=None)
        mock_sock.getsockname.return_value = ("192.168.7.42", 51234)
        with patch("pi_agent.lan_ip.socket.socket", return_value=mock_sock):
            self.assertEqual(lan_ip.detect_lan_ip(), "192.168.7.42")
        # No packet is actually sent — only connect() to select the interface.
        mock_sock.connect.assert_called_once_with(("8.8.8.8", 53))

    def test_oserror_returns_empty_string_when_no_interfaces(self):
        # No default route AND no enumerable interface → "". `list_interface_ips`
        # is mocked empty so this is deterministic on a Linux CI host (where
        # `ip` would otherwise return real addresses).
        with patch("pi_agent.lan_ip.socket.socket", side_effect=OSError("ENETUNREACH")), \
             patch("pi_agent.lan_ip.list_interface_ips", return_value=[]):
            self.assertEqual(lan_ip.detect_lan_ip(), "")

    def test_connect_oserror_returns_empty_string_when_no_interfaces(self):
        mock_sock = MagicMock()
        mock_sock.__enter__ = MagicMock(return_value=mock_sock)
        mock_sock.__exit__ = MagicMock(return_value=None)
        mock_sock.connect.side_effect = OSError("no route to host")
        with patch("pi_agent.lan_ip.socket.socket", return_value=mock_sock), \
             patch("pi_agent.lan_ip.list_interface_ips", return_value=[]):
            self.assertEqual(lan_ip.detect_lan_ip(), "")


class TestFallbackIpv4(unittest.TestCase):
    """A8: no default route → enumerate a non-loopback interface IPv4
    (prefer routable, else link-local 169.254.x.x)."""

    def test_prefers_routable_over_link_local(self):
        with patch("pi_agent.lan_ip.socket.socket", side_effect=OSError("no route")), \
             patch("pi_agent.lan_ip.list_interface_ips",
                   return_value=["169.254.7.7", "192.168.9.4", "127.0.0.1"]):
            self.assertEqual(lan_ip.detect_lan_ip(), "192.168.9.4")

    def test_falls_back_to_link_local(self):
        # The Tier-3 direct-ethernet mode: only a link-local address exists.
        with patch("pi_agent.lan_ip.socket.socket", side_effect=OSError("no route")), \
             patch("pi_agent.lan_ip.list_interface_ips",
                   return_value=["127.0.0.1", "169.254.12.34", "fe80::1"]):
            self.assertEqual(lan_ip.detect_lan_ip(), "169.254.12.34")

    def test_skips_loopback_and_ipv6(self):
        with patch("pi_agent.lan_ip.socket.socket", side_effect=OSError("no route")), \
             patch("pi_agent.lan_ip.list_interface_ips",
                   return_value=["127.0.0.1", "::1", "fe80::abcd"]):
            self.assertEqual(lan_ip.detect_lan_ip(), "")


class TestListInterfaceIps(unittest.TestCase):
    _IP_OUTPUT = (
        "1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever\n"
        "2: eth0    inet 192.168.1.5/24 brd 192.168.1.255 scope global eth0\n"
        "3: eth0    inet6 fe80::1234%eth0/64 scope link \n"
        "4: wlan0    inet 10.0.0.9/24 scope global wlan0\n"
    )

    def test_parses_and_skips_loopback(self):
        with patch("pi_agent.lan_ip._run_ip_addr", return_value=self._IP_OUTPUT):
            ips = lan_ip.list_interface_ips()
        self.assertIn("192.168.1.5", ips)
        self.assertIn("10.0.0.9", ips)
        self.assertIn("fe80::1234", ips)   # scope-id stripped
        self.assertNotIn("127.0.0.1", ips)  # lo row skipped

    def test_empty_when_ip_absent(self):
        with patch("pi_agent.lan_ip._run_ip_addr", return_value=""):
            self.assertEqual(lan_ip.list_interface_ips(), [])


if __name__ == "__main__":
    unittest.main()
