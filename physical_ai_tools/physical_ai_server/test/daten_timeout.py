"""A per-test time limit for the Daten 2.0 tests that wait on threads, sockets
or processes (V1-3, the pytest twin of robotis_ai_setup/tests/timeout_guard.py):
a wait the code under test never ends FAILS within the limit instead of hanging
the run until the CI job's own timeout.

Not a test module; a test module opts in by importing the fixture:
``from daten_timeout import per_test_time_limit  # noqa: F401`` (an autouse
fixture imported into a module applies to every test in it). The limit is the
module's ``TEST_TIMEOUT_S`` when set, else ``DEFAULT_TIMEOUT_S``.

``SIGALRM`` raises ``TestTimedOut`` in the main thread wherever the test waits;
it derives from ``BaseException`` so the product's ``except Exception`` cannot
swallow it, and it re-fires every ``REFIRE_S`` until the test has returned.
POSIX main thread only; elsewhere the fixture does nothing.
"""

import signal
import threading

import pytest

DEFAULT_TIMEOUT_S = 120.0
REFIRE_S = 5.0


class TestTimedOut(BaseException):
    """A test ran past its time limit (a hang turned into a failure)."""

    __test__ = False                                  # not a test class


@pytest.fixture(autouse=True)
def per_test_time_limit(request):
    limit = getattr(request.module, 'TEST_TIMEOUT_S', DEFAULT_TIMEOUT_S)
    if not limit or not hasattr(signal, 'SIGALRM') or threading.current_thread() is not threading.main_thread():
        yield
        return
    name = request.node.nodeid

    def fire(signum, frame):
        raise TestTimedOut(f'{name} ran longer than {limit:g} s (a wait that never ends)')
    previous = signal.signal(signal.SIGALRM, fire)
    signal.setitimer(signal.ITIMER_REAL, float(limit), REFIRE_S)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
