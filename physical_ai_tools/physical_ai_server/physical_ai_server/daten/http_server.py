#!/usr/bin/env python3
#
# Copyright 2026 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The Daten sidecar: a READ-ONLY HTTP endpoint for the student's datasets
(spec §B, owner decision D3).

A separate process (``python3 -m physical_ai_server.daten.http_server``),
started by ``launch/physical_ai_server_bringup.launch.py`` with
``prefix='nice -n 10'`` (R-15: priority comes from the launch prefix, never
from ``os.nice``, which would change only the calling thread after the module
imports started theirs). Every byte-serving, parquet-reading, PyAV-muxing,
hint-computing and hub-reading step runs HERE, so it can never take the node's
GIL. It imports nothing from ROS (``rclpy`` and ``physical_ai_interfaces``
are absent from its module graph — a test fences this) and never LeRobot.

Port ``contract.HTTP_PORT`` (8095) on ``0.0.0.0`` inside the container: the
manager's nginx reaches it over ``ros_net`` (``location /daten-api/``); no
compose publishes it. GET/HEAD only. Every route but ``/health`` carries a
short-lived link token minted by the node (``/daten/command`` action ``link``)
— never by this process — so rosbridge stays the only root of trust; the
scope is ``lib`` or ``ds:<ns>/<name>`` and the dataset of a ``ds`` route comes
from the token, never from the URL. Errors are JSON ``{"error": <code>}`` and
never echo a path or a parameter; nothing is ever ``text/html``.

Load limits (§B8, R-3, R-21): at most ``MAX_CONNECTIONS`` sockets (beyond:
``503 overloaded``, ``Retry-After: 2``, closed), each with a
``SOCKET_TIMEOUT_S`` timeout; a media pool (``MEDIA_WORKERS`` at once,
``MEDIA_QUEUE_MAX`` waiting up to ``MEDIA_WAIT_S``) for anything that reads
parquet or video — its queue is as deep as the connection cap, so a media
request the cap admits waits for a slot and is refused only past
``MEDIA_WAIT_S`` (T2-6); its own hub pool (``HUB_WORKERS``/``HUB_QUEUE_MAX``/
``HUB_WAIT_S``) so a slow Hugging Face never holds a media slot; byte serving
from the clip cache is not gated.

This module imports only the stdlib, ``contract`` and ``link_tokens`` at module
level; ``library`` and ``hub_reads`` (pyarrow, PyAV, numpy) are loaded by
``load_modules`` when the server is built, so the Range parser and the pools
are testable without them.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


def _daten(name):
    try:
        return importlib.import_module(f'physical_ai_server.daten.{name}')
    except ImportError:
        key = f'_edubotics_daten_{name}'
        module = sys.modules.get(key)
        if module is None:
            spec = importlib.util.spec_from_file_location(key, str(Path(__file__).with_name(f'{name}.py')))
            module = importlib.util.module_from_spec(spec)
            sys.modules[key] = module
            spec.loader.exec_module(module)
        return module


C = _daten('contract')
LT = _daten('link_tokens')

STATUS_OF = {'invalid': 400, 'token_invalid': 403, 'token_expired': 403, 'scope': 403, 'not_found': 404,
             'in_session': 409, 'incomplete': 409, 'unsupported': 409, 'unplayable': 409, 'range': 416,
             'overloaded': 503, 'internal': 500}
JSON_TYPE = 'application/json; charset=utf-8'
MEDIA_CACHE = 'private, max-age=300'
_RANGE = re.compile(r'bytes=(\d*)-(\d*)')
_ROUTES = [
    ('health', re.compile(r'/health')),
    ('library', re.compile(r'/lib/(?P<t>[^/]+)/library')),
    ('probe', re.compile(r'/lib/(?P<t>[^/]+)/hub/probe')),
    ('summary', re.compile(r'/ds/(?P<t>[^/]+)/summary')),
    ('hubstate', re.compile(r'/ds/(?P<t>[^/]+)/hubstate')),
    ('data', re.compile(r'/ds/(?P<t>[^/]+)/episode/(?P<i>\d{1,7})/data')),
    ('video', re.compile(r'/ds/(?P<t>[^/]+)/episode/(?P<i>\d{1,7})/video/(?P<c>\d{1,3})\.mp4')),
    ('thumb', re.compile(r'/ds/(?P<t>[^/]+)/thumb\.jpg')),
]


