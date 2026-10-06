"""Daten 2.0: every hub call of the sidecar is bounded (spec §B8, G-6, P31).

A local socket that ACCEPTS and never answers stands in for a black-holed Hugging
Face. With the sidecar's client factory installed (``hub_reads.
install_client_factory``, what ``http_server.main`` calls) each of ``whoami``,
``list_repo_refs``, ``list_repo_tree``, ``list_datasets``, ``dataset_info``,
``repo_exists`` and the one-GET ``info.json`` read raises within the bound —
measured as the FIRST call of a fresh process with ``HF_HUB_DISABLE_TELEMETRY=1``
(the bringup launch's setting; without it huggingface_hub makes one hidden
request first). Without the factory a listing call hangs past three bounds (the
hook is what bounds it). The hook lowers a missing or higher timeout and never
raises a caller's lower one. No ``hf_hub_download`` anywhere in ``daten/``.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
import socket
import subprocess
import sys
import textwrap
import threading
import types

import pytest

pytest.importorskip('huggingface_hub')
pytest.importorskip('httpx')

PKG_PARENT = Path(__file__).resolve().parents[1]
DATEN = PKG_PARENT / 'physical_ai_server' / 'daten'
BOUND_S = 1.5


class BlackHole:
    """Accepts every connection and never sends a byte."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(64)
        self.port = self.sock.getsockname()[1]
        self.held = []
        self.stop = False
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        self.sock.settimeout(0.2)
        while not self.stop:
            try:
                conn, _ = self.sock.accept()
                self.held.append(conn)
            except OSError:
                continue

    def close(self):
        self.stop = True
        self.thread.join(2)
        for c in self.held:
            c.close()
        self.sock.close()


@pytest.fixture(scope='module')
def hole():
    h = BlackHole()
    yield h
    h.close()


CALLS = {
    'whoami': 'api.whoami()',
    'list_repo_refs': "api.list_repo_refs('lena-schmidt/omx_f_x', repo_type='dataset')",
    'list_repo_tree': "list(api.list_repo_tree('lena-schmidt/omx_f_x', repo_type='dataset'))",
    'list_datasets': "list(api.list_datasets(author='lena-schmidt'))",
    'dataset_info': "api.dataset_info('lena-schmidt/omx_f_x')",
    'repo_exists': "api.repo_exists('lena-schmidt/omx_f_x', repo_type='dataset')",
    'info_json_get': ("hub_reads.HubReads()._json_at(api, 'lena-schmidt/omx_f_x', "
                      "'0123456789abcdef0123456789abcdef01234567', 'meta/info.json', 'hf_blackholeAAAAAAAAAAAAAAAAAAAAAAAA')"),
}

PROBE = textwrap.dedent('''
    import sys, time
    from physical_ai_server.daten import hub_reads
    if {install}:
        hub_reads.install_client_factory({bound})
    from huggingface_hub import HfApi
    api = HfApi(token='hf_blackholeAAAAAAAAAAAAAAAAAAAAAAAA')
    t0 = time.monotonic()
    try:
        {call}
        print('RETURNED')
    except Exception as e:
        print('RAISED', type(e).__name__)
    print('ELAPSED %.3f' % (time.monotonic() - t0))
''')


def run_probe(hole, call, install, tmp_path, timeout):
    env = dict(os.environ)
    for k in ('HF_HUB_OFFLINE', 'HF_TOKEN', 'HF_TOKEN_PATH', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY'):
        env.pop(k, None)
    env.update({'HF_ENDPOINT': f'http://127.0.0.1:{hole.port}', 'HF_HUB_DISABLE_TELEMETRY': '1',
                'HF_HOME': str(tmp_path), 'NO_PROXY': '*', 'PYTHONPATH': str(PKG_PARENT)})
    code = PROBE.format(install=install, bound=BOUND_S, call=call)
    return subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, env=env,
                          timeout=timeout, cwd=str(PKG_PARENT))


