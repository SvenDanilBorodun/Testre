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

"""The Daten sidecar's Hugging Face reads (spec §B8, §C1, §C3, §E7, §J.4).

READ-ONLY, as the robot's current token (the tmpfs slot ``HF_TOKEN_PATH`` that
holds the signed-in student's token); nothing is ever written to the hub here.

Bounded and cached (§B8, G-6, S-3):

* every hub request is bounded by ``HUB_CALL_TIMEOUT_S`` through a REQUEST
  event hook on the client (``install_client_factory``, called by the
  sidecar's ``main``) that LOWERS ``request.extensions['timeout']`` to the bound
  wherever the caller set none or a higher one and never raises a caller's
  lower bound — a client-level timeout is not enough: ``list_repo_tree``,
  ``list_datasets``, ``dataset_info`` and ``repo_exists`` pass ``timeout=None``
  per request (P31). The bringup launch sets ``HF_HUB_DISABLE_TELEMETRY=1`` so
  the library's hidden first-call request does not add to it;
* everything derived from a head — the top-level tree view (tree ids + last
  commits), ``meta/info.json`` and ``meta/stats.json`` at that head, the
  recursive file listing — is cached in ONE LRU keyed ``(repo_id, head)``,
  ``HUB_CACHE_MAX`` entries, no TTL (a head is immutable); ``whoami`` per token
  fingerprint likewise. The namespace listing (``list_datasets``: its ``sha``
  is main's head, ``private`` can change) is asked on every ``hub=1`` request
  and never cached;
* one library hub listing per token at a time: a concurrent identical request
  waits for the same answer.

Error classes (R-12, P24): a ``RepositoryNotFoundError`` of ANY status — 401
anonymous, 404 with a token — is „not there / no access" FIRST; only then the
``hf_errors`` classes (``auth`` for 401/403, everything else „unreachable").
``hf_hub_download`` is never used here (it retries a timed-out request five
times with backoff).
"""

from __future__ import annotations

import collections
import json
import re
import threading
from typing import Optional

from physical_ai_server.daten import contract as C
from physical_ai_server.data_processing import dataset_sync as S
from physical_ai_server.data_processing import hf_errors
from physical_ai_server.data_processing import hf_token_store
from physical_ai_server.data_processing import hub_sync as HS
from physical_ai_server.data_processing import v3_surgery as V

_REPO_ID = re.compile(C.REPO_ID_RE)
WHOAMI_CACHE_MAX = 32


# ── the request-timeout hook (G-6, H-11) ──────────────────────────────────────

def bound_request_timeout(request, bound_s):
    """Lower each of connect/read/write/pool to ``bound_s`` where the caller set
    none or a higher one; NEVER raise a caller's lower bound."""
    t = request.extensions.get('timeout') or {}
    request.extensions['timeout'] = {
        k: (bound_s if t.get(k) is None else min(t[k], bound_s)) for k in ('connect', 'read', 'write', 'pool')}


def make_client_factory(bound_s=C.HUB_CALL_TIMEOUT_S):
    """A client factory whose clients bound every request. The library's own
    client (``default_client_factory``: its event hook, its redirects) when that
    PRIVATE name resolves — the image's Daten gate asserts it does — else a plain
    ``httpx.Client(follow_redirects=True)`` with the hook (H-11: both bound 7/7)."""
    def factory():
        try:
            from huggingface_hub.utils._http import default_client_factory
            client = default_client_factory()
        except Exception:  # noqa: BLE001 — never crash the sidecar into a respawn loop over it
            import httpx
            client = httpx.Client(follow_redirects=True)
        client.event_hooks.setdefault('request', [])
        client.event_hooks['request'] = list(client.event_hooks['request']) + [
            lambda request: bound_request_timeout(request, bound_s)]
        return client
    return factory


def install_client_factory(bound_s=C.HUB_CALL_TIMEOUT_S):
    import huggingface_hub
    huggingface_hub.set_client_factory(make_client_factory(bound_s))