def parse_range(header, size):
    """The ONE Range parser of every byte route (§B3): ``('full', None)`` (no
    or an ignorable header: a multi-range, garbage), ``('partial', (start,
    end))`` (inclusive), or ``('unsatisfiable', None)`` → 416."""
    if not header:
        return 'full', None
    header = header.strip()
    if ',' in header:
        return 'full', None                      # one range only; a multi-range gets the whole body
    m = _RANGE.fullmatch(header)
    if not m or (m.group(1) == '' and m.group(2) == ''):
        return 'full', None
    if m.group(1) == '':                         # bytes=-n: the last n bytes
        n = int(m.group(2))
        if n == 0 or size == 0:
            return 'unsatisfiable', None
        return 'partial', (max(0, size - n), size - 1)
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else size - 1
    if start >= size or start > end:
        return 'unsatisfiable', None
    return 'partial', (start, min(end, size - 1))


def _etag_matches(header, etag):
    """``If-None-Match`` (a list, ``*``, weak validators) against our strong ETag."""
    if not header:
        return False
    tags = [t.strip() for t in header.split(',')]
    return '*' in tags or etag in tags or ('W/' + etag) in tags


class Overloaded(Exception):
    pass


class Pool:
    """``workers`` run at once; up to ``queue_max`` further callers WAIT, each at
    most ``wait_s``; a caller finding the queue full, or waiting longer, is
    ``Overloaded``."""

    def __init__(self, workers, queue_max, wait_s):
        self._sem = threading.BoundedSemaphore(int(workers))
        self._lock = threading.Lock()
        self._waiting = 0
        self.queue_max = int(queue_max)
        self.wait_s = float(wait_s)

    def __enter__(self):
        if self._sem.acquire(blocking=False):
            return self
        with self._lock:
            if self._waiting >= self.queue_max:
                raise Overloaded()
            self._waiting += 1
        try:
            if not self._sem.acquire(timeout=self.wait_s):
                raise Overloaded()
        finally:
            with self._lock:
                self._waiting -= 1
        return self

    def __exit__(self, *exc):
        self._sem.release()
        return False


class Reply:
    def __init__(self, status, body=b'', ctype=JSON_TYPE, headers=None):
        self.status = status
        self.body = body
        self.ctype = ctype
        self.headers = dict(headers or {})


def json_reply(obj, status=200):
    return Reply(status, json.dumps(obj, ensure_ascii=False, separators=(',', ':')).encode('utf-8'),
                 JSON_TYPE, {'Cache-Control': 'no-store'})


def error_reply(code):
    reply = json_reply({'error': code}, STATUS_OF.get(code, 500))
    if code == 'overloaded':
        reply.headers['Retry-After'] = '2'
    return reply


def load_modules():
    """The sidecar's heavy half (pyarrow/PyAV/numpy, never ROS or LeRobot)."""
    return _daten('library'), _daten('hub_reads')


