"""Cause-aware Hugging Face errors (spec §7.5, R5-4b).

``classify_hf_error`` recognises the library classes by module and name, never
by importing them, so the first half of this file builds the eight measured
shapes from stand-ins that carry the real modules and class names (the
deps-free CI suite has neither httpx nor huggingface_hub). The second half
reproduces the same eight shapes with the REAL libraries against a local HTTP
server, exactly as measured in the server image (huggingface_hub 1.x); it runs
wherever those libraries are installed (the server image) and is skipped
elsewhere.
"""

from __future__ import annotations

import ast
import json
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from physical_ai_server.data_processing import hf_errors as he
from physical_ai_server.data_processing import record_texts_de as texts


def _cls(module, name, *bases):
    return type(name, bases or (Exception,), {'__module__': module})


# stand-ins with the real module paths and class hierarchy
HttpxTransportError = _cls('httpx', 'TransportError')
HttpxConnectError = _cls('httpx', 'ConnectError', HttpxTransportError)
HttpxRemoteProtocolError = _cls('httpx', 'RemoteProtocolError', HttpxTransportError)
HttpxHTTPStatusError = _cls('httpx', 'HTTPStatusError')
HttpcoreConnectError = _cls('httpcore', 'ConnectError')
HttpcoreRemoteProtocolError = _cls('httpcore', 'RemoteProtocolError')
HfHubHTTPError = _cls('huggingface_hub.errors', 'HfHubHTTPError', HttpxHTTPStatusError)
LocalTokenNotFoundError = _cls('huggingface_hub.errors', 'LocalTokenNotFoundError', OSError)
OfflineModeIsEnabled = _cls('huggingface_hub.errors', 'OfflineModeIsEnabled', ConnectionError)
RequestsRequestException = _cls('requests.exceptions', 'RequestException', OSError)
RequestsConnectionError = _cls('requests.exceptions', 'ConnectionError', RequestsRequestException)
RequestsReadTimeout = _cls('requests.exceptions', 'ReadTimeout',
                           _cls('requests.exceptions', 'Timeout', RequestsRequestException))


class _Response:
    def __init__(self, status_code):
        self.status_code = status_code


def _chain(*excs):
    """excs[0] raised from excs[1] raised from …; returns excs[0]."""
    for outer, inner in zip(excs, excs[1:]):
        outer.__cause__ = inner
    return excs[0]


def _status(cls, code, msg='x'):
    exc = cls(msg)
    exc.response = _Response(code)
    return exc


def _shapes():
    return {
        'drop': (_chain(HttpxRemoteProtocolError('Server disconnected without sending a response.'),
                        HttpcoreRemoteProtocolError('x')), 'network'),
        'closedport': (_chain(HttpxConnectError('[Errno 111] Connection refused'),
                              HttpcoreConnectError('x'), ConnectionRefusedError(111, 'refused')),
                       'network'),
        'dns': (_chain(HttpxConnectError('[Errno -2] Name or service not known'),
                       HttpcoreConnectError('x'), socket.gaierror(-2, 'Name or service')),
                'network'),
        '401': (_chain(_status(HfHubHTTPError, 401, 'Invalid user token.'),
                       _status(HfHubHTTPError, 401), _status(HttpxHTTPStatusError, 401)), 'auth'),
        '403': (_chain(_status(HfHubHTTPError, 403, '403 Forbidden: None.'),
                       _status(HttpxHTTPStatusError, 403)), 'auth'),
        '503': (_chain(_status(HfHubHTTPError, 503, "Server error '503 Service Unavailable'"),
                       _status(HttpxHTTPStatusError, 503)), 'server'),
        '429': (_chain(_status(HfHubHTTPError, 429, "You've hit the rate limit"),
                       _status(HfHubHTTPError, 429), _status(HttpxHTTPStatusError, 429)), 'busy'),
        'notoken': (LocalTokenNotFoundError('Token is required to call the /whoami-v2 endpoint'),
                    'auth'),
    }


@pytest.mark.parametrize('label', sorted(_shapes()))
def test_the_eight_measured_shapes(label):
    exc, expected = _shapes()[label]
    assert he.classify_hf_error(exc) == expected


@pytest.mark.parametrize('exc,expected', [
    (OfflineModeIsEnabled('Cannot reach …: offline mode is enabled.'), 'network'),
    (_chain(RequestsConnectionError('HTTPConnectionPool: Max retries exceeded'),
            ConnectionRefusedError(111, 'refused')), 'network'),
    (RequestsReadTimeout('read timeout=10'), 'network'),
    (TimeoutError('timed out'), 'network'),
    (ConnectionResetError(104, 'reset'), 'network'),
    (socket.gaierror(-3, 'Temporary failure'), 'network'),
    (_status(HfHubHTTPError, 500), 'server'),
    (_status(HfHubHTTPError, 502), 'server'),
    (ValueError('something else'), None),
    (_status(HfHubHTTPError, 404, '404 Not Found'), None),
    (RuntimeError('plain'), None),
])
def test_other_shapes(exc, expected):
    assert he.classify_hf_error(exc) == expected


