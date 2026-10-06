"""V1-3: ``daten_timeout.per_test_time_limit`` turns a test that waits forever
into a failure within its limit — through ``except Exception`` and through a
bare ``except:`` — and the next test runs. The hanging module runs in a child
pytest with a hard timeout, so a broken guard fails here instead of hanging."""

import json
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import textwrap

import pytest

HERE = Path(__file__).resolve().parent

HANGS = textwrap.dedent('''
    import time
    import daten_timeout
    from daten_timeout import per_test_time_limit  # noqa: F401
    daten_timeout.REFIRE_S = 0.2
    TEST_TIMEOUT_S = 0.3

    def test_poll():
        while True:
            time.sleep(0.01)

    def test_swallowed_by_except_exception():
        while True:
            try:
                time.sleep(0.05)
            except Exception:              # the product's own broad handler
                pass

    def test_swallowed_once_by_a_bare_except():
        swallowed = []
        while True:
            try:
                time.sleep(0.05)
            except:                        # the worst case: it catches the first firing
                swallowed.append(1)
                if len(swallowed) > 1:
                    raise

    def test_the_next_test_runs():
        assert True
''')


@pytest.mark.skipif(not hasattr(signal, 'SIGALRM'), reason='SIGALRM is POSIX-only')
def test_a_wait_that_never_ends_fails_within_the_limit(tmp_path):
    shutil.copy(HERE / 'daten_timeout.py', tmp_path / 'daten_timeout.py')
    (tmp_path / 'test_hangs.py').write_text(HANGS)
    p = subprocess.run([sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', '-rA', str(tmp_path)],
                       cwd=str(tmp_path), capture_output=True, text=True, timeout=60)
    out = p.stdout
    assert p.returncode == 1, out[-3000:]
    assert '3 failed, 1 passed' in out, out[-3000:]
    assert out.count('TestTimedOut') >= 3, out[-3000:]


def test_the_default_limit_is_generous():
    import daten_timeout
    assert daten_timeout.DEFAULT_TIMEOUT_S == 120.0
    assert json.dumps(daten_timeout.REFIRE_S) == '5.0'