class Sidecar:
    """Routing + the two pools; the transport-free core of the server."""

    def __init__(self, secret, library, hub, media=None, hub_pool=None):
        self.secret = secret
        self.library = library
        self.hub = hub
        self.media = media or Pool(C.MEDIA_WORKERS, C.MEDIA_QUEUE_MAX, C.MEDIA_WAIT_S)
        self.hub_pool = hub_pool or Pool(C.HUB_WORKERS, C.HUB_QUEUE_MAX, C.HUB_WAIT_S)

    def handle(self, raw_path, headers):
        try:
            return self._handle(raw_path, headers)
        except Overloaded:
            return error_reply('overloaded')
        except Exception as e:  # noqa: BLE001 — never a traceback or a path to the client
            code = getattr(e, 'code', None)
            if code in STATUS_OF:
                return error_reply(code)
            print(f'[daten-sidecar] internal error: {type(e).__name__}', file=sys.stderr, flush=True)
            return error_reply('internal')

    def _scope(self, token, kind):
        scope, error = LT.read_scope(self.secret, token)
        if error:
            raise _Err(error)
        if kind == 'lib':
            if scope != LT.LIB_SCOPE:
                raise _Err('scope')
            return None
        if not scope.startswith(LT.DS_SCOPE_PREFIX):
            raise _Err('scope')
        return scope[len(LT.DS_SCOPE_PREFIX):]

    def _handle(self, raw_path, headers):
        parts = urlsplit(raw_path)
        if not parts.path.startswith(C.API_PREFIX + '/'):
            return error_reply('not_found')
        sub = parts.path[len(C.API_PREFIX):]
        query = parse_qs(parts.query, keep_blank_values=True)
        for name, rx in _ROUTES:
            m = rx.fullmatch(sub)
            if m:
                break
        else:
            return error_reply('not_found')
        if name == 'health':
            return json_reply({'v': C.SCHEMA_VERSION})
        if name in ('library', 'probe'):
            self._scope(m.group('t'), 'lib')
            return self._library(query) if name == 'library' else self._probe(query)
        dataset_id = self._scope(m.group('t'), 'ds')    # the dataset comes from the token, never the URL
        self.library.path_of(dataset_id)                # validates + confines: invalid / not_found
        if name == 'summary':
            return json_reply(self.library.summary(dataset_id, gate=self.media))
        if name == 'hubstate':
            with self.hub_pool:
                return json_reply(self.hub.hubstate(self.library, dataset_id))
        if name == 'data':
            with self.media:
                return json_reply(self.library.episode_data(dataset_id, int(m.group('i'))))
        if name == 'video':
            clip = self.library.clip(dataset_id, int(m.group('i')), int(m.group('c')), gate=self.media)
            return self._bytes(clip, 'video/mp4', headers)
        return self._bytes(self.library.thumb(dataset_id, gate=self.media), 'image/jpeg', headers)

    def _library(self, query):
        def one(key):
            return (query.get(key) or [''])[0]
        namespaces = [n for n in one('ns').split(',') if n]
        ids = [i for i in one('ids').split(',') if i] if 'ids' in query else None
        if len(namespaces) > C.MAX_LINK_DATASETS or (ids is not None and len(ids) > C.MAX_LINK_DATASETS):
            raise _Err('invalid')
        namespaces = self.library.namespaces_present(namespaces)        # valid names only, deduplicated
        if ids is not None:
            ids = [i for i in dict.fromkeys(ids) if self.library_module.is_valid_id(i)]
        if one('hub') not in ('', '0', '1'):
            raise _Err('invalid')
        hub_asked = one('hub') == '1'
        with self.media:
            local = self.library.scan(namespaces, ids=ids)
        if not hub_asked:
            hub, views, listed = self.hub.library_hub(namespaces, local, ids=ids, hub_asked=False)
            sync = self.library_module.sync_map(self.library, local, views, listed)
        else:
            with self.hub_pool:                         # the content decisions list the hub too (§B8)
                hub, views, listed = self.hub.library_hub(namespaces, local, ids=ids, hub_asked=True,
                                                          records=self.library.records(local))
                failed = hub.get('state') in ('unreachable', 'auth')
                sync = self.library_module.sync_map(self.library, local, views, listed,
                                                    default_view={'state': 'unreachable'} if failed else None)
        return json_reply({'v': C.SCHEMA_VERSION, 'robot_type': self.library.robot_type, 'local': local,
                           'hub': hub, 'sync': sync})

    def _probe(self, query):
        repo = (query.get('repo') or [''])[0]
        with self.hub_pool:
            return json_reply(self.hub.probe(repo))

    @property
    def library_module(self):
        return sys.modules[type(self.library).__module__]

    def _bytes(self, data_etag, ctype, headers):
        data, etag = data_etag
        base = {'Accept-Ranges': 'bytes', 'Cache-Control': MEDIA_CACHE, 'ETag': etag}
        if etag and _etag_matches(headers.get('If-None-Match'), etag):
            return Reply(304, b'', ctype, base)
        kind, rng = parse_range(headers.get('Range'), len(data))
        if kind == 'unsatisfiable':
            r = error_reply('range')
            r.headers['Content-Range'] = f'bytes */{len(data)}'
            return r
        if kind == 'partial':
            start, end = rng
            return Reply(206, data[start:end + 1], ctype,
                         dict(base, **{'Content-Range': f'bytes {start}-{end}/{len(data)}'}))
        return Reply(200, data, ctype, base)


