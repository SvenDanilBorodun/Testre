"""V1-3: ``timeout_guard.BoundedTestCase`` turns a test that waits forever into
a failure within its limit — through ``except Exception`` and through a bare
``except:`` — and leaves no alarm or handler behind.

The hanging cases run in a child process with a hard timeout, so a broken
guard fails this test instead of hanging it."""

import json
import pathlib
import signal
import subprocess
import sys
import textwrap
import time
import unittest

import timeout_guard
from timeout_guard import BoundedTestCase

HERE = pathlib.Path(__file__).resolve().parent

CHILD = textwrap.dedent('''
    import json, sys, time, unittest
    import timeout_guard
    from timeout_guard import BoundedTestCase
    timeout_guard.REFIRE_S = 0.2

    class Hangs(BoundedTestCase):
        TEST_TIMEOUT_S = 0.3

        def test_poll(self):
            while True:
                time.sleep(0.01)

        def test_swallowed_by_except_exception(self):
            while True:
                try:
                    time.sleep(0.05)
                except Exception:          # the product's own broad handler
                    pass

        def test_swallowed_once_by_a_bare_except(self):
            swallowed = []
            while True:
                try:
                    time.sleep(0.05)
                except:                    # the worst case: it catches the first firing
                    swallowed.append(1)
                    if len(swallowed) > 1:
                        raise

    out = {}
    for name in ('test_poll', 'test_swallowed_by_except_exception', 'test_swallowed_once_by_a_bare_except'):
        result = unittest.TestResult()
        t0 = time.monotonic()
        Hangs(name).run(result)
        out[name] = {'errors': [e[1].strip().splitlines()[-1] for e in result.errors],
                     'failures': len(result.failures), 's': time.monotonic() - t0}
    print(json.dumps(out))
''')


@unittest.skipUnless(timeout_guard._available(), 'SIGALRM is POSIX-only')
class TheGuard(unittest.TestCase):

    def test_a_wait_that_never_ends_fails_within_the_limit(self):
        p = subprocess.run([sys.executable, '-c', CHILD], cwd=str(HERE), capture_output=True, text=True,
                           timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        out = json.loads(p.stdout.strip().splitlines()[-1])
        for name, r in out.items():
            with self.subTest(name=name):
                self.assertEqual(len(r['errors']), 1, r)
                self.assertTrue(r['errors'][0].startswith('timeout_guard.TestTimedOut'), r)
                self.assertLess(r['s'], 3.0)

    def test_nothing_is_left_armed(self):
        before = signal.getsignal(signal.SIGALRM)

        class Quick(BoundedTestCase):
            TEST_TIMEOUT_S = 0.3

            def test_ok(self):
                pass
        result = unittest.TestResult()
        Quick('test_ok').run(result)
        self.assertTrue(result.wasSuccessful())
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))
        self.assertIs(signal.getsignal(signal.SIGALRM), before)
        time.sleep(0.5)                                   # nothing fires later

    def test_the_default_limit_is_generous(self):
        self.assertEqual(BoundedTestCase.TEST_TIMEOUT_S, 60.0)


if __name__ == '__main__':
    unittest.main()