def test_a_wrapped_cause_is_found_through_context_and_cause():
    inner = _chain(HttpxConnectError('refused'), ConnectionRefusedError())
    try:
        try:
            raise inner
        except Exception:
            raise RuntimeError('upload failed')      # implicit __context__
    except RuntimeError as outer:
        assert he.classify_hf_error(outer) == 'network'
    wrapped = RuntimeError('upload failed')
    wrapped.__cause__ = _status(HfHubHTTPError, 429)
    assert he.classify_hf_error(wrapped) == 'busy'


def test_the_chain_is_walked_at_most_8_deep_and_survives_a_cycle():
    deep = [RuntimeError(f'level {i}') for i in range(9)] + [_status(HfHubHTTPError, 503)]
    assert he.classify_hf_error(_chain(*deep)) is None
    shallow = [RuntimeError(f'level {i}') for i in range(7)] + [_status(HfHubHTTPError, 503)]
    assert he.classify_hf_error(_chain(*shallow)) == 'server'
    a, b = RuntimeError('a'), RuntimeError('b')
    a.__cause__, b.__cause__ = b, a
    assert he.classify_hf_error(a) is None


def test_a_status_code_beats_the_text_and_text_markers_are_the_last_resort():
    # „connection“ in a 401's text is still auth
    assert he.classify_hf_error(_status(HfHubHTTPError, 401, 'connection to hub')) == 'auth'
    # no class, no status: the long-standing markers
    assert he.classify_hf_error(RuntimeError('Invalid user token')) == 'auth'
    assert he.classify_hf_error(RuntimeError('401 Client Error')) == 'auth'
    assert he.classify_hf_error(RuntimeError('Server disconnected')) == 'network'
    assert he.classify_hf_error(RuntimeError('Read timed out.')) == 'network'
    assert he.classify_hf_error(RuntimeError('name resolution failed')) == 'network'
    assert he.classify_hf_error(None) is None


def test_a_boolean_status_is_not_a_status():
    exc = RuntimeError('x')
    exc.response = _Response(True)
    assert he.classify_hf_error(exc) is None


def test_sentences():
    assert he.hf_error_sentence_de(_status(HfHubHTTPError, 401)) == texts.HF_AUTH_ERROR_DE
    assert he.hf_error_sentence_de(socket.gaierror()) == texts.HF_NETWORK_ERROR_DE
    assert he.hf_error_sentence_de(_status(HfHubHTTPError, 429)) == texts.HF_BUSY_ERROR_DE
    assert he.hf_error_sentence_de(_status(HfHubHTTPError, 503)) == texts.HF_SERVER_ERROR_DE
    assert he.hf_error_sentence_de(ValueError('x')) is None


def test_module_imports_no_library():
    tree = ast.parse(open(he.__file__, encoding='utf-8').read())
    top = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top.update(a.name.split('.')[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            top.add((node.module or '').split('.')[0])
    assert top <= {'__future__', 'importlib', 'os', 'socket', 'typing'}, top


# ── the real libraries (server image) ────────────────────────────────────────

class _Hub(BaseHTTPRequestHandler):
    mode = 'ok'

    def log_message(self, *a):
        pass

    def do_GET(self):
        mode = _Hub.mode
        if mode == 'drop':
            self.connection.shutdown(socket.SHUT_RDWR)
            self.close_connection = True
            return
        code = {'401': 401, '403': 403, '503': 503, '429': 429}.get(mode, 200)
        body = json.dumps({'error': 'x'} if code != 200 else {'name': 'u', 'orgs': []}).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope='module')
def hub_server():
    srv = HTTPServer(('127.0.0.1', 0), _Hub)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv.server_address[1]
    srv.shutdown()


def _free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.mark.parametrize('label,expected', [
    ('drop', 'network'), ('closedport', 'network'), ('dns', 'network'), ('401', 'auth'),
    ('403', 'auth'), ('503', 'server'), ('429', 'busy'), ('notoken', 'auth')])
def test_the_eight_shapes_with_the_real_libraries(hub_server, label, expected, monkeypatch,
                                                  tmp_path):
    pytest.importorskip('httpx')
    hub = pytest.importorskip('huggingface_hub')
    monkeypatch.delenv('HF_TOKEN', raising=False)
    monkeypatch.setenv('HF_HOME', str(tmp_path))
    monkeypatch.setenv('HF_HUB_DISABLE_IMPLICIT_TOKEN', '1')
    _Hub.mode = label
    endpoint = f'http://127.0.0.1:{hub_server}'
    token = 'hf_x'
    if label == 'closedport':
        endpoint = f'http://127.0.0.1:{_free_port()}'
    elif label == 'dns':
        endpoint = 'http://nonexistent.invalid'
    elif label == 'notoken':
        token = None
    if label == 'notoken':
        # no token anywhere: env cleared above, the stored-token files point
        # into an empty directory
        import huggingface_hub.constants as hub_constants
        for name in ('HF_TOKEN_PATH', 'HF_STORED_TOKENS_PATH'):
            if hasattr(hub_constants, name):
                monkeypatch.setattr(hub_constants, name, str(tmp_path / name.lower()))
    api = hub.HfApi(endpoint=endpoint, token=token)
    with pytest.raises(Exception) as info:
        api.whoami()
    assert he.classify_hf_error(info.value) == expected, (
        label, type(info.value), os.environ.get('HF_HOME'))
