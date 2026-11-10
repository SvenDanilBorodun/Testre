# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""The Orange Pi's one-time login tickets (SA-06, 2026-11-13).

The ticket book (app/services/pi_tickets.py) is tested here directly: single
use, the Pi binding, the expiry, the session bound by the access token, the
bounded memory, no ticket kept in the clear. The routes run through the real
FastAPI stack in a child interpreter (pi_tickets_cases.py, the edge_cases.py
pattern), because other suites of this package stub ``fastapi`` during
discovery."""

from __future__ import annotations

import os
import subprocess
import sys
import unittest

from app.services import pi_tickets

API = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PI_ID = "q8V0f2Lr3nC1sZtA9xY7wB"


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class TicketBook(unittest.TestCase):
    def setUp(self):
        self.mono = _Clock(1000.0)
        self.wall = _Clock(1_800_000_000.0)
        self.store = pi_tickets.TicketStore(now=self.mono, wall=self.wall)

    def mint(self, **over):
        kw = {"user_id": "u-1", "role": "student", "pi_id": PI_ID, "token_exp": self.wall.t + 3600}
        kw.update(over)
        return self.store.mint(**kw)

    def test_a_ticket_is_256_random_bits_and_redeems_once(self):
        ticket = self.mint()
        self.assertTrue(pi_tickets.valid_ticket(ticket))
        self.assertNotEqual(ticket, self.mint())
        got = self.store.redeem(ticket, PI_ID)
        self.assertEqual(got, {"user_id": "u-1", "role": "student",
                               "session_max_s": pi_tickets.SESSION_MAX_S})
        self.assertIsNone(self.store.redeem(ticket, PI_ID))

    def test_only_the_pi_it_was_minted_for_and_one_try(self):
        ticket = self.mint()
        self.assertIsNone(self.store.redeem(ticket, "Zz8V0f2Lr3nC1sZtA9xY7w"))
        self.assertIsNone(self.store.redeem(ticket, PI_ID))

    def test_a_ticket_dies_after_a_minute(self):
        ticket = self.mint()
        self.mono.t += pi_tickets.TICKET_TTL_S
        self.assertIsNone(self.store.redeem(ticket, PI_ID))
        ticket = self.mint()
        self.mono.t += pi_tickets.TICKET_TTL_S - 1
        self.assertIsNotNone(self.store.redeem(ticket, PI_ID))

    def test_the_session_is_bound_by_the_access_token(self):
        ticket = self.mint(token_exp=self.wall.t + 120)
        self.wall.t += 20
        self.assertEqual(self.store.redeem(ticket, PI_ID)["session_max_s"], 100)
        ticket = self.mint(token_exp=self.wall.t + 5)
        self.wall.t += 10
        self.assertIsNone(self.store.redeem(ticket, PI_ID))  # the token ran out
        ticket = self.mint(token_exp=None)
        self.assertEqual(self.store.redeem(ticket, PI_ID)["session_max_s"], pi_tickets.SESSION_MAX_S)
        ticket = self.mint(token_exp=True)  # a bool is not an exp
        self.assertEqual(self.store.redeem(ticket, PI_ID)["session_max_s"], pi_tickets.SESSION_MAX_S)

    def test_malformed_input_is_refused_without_a_lookup(self):
        ticket = self.mint()
        for bad_ticket, bad_pi in ((None, PI_ID), (7, PI_ID), (ticket + "x", PI_ID),
                                   (ticket, None), (ticket, "short"), (ticket, PI_ID + "!")):
            with self.subTest(ticket=bad_ticket, pi=bad_pi):
                self.assertIsNone(self.store.redeem(bad_ticket, bad_pi))
        self.assertEqual(len(self.store), 1)  # no shape refusal spent it
        self.assertIsNotNone(self.store.redeem(ticket, PI_ID))

    def test_mint_refuses_an_unknown_role_or_pi_id(self):
        with self.assertRaises(ValueError):
            self.mint(role="superuser")
        with self.assertRaises(ValueError):
            self.mint(pi_id="x")

    def test_memory_is_bounded_and_expired_tickets_go_first(self):
        store = pi_tickets.TicketStore(now=self.mono, wall=self.wall, max_tickets=5)
        old = [store.mint(user_id="u", role="student", pi_id=PI_ID, token_exp=None) for _ in range(3)]
        self.mono.t += pi_tickets.TICKET_TTL_S + 1
        store.mint(user_id="u", role="student", pi_id=PI_ID, token_exp=None)
        self.assertEqual(len(store), 1)  # the three expired were pruned
        fresh = [store.mint(user_id="u", role="student", pi_id=PI_ID, token_exp=None) for _ in range(7)]
        self.assertEqual(len(store), 5)
        self.assertIsNone(store.redeem(fresh[0], PI_ID))  # the oldest dropped
        self.assertIsNotNone(store.redeem(fresh[-1], PI_ID))
        for t in old:
            self.assertIsNone(store.redeem(t, PI_ID))

    def test_no_ticket_is_kept_in_the_clear(self):
        ticket = self.mint()
        self.assertNotIn(ticket, repr(self.store._tickets))
        for key, record in self.store._tickets.items():
            self.assertNotIn(ticket, key)
            self.assertNotIn(ticket, repr(record))

    def test_the_pi_id_comparison_is_constant_time(self):
        import inspect
        self.assertIn("hmac.compare_digest", inspect.getsource(pi_tickets.TicketStore.redeem))


class RoutesInAFreshInterpreter(unittest.TestCase):
    def test_pi_tickets_cases(self):
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", "app.tests.pi_tickets_cases", "-v"],
            cwd=API, env=env, capture_output=True, text=True, timeout=300,
        )
        out = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, out[-6000:])
        self.assertRegex(out, r"Ran \d+ tests")
        self.assertNotIn("skipped", out.splitlines()[-1])


if __name__ == "__main__":
    unittest.main()