def test_every_hub_call_raises_within_the_bound_on_its_first_call(hole, tmp_path):
    results = {}

    def one(name, call):
        d = tmp_path / name
        d.mkdir()
        results[name] = run_probe(hole, call, True, d, timeout=60)

    threads = [threading.Thread(target=one, args=item) for item in CALLS.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join(90)
    for name in CALLS:
        r = results[name]
        assert r.returncode == 0, (name, r.stderr[-2000:])
        lines = r.stdout.split('\n')
        assert any(line.startswith('RAISED') for line in lines), (name, r.stdout, r.stderr[-1500:])
        elapsed = float(next(line for line in lines if line.startswith('ELAPSED')).split()[1])
        # It really waited for the black hole (not a refused connection) and no longer than the bound.
        assert BOUND_S * 0.8 <= elapsed <= BOUND_S + 1.0, (name, elapsed)


def test_without_the_factory_a_listing_hangs_past_three_bounds(hole, tmp_path):
    with pytest.raises(subprocess.TimeoutExpired):
        run_probe(hole, CALLS['list_datasets'], False, tmp_path, timeout=3 * BOUND_S + 3)


def _request(timeout):
    return types.SimpleNamespace(extensions={} if timeout is None else {'timeout': dict(timeout)})


def test_the_hook_lowers_and_never_raises():
    from physical_ai_server.daten import hub_reads
    r = _request(None)
    hub_reads.bound_request_timeout(r, 10)
    assert r.extensions['timeout'] == {'connect': 10, 'read': 10, 'write': 10, 'pool': 10}
    r = _request({'connect': None, 'read': 30, 'write': 2.5, 'pool': 10})
    hub_reads.bound_request_timeout(r, 10)
    assert r.extensions['timeout'] == {'connect': 10, 'read': 10, 'write': 2.5, 'pool': 10}
    r = _request({'connect': 0.5, 'read': 0.5, 'write': 0.5, 'pool': 0.5})
    hub_reads.bound_request_timeout(r, 10)
    assert r.extensions['timeout'] == {'connect': 0.5, 'read': 0.5, 'write': 0.5, 'pool': 0.5}


def test_the_factory_keeps_the_library_client_and_appends_one_hook():
    from physical_ai_server.daten import hub_reads
    client = hub_reads.make_client_factory(3)()
    try:
        hooks = client.event_hooks['request']
        assert len(hooks) >= 1
        r = _request({'connect': 60, 'read': None, 'write': 1, 'pool': 60})
        hooks[-1](r)
        assert r.extensions['timeout'] == {'connect': 3, 'read': 3, 'write': 1, 'pool': 3}
        assert client.follow_redirects is True
    finally:
        client.close()


def test_no_hf_hub_download_in_daten():
    offenders = []
    for path in sorted(DATEN.glob('*.py')):
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            name = node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else \
                node.name if isinstance(node, ast.alias) else None
            if name == 'hf_hub_download':
                offenders.append(path.name)
    assert offenders == []


def test_the_telemetry_switch_is_the_launchs_first_action():
    tree = ast.parse((PKG_PARENT / 'launch' / 'physical_ai_server_bringup.launch.py').read_text(encoding='utf-8'))
    ld = next(n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, 'id', '') == 'LaunchDescription')
    first = ld.args[0].elts[0]
    assert ast.unparse(first) == "SetEnvironmentVariable('HF_HUB_DISABLE_TELEMETRY', '1')"


def test_the_sidecar_is_started_niced_by_the_launch_never_by_os_nice():
    src = (PKG_PARENT / 'launch' / 'physical_ai_server_bringup.launch.py').read_text(encoding='utf-8')
    tree = ast.parse(src)
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, 'id', '') == 'ExecuteProcess')
    kw = {k.arg: ast.unparse(k.value) for k in call.keywords}
    assert kw['cmd'] == "[sys.executable, '-m', 'physical_ai_server.daten.http_server']"
    assert (kw['name'], kw['prefix'], kw['respawn']) == ("'daten_http'", "'nice -n 10'", 'True')
    assert (kw['respawn_delay'], kw['sigterm_timeout'], kw['output']) == ('2.0', "'3'", "'screen'")
    for path in sorted(DATEN.glob('*.py')):
        calls = [n for n in ast.walk(ast.parse(path.read_text(encoding='utf-8'))) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute) and n.func.attr == 'nice']
        assert calls == [], path.name