# ── classification ─────────────────────────────────────────────────────────────

def _is_not_found(error) -> bool:
    seen = 0
    while error is not None and seen < 8:
        if type(error).__name__ in ('RepositoryNotFoundError', 'GatedRepoError') or \
                any(c.__name__ == 'RepositoryNotFoundError' for c in type(error).__mro__):
            return True
        error = error.__cause__ or error.__context__
        seen += 1
    return False


def classify(error) -> str:
    """``not_found`` (FIRST, any status) | ``auth`` | ``unreachable``."""
    if _is_not_found(error):
        return 'not_found'
    return 'auth' if hf_errors.classify_hf_error(error) == 'auth' else 'unreachable'


class _HubError(Exception):
    def __init__(self, state):
        super().__init__(state)
        self.state = state


class HubReads:
    """The hub half of the sidecar (one per process)."""

    def __init__(self, robot_type='omx_f', api_factory=None, token_reader=None, cache_max=C.HUB_CACHE_MAX):
        self.robot_type = robot_type
        self._api_factory = api_factory
        self._token_reader = token_reader or hf_token_store.read
        self.cache_max = int(cache_max)
        self._cache = collections.OrderedDict()       # (repo, head) -> {kind: value}
        self._whoami = collections.OrderedDict()      # fp -> account
        self._lock = threading.Lock()
        self._inflight = {}

    # ── plumbing ──────────────────────────────────────────────────────────

    def token(self):
        token = self._token_reader()
        return (token, hf_token_store.fingerprint(token)) if token else (None, None)

    def api(self, token):
        if self._api_factory is not None:
            return self._api_factory(token)
        from huggingface_hub import HfApi
        return HfApi(token=token)

    def _cached(self, repo, head, kind, fn):
        key = (repo, head)
        with self._lock:
            slot = self._cache.get(key)
            if slot is not None and kind in slot:
                self._cache.move_to_end(key)
                return slot[kind]
        value = fn()
        with self._lock:
            slot = self._cache.setdefault(key, {})
            slot[kind] = value
            self._cache.move_to_end(key)
            while len(self._cache) > self.cache_max:
                self._cache.popitem(last=False)
        return value

    def cache_size(self):
        with self._lock:
            return len(self._cache)

    def _single_flight(self, key, fn):
        with self._lock:
            slot = self._inflight.get(key)
            owner = slot is None
            if owner:
                slot = {'event': threading.Event(), 'result': None, 'error': None}
                self._inflight[key] = slot
        if not owner:
            slot['event'].wait()
            if slot['error'] is not None:
                raise slot['error']
            return slot['result']
        try:
            slot['result'] = fn()
            return slot['result']
        except BaseException as e:
            slot['error'] = e
            raise
        finally:
            with self._lock:
                self._inflight.pop(key, None)
            slot['event'].set()

    def account(self, api, fp):
        with self._lock:
            if fp in self._whoami:
                return self._whoami[fp]
        name = api.whoami()['name']
        with self._lock:
            self._whoami[fp] = name
            while len(self._whoami) > WHOAMI_CACHE_MAX:
                self._whoami.popitem(last=False)
        return name

    # ── per (repo, head) ──────────────────────────────────────────────────

    def tree(self, api, repo, head):
        return self._cached(repo, head, 'tree', lambda: HS.tree_view(api, repo, head))

    def files(self, api, repo, head):
        return self._cached(repo, head, 'files', lambda: HS.file_listing(api, repo, head))

    def _json_at(self, api, repo, head, path, token):
        """ONE bounded GET of a file at ``head`` (never hf_hub_download)."""
        from huggingface_hub import hf_hub_url
        from huggingface_hub.utils import build_hf_headers, get_session, hf_raise_for_status
        r = get_session().get(hf_hub_url(repo, path, repo_type='dataset', revision=head),
                              headers=build_hf_headers(token=token))
        hf_raise_for_status(r)
        return json.loads(r.content.decode('utf-8'))

    def info_json(self, api, repo, head, token):
        return self._cached(repo, head, 'info', lambda: self._json_at(api, repo, head, 'meta/info.json', token))

    def stats_json(self, api, repo, head, token):
        return self._cached(repo, head, 'stats', lambda: self._json_at(api, repo, head, 'meta/stats.json', token))

    def view(self, api, repo, head, record=None):
        """The hub view of §C3 at ``head``; the record's fast path: a synced
        dataset whose head equals its record needs no tree call."""
        if record and record.get('hub_sha') == head and record.get('hub_trees'):
            trees, last = record['hub_trees'], {}
        else:
            t = self.tree(api, repo, head)
            trees, last = t['trees'], t['last']
        return {'state': 'present', 'head': head, 'trees': trees, 'last': last,
                'files': lambda: self.files(api, repo, head)}

    def card(self, api, repo, head, token, private=None, last_modified=None, size_bytes=None):
        """An online card's fields from ``meta/info.json`` at ``head``; None when
        the dataset is not one this robot can open (v2.x, another robot, a
        non-default layout, no LeRobot info at all) — it is then hidden."""
        try:
            info = self.info_json(api, repo, head, token)
        except Exception:  # noqa: BLE001 — not a LeRobot dataset we can read: hidden
            return None, 'unreadable'
        refusal = refusal_of(info, self.robot_type)
        fps, frames = info.get('fps'), info.get('total_frames')
        out = {'id': repo, 'private': private, 'last_modified': last_modified, 'head': head,
               'total_episodes': info.get('total_episodes'), 'total_frames': frames,
               'duration_s': round(frames / fps, 3) if isinstance(frames, int) and fps else None,
               'fps': fps, 'robot_type': info.get('robot_type'), 'cameras': _cameras(info),
               'stat_names': None, 'size_bytes': size_bytes}
        if refusal is None:
            try:
                stats = self.stats_json(api, repo, head, token)
                out['stat_names'] = {f: sorted(v) for f, v in stats.items() if isinstance(v, dict)}
            except Exception:  # noqa: BLE001 — the card works without it
                out['stat_names'] = None
        return out, refusal

    # ── the library's hub part (§J.4.1) ───────────────────────────────────

    def library_hub(self, namespaces, local_entries, ids=None, hub_asked=True, records=None):
        """``(hub, views, listed)``: the hub part of the library reply, the hub
        view per local id (for ``library.sync_map``), and the listed online
        repos ({id: entry}). ``records``: {id: the full sync record} (the head
        fast path reads its ``hub_trees``). Never raises: a failure is
        ``hub.state``."""
        empty = {'state': 'skipped', 'token_fp': None, 'account': None, 'fetched_at': None,
                 'hidden_count': 0, 'complete_ns': [], 'entries': []}
        if not hub_asked:
            return empty, {}, {}
        token, fp = self.token()
        if not token:
            return dict(empty, state='no_token'), {}, {}
        key = (fp, tuple(namespaces or ()), tuple(ids) if ids is not None else None,
               tuple(sorted((e['id'], e.get('meta_digest')) for e in local_entries)))
        try:
            hub, views, listed = self._single_flight(
                key, lambda: self._library_hub(token, fp, namespaces, local_entries, ids, records or {}))
        except _HubError as e:
            return dict(empty, state=e.state, token_fp=fp), {}, {}
        _, fp_after = self.token()
        if fp_after != fp:
            return dict(empty, state='token_changed', token_fp=fp), {}, {}
        return hub, views, listed

    def _library_hub(self, token, fp, namespaces, local_entries, ids, records):
        api = self.api(token)
        try:
            account = self.account(api, fp)
        except Exception as e:  # noqa: BLE001
            raise _HubError('auth' if classify(e) == 'auth' else 'unreachable')
        local_ids = {e['id'] for e in local_entries}
        listed, views, entries, hidden, complete_ns = {}, {}, [], 0, []
        try:
            if ids is None:
                for ns in namespaces:
                    try:
                        found = list(api.list_datasets(author=ns))
                    except Exception as e:  # noqa: BLE001
                        if classify(e) == 'not_found':
                            found = []
                        else:
                            raise
                    if ns == account:
                        complete_ns.append(ns)
                    for d in found:
                        if getattr(d, 'sha', None):
                            listed[d.id] = {'id': d.id, 'head': d.sha, 'private': getattr(d, 'private', None),
                                            'last_modified': _iso(getattr(d, 'last_modified', None))}
                targets = [i for i in local_ids if i.split('/')[0] in namespaces]
            else:
                targets = list(ids)
                for repo in targets:
                    try:
                        info = api.dataset_info(repo)
                    except Exception as e:  # noqa: BLE001
                        if classify(e) == 'not_found':
                            continue
                        raise
                    if getattr(info, 'sha', None):
                        listed[repo] = {'id': repo, 'head': info.sha, 'private': getattr(info, 'private', None),
                                        'last_modified': _iso(getattr(info, 'last_modified', None))}
                    if repo.split('/')[0] == account and repo.split('/')[0] not in complete_ns:
                        complete_ns.append(repo.split('/')[0])
            for repo in targets:
                if repo in listed:
                    views[repo] = self.view(api, repo, listed[repo]['head'], self._record_for(records, repo))
                else:
                    views[repo] = {'state': 'absent', 'complete': repo.split('/')[0] == account}
            for repo, item in list(listed.items()):
                if repo in local_ids:
                    entries.append(dict(item, total_episodes=None, total_frames=None, duration_s=None, fps=None,
                                        robot_type=None, cameras=None, stat_names=None, size_bytes=None))
                    continue
                if ids is not None:
                    entries.append(item)
                    continue
                card, refusal = self.card(api, repo, item['head'], token, private=item['private'],
                                          last_modified=item['last_modified'],
                                          size_bytes=self._size(api, repo, item['head']))
                if card is None or refusal is not None:
                    hidden += 1
                    del listed[repo]
                    continue
                entries.append(card)
        except _HubError:
            raise
        except Exception as e:  # noqa: BLE001
            raise _HubError('auth' if classify(e) == 'auth' else 'unreachable')
        hub = {'state': 'ok', 'token_fp': fp, 'account': account, 'fetched_at': S.now_iso(),
               'hidden_count': hidden, 'complete_ns': complete_ns, 'entries': entries}
        return hub, views, listed

    @staticmethod
    def _record_for(records, repo):
        rec = records.get(repo) or {}
        return rec if rec.get('hub_sha') else None

    def _size(self, api, repo, head):
        try:
            return sum(int(e.get('size') or 0) for e in self.files(api, repo, head).values())
        except Exception:  # noqa: BLE001
            return None

    # ── probe (§J.4.2) ────────────────────────────────────────────────────

    def probe(self, repo):
        base = {'v': C.SCHEMA_VERSION, 'repo_id': repo, 'found': False, 'refusal': None}
        if not isinstance(repo, str) or not _REPO_ID.match(repo):
            return dict(base, refusal='invalid')
        token, _ = self.token()
        if not token:
            return dict(base, refusal='no_token')
        api = self.api(token)
        try:
            info = api.dataset_info(repo, files_metadata=True)
        except Exception as e:  # noqa: BLE001
            kind = classify(e)
            return dict(base, refusal='not_found' if kind == 'not_found' else 'unreachable')
        head = info.sha
        size = sum(int(getattr(f, 'size', 0) or 0) for f in (info.siblings or []))
        try:
            card, refusal = self.card(api, repo, head, token, private=bool(info.private),
                                      last_modified=_iso(getattr(info, 'last_modified', None)), size_bytes=size)
        except Exception:  # noqa: BLE001
            return dict(base, refusal='unreachable')
        if card is None:
            return dict(base, refusal='unsupported')
        out = dict(base, found=refusal is None, refusal=refusal, private=bool(info.private),
                   last_modified=card['last_modified'], sha=head, total_episodes=card['total_episodes'],
                   total_frames=card['total_frames'], duration_s=card['duration_s'], fps=card['fps'],
                   robot_type=card['robot_type'], cameras=card['cameras'], size_bytes=size)
        return out

    # ── hubstate (§J.4.4) ─────────────────────────────────────────────────

    def hubstate(self, library, dataset_id):
        path = library.path_of(dataset_id)
        rec = S.own_record(path, dataset_id)
        out = {'v': C.SCHEMA_VERSION, 'id': dataset_id, 'meta_digest': library.meta_digest(path),
               'sync': {'state': 'unknown', 'reason': 'not_asked'}, 'hub': None,
               'local': library.local_for_hubstate(dataset_id),
               'new_repo_private': bool(rec.get('private')) if rec else False}
        token, fp = self.token()
        if not token:
            return out
        api = self.api(token)
        view = None
        try:
            account = self.account(api, fp)
            try:
                info = api.dataset_info(dataset_id)
            except Exception as e:  # noqa: BLE001
                if classify(e) != 'not_found':
                    raise
                info = None
            if info is None:
                view = {'state': 'absent', 'complete': dataset_id.split('/')[0] == account}
            else:
                view = self.view(api, dataset_id, info.sha, rec if rec.get('hub_sha') else None)
                exists = any((view.get('trees') or {}).values())
                hub = {'exists': exists, 'private': bool(getattr(info, 'private', False)), 'head': info.sha,
                       'last_modified': _iso(getattr(info, 'last_modified', None)),
                       'total_episodes': None, 'duration_s': None}
                if exists:
                    try:
                        hinfo = self.info_json(api, dataset_id, info.sha, token)
                        fps, frames = hinfo.get('fps'), hinfo.get('total_frames')
                        hub['total_episodes'] = hinfo.get('total_episodes')
                        hub['duration_s'] = round(frames / fps, 3) if isinstance(frames, int) and fps else None
                    except Exception:  # noqa: BLE001 — the numbers are a courtesy
                        pass
                out['hub'] = hub
        except Exception:  # noqa: BLE001
            view = {'state': 'unreachable'}
        state, reason, _ = S.decide(path, rec, view)
        out['sync'] = {'state': state, 'reason': reason}
        return out


def refusal_of(info, robot_type) -> Optional[str]:
    """Why this robot cannot open a hub dataset: ``old_format`` (not v3),
    ``other_robot``, ``unsupported`` (a non-default layout, R-2) — or None."""
    if not isinstance(info, dict) or not str(info.get('codebase_version', '')).startswith('v3'):
        return 'old_format'
    if robot_type and info.get('robot_type') != robot_type:
        return 'other_robot'
    try:
        V.check_layout(info, [])
    except V.SurgeryError:
        return 'unsupported'
    return None


def _cameras(info):
    out = []
    for i, key in enumerate(V.video_keys_of(info or {})):
        vi = ((info.get('features') or {}).get(key) or {}).get('info') or {}
        out.append({'index': i, 'key': key, 'name': key.split('.')[-1], 'width': vi.get('video.width'),
                    'height': vi.get('video.height'), 'codec': vi.get('video.codec'),
                    'pix_fmt': vi.get('video.pix_fmt'), 'fps': vi.get('video.fps')})
    return out


def _iso(value):
    if value is None:
        return None
    if hasattr(value, 'strftime'):
        try:
            return value.strftime('%Y-%m-%dT%H:%M:%SZ')
        except Exception:  # noqa: BLE001
            return str(value)
    return str(value)