class _Err(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    timeout = C.SOCKET_TIMEOUT_S
    server_version = 'edubotics-daten'
    sys_version = ''

    def log_message(self, *args):          # the path carries a token: never logged
        return None

    def _send(self, reply, head=False):
        self.send_response(reply.status)
        self.send_header('Content-Type', reply.ctype)
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Length', str(len(reply.body)))
        for k, v in reply.headers.items():
            self.send_header(k, v)
        self.end_headers()
        if not head and reply.body:
            self.wfile.write(reply.body)

    def do_GET(self):
        self._send(self.server.sidecar.handle(self.path, self.headers))

    def do_HEAD(self):
        self._send(self.server.sidecar.handle(self.path, self.headers), head=True)

    def _not_allowed(self):
        self.close_connection = True
        reply = json_reply({'error': 'invalid'}, 405)
        reply.headers['Allow'] = 'GET, HEAD'
        self._send(reply)

    do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _not_allowed

    def send_error(self, code, message=None, explain=None):
        """The library's own error page is HTML; ours is JSON (§B6). An unknown
        method (501 there) is 405 here like every other non-GET/HEAD. A request
        line that failed to parse leaves the version at HTTP/0.9, which would
        drop the status line and headers; the refusal always carries them."""
        self.close_connection = True
        if getattr(self, 'request_version', 'HTTP/0.9') == 'HTTP/0.9':
            self.request_version = 'HTTP/1.1'
        try:
            if code == 501:
                self._not_allowed()
            else:
                self._send(json_reply({'error': 'not_found' if code == 404 else 'invalid'}, code))
        except OSError:
            pass


_OVERLOADED_RAW = (b'HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json; charset=utf-8\r\n'
                   b'X-Content-Type-Options: nosniff\r\nRetry-After: 2\r\nCache-Control: no-store\r\n'
                   b'Content-Length: 22\r\nConnection: close\r\n\r\n{"error":"overloaded"}')
_REFUSERS = 8                      # connections being refused at once; beyond it: closed at once


def _refuse(request, slots):
    """Answer one connection over the cap with the minimal 503 and close it.
    The request head is read first (bounded) so the close does not reset the
    connection under an unread request and lose the answer."""
    try:
        request.settimeout(2.0)
        head = b''
        while b'\r\n\r\n' not in head and len(head) < 16384:
            chunk = request.recv(4096)
            if not chunk:
                break
            head += chunk
        request.sendall(_OVERLOADED_RAW)
        request.shutdown(socket.SHUT_WR)
    except OSError:
        pass
    finally:
        try:
            request.close()
        except OSError:
            pass
        slots.release()


def listen_backlog(max_connections=C.MAX_CONNECTIONS):
    """The listen backlog: twice the connection cap. socketserver's default of 5
    made a burst of parallel connects (a page loading 30 thumbnails) overflow
    the accept queue on Linux; the kernel then drops the SYN and the client
    retries only after a full second. The cap is enforced after ``accept``
    (``Server.process_request``), so the queue must hold a burst of at least the
    cap's size, plus the connections being refused meanwhile. Never below
    twice the contract's cap, whatever cap a caller passes."""
    return 2 * max(int(max_connections), C.MAX_CONNECTIONS)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, sidecar, max_connections=C.MAX_CONNECTIONS):
        self.sidecar = sidecar
        self._connections = threading.BoundedSemaphore(int(max_connections))
        self._refusers = threading.BoundedSemaphore(_REFUSERS)
        self.request_queue_size = listen_backlog(max_connections)     # read by server_activate's listen()
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        if not self._connections.acquire(blocking=False):
            if self._refusers.acquire(blocking=False):
                threading.Thread(target=_refuse, args=(request, self._refusers), daemon=True).start()
            else:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._connections.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connections.release()


def make_server(host='0.0.0.0', port=C.HTTP_PORT, *, library=None, hub=None, secret=None,
                max_connections=C.MAX_CONNECTIONS, media=None, hub_pool=None):
    """The server with the rig's library and hub reads (each injectable)."""
    library_mod, hub_mod = load_modules()
    if library is None:
        library = library_mod.Library(robot_type=_rig_robot_type())
    if hub is None:
        hub = hub_mod.HubReads(robot_type=library.robot_type)
    secret = secret if secret is not None else LT.load_or_create_secret()
    sidecar = Sidecar(secret, library, hub, media=media, hub_pool=hub_pool)
    return Server((host, port), sidecar, max_connections=max_connections)


def _rig_robot_type():
    """The dataset robot type this rig records (``data_robot_type``)."""
    try:
        from physical_ai_server import robot_profiles
        return robot_profiles.resolve(os.environ.get('EDUBOTICS_ROBOT_TYPE')).data_robot_type
    except Exception:  # noqa: BLE001
        return 'omx_f'


def main():
    _, hub_mod = load_modules()
    hub_mod.install_client_factory(C.HUB_CALL_TIMEOUT_S)          # G-6: every hub request bounded
    server = make_server()
    server.sidecar.library.start_hint_worker()
    server.sidecar.library.start_remember_worker()          # T2-1
    print(f'[daten-sidecar] listening on 0.0.0.0:{C.HTTP_PORT}', flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
