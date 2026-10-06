"""A per-test time limit for the unittest suites (V1-3): a test that waits on
a condition the code under test never meets FAILS within the limit instead of
hanging the whole run until the CI job's own timeout.

Not a test module (the name does not match ``test*.py``); the test modules
import it as a sibling, the way they import each other.

``BoundedTestCase`` arms ``SIGALRM`` around each test's ``run`` (setUp, the
test, tearDown and the cleanups). When it fires, ``TestTimedOut`` is raised in
the main thread wherever the test waits (``time.sleep``, a lock, a join,
``Event.wait`` are all interrupted by a signal on POSIX). It derives from
``BaseException`` so the product's own ``except Exception`` cannot swallow it,
and it re-fires every ``REFIRE_S`` until the test has returned, in case a bare
``except:`` did. unittest records it as an error of that test, the run goes on.

POSIX main thread only (the CI runner, macOS, the server images). Elsewhere
(Windows, a test run from another thread) the guard is a no-op and the test
runs unbounded, as before.
"""

import signal
import threading
import unittest

DEFAULT_TIMEOUT_S = 60.0
REFIRE_S = 5.0


class TestTimedOut(BaseException):
    """A test ran past its time limit (a hang turned into a failure)."""


def _available():
    return hasattr(signal, 'SIGALRM') and hasattr(signal, 'setitimer') \
        and threading.current_thread() is threading.main_thread()


class BoundedTestCase(unittest.TestCase):
    """``TEST_TIMEOUT_S`` per test (class attribute; 0 or None = unbounded)."""

    TEST_TIMEOUT_S = DEFAULT_TIMEOUT_S

    def run(self, result=None):
        limit = self.TEST_TIMEOUT_S
        if not limit or not _available():
            return super().run(result)
        name = self.id()

        def fire(signum, frame):
            raise TestTimedOut(f'{name} ran longer than {limit:g} s (a wait that never ends)')
        previous = signal.signal(signal.SIGALRM, fire)
        signal.setitimer(signal.ITIMER_REAL, float(limit), REFIRE_S)
        try:
            return super().run(result)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
