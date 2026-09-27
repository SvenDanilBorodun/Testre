#!/usr/bin/env python3
#
# Copyright 2026 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""The Roboter-API table — one row per handler-table key, one source for
three surfaces.

``ROBOT_API`` is a tuple of :class:`ApiCall` rows, one for every key of
``handlers.STATEMENT_HANDLERS`` | ``handlers.VALUE_EVALUATORS`` (a bijection,
fenced by ``test/test_robot_api.py``). Each row names the Python method a
text program calls (``robot.move_to``), its Java spelling (``Robot.moveTo``),
the block type it dispatches to, its positional parameters with the KIND and
RANGE the server validates, what it returns, and a German docstring.

Three things are rendered from it and checked in (byte-equality fenced by
``test/test_robot_api_generated.py``):

- the Python stub the student process imports
  (``robotis_ai_setup/docker/code_runner/runner/lib/robot.py``),
- the Java client (``Robot.java`` + ``Greifobjekt.java`` + ``RpcClient.java``
  under ``runner/java/edubotics/``),
- the editor's ``robot_api.json`` (autocomplete, hover docs, the caps).

``INTERNAL_METHODS`` are the calls the stubs make that dispatch to NO handler
(the ``__`` protocol methods and ``register_object``); they sit in the same
shape so the validator reads one table and the ``Greifobjekt`` renderers read
the same ``register_object`` row the server validates against — one wire
shape, one source.

``CODE_ONLY_METHODS`` are PUBLIC calls a text program makes that have no block
and dispatch to no handler (``zeige``: show a value in the Variablen panel).
They render into both stubs and the JSON like a table row, sit OUTSIDE the
handler bijection, and never reach the arm: ``code_rpc`` answers them itself,
through ``ctx.log`` only.

``ApiParam.asset`` tags a parameter whose string argument names a Sammlung
asset (``recording`` / ``place`` / ``place_def`` / ``counter`` / ``object`` /
``variable``). Only the JSON renders it: the editor's scanner, completion,
lint and hover read the tag, never a hand-kept list of method names; the two
stubs are unaffected by it.

Seven Blockly types deliberately have no row: ``edubotics_forever`` /
``edubotics_wait_until`` / ``edubotics_while_visible`` are language constructs
in a text program (``while True``, ``while not …``, ``while robot.sees(…)``),
and the three hats plus ``edubotics_broadcast`` have no meaning without hats —
a text program uses functions.

Every ``arg_key`` is a key the handler actually reads (fenced by an AST scan
of each handler body, helpers included). The three point-takers share the
student-facing name ``target`` but ``move_to``/``drop_at`` read
``args['destination']`` while ``pickup`` reads ``args['target']`` — the rows
say so, the test proves it.

The rate budget both ends enforce lives here as :data:`RPC_LIMITS` (the
generated stubs embed the numbers; ``code_rpc.py`` imports them). The project
caps of the code editor live beside it so the JSON carries both.

Nothing here reads the environment; there is no ``EDU…_`` knob in this module
by design (the guard that scans the server package for such names would
demand a compose forward for it).
"""

from __future__ import annotations

import dataclasses
import difflib
import json
from dataclasses import dataclass
from typing import Any


# ══════════════════════════════════════════════════════════════════════════
# Limits — one policy, two enforcement points (runner floor + server bucket)
# ══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class RpcLimits:
    """The call-rate budget and the two frame bounds (§3.2).

    ``MAX_FRAME_BYTES`` bounds every DATA frame (the socket the student uid
    connects to — the hostile surface). ``CONTROL_MAX_FRAME_BYTES`` bounds
    the supervisor's control socket, which is ``0600`` and carries the whole
    project in its ``start`` envelope, so it must fit
    ``MAX_CODE_PROJECT_BYTES`` plus the envelope."""

    MAX_CALLS_PER_S: float = 200.0
    BURST: int = 50
    PERCEPTION_MAX_PER_S: float = 20.0
    PERCEPTION_BURST: int = 5
    MAX_FRAME_BYTES: int = 65536
    CONTROL_MAX_FRAME_BYTES: int = 196608


RPC_LIMITS = RpcLimits()

# The code-project caps (§3.7). Measured on the same
# ``json.dumps(files, ensure_ascii=False)`` encoding the control frame uses,
# so a project's bytes on the wire ARE the bytes the cap counted.
MAX_CODE_FILES = 32
MAX_CODE_FILE_BYTES = 65536
MAX_CODE_PROJECT_BYTES = 131072
# ≤ 80 chars, at most three directories, no `..`, no leading `/`, stems start
# with a letter, `.py` or `.java`.
CODE_PATH_RE = (
    r'^(?:[A-Za-z][A-Za-z0-9_]{0,39}/){0,3}[A-Za-z][A-Za-z0-9_]{0,39}\.(py|java)$'
)

CODE_PROJECT_LIMITS: dict[str, Any] = {
    'MAX_CODE_FILES': MAX_CODE_FILES,
    'MAX_CODE_FILE_BYTES': MAX_CODE_FILE_BYTES,
    'MAX_CODE_PROJECT_BYTES': MAX_CODE_PROJECT_BYTES,
    'CODE_PATH_RE': CODE_PATH_RE,
}


# ══════════════════════════════════════════════════════════════════════════
# The row shape
# ══════════════════════════════════════════════════════════════════════════

# The server validates every argument by KIND (see code_rpc.validate_value):
#   float   finite number, lo ≤ v ≤ hi (lo_open → lo < v); bool refused
#   int     integer, lo ≤ v ≤ hi; bool refused
#   str     text ≤ max_len, no control characters, matching `pattern`
#   text    a JSON scalar shown to the student (str/int/float/bool); a str
#           obeys max_len — what „melde"/„sage"/„zeige" stringify anyway
#   bool    true / false
#   point   [x, y, z], three finite floats each within ±1.0 m
#   target  a `point` OR the name of a pinned destination (`str` rules with
#           the destination alphabet) — what motion._resolve_target accepts
#   ziel    a Greifziel handle (int ≥ 0) the run owns
#   tagids  1..16 ints each in 0..586 (tag36h11's id space)
#   obj     a catalog type name / Greifobjekt name (`_TYPE_NAME_RE`, ≤ 24)
#   dict    a JSON object with ≤ max_len string keys (protocol payloads)
#   value   any JSON value a student shows („zeige"): nesting and node count
#           bounded (SHOWN_VALUE_MAX_DEPTH / _NODES in code_rpc), the frame
#           bound caps the bytes; rendered like a Blockly [VAR:] value
KINDS = frozenset({
    'float', 'int', 'str', 'text', 'bool', 'point', 'target', 'ziel',
    'tagids', 'obj', 'dict', 'value',
})

# What a string argument names, for the editor (see the module docstring).
ASSET_KINDS = frozenset({
    'recording', 'place', 'place_def', 'counter', 'object', 'variable',
})

# What a row hands back over the wire:
#   none    statement, `r` is null
#   bool / int / point ([x, y, z]) / name (a destination name string)
#   ziel    {"h": <handle>} or null — the stub wraps it as a Greifziel
RETURN_KINDS = frozenset({'none', 'bool', 'int', 'point', 'name', 'ziel'})


class _Required:
    """Sentinel: the parameter has no default (rendered as required)."""

    def __repr__(self) -> str:
        return 'REQUIRED'


REQUIRED = _Required()

# Name alphabets shared with the handlers that enforce them (the same regex
# destinations.py / physical_ai_server.py / the cloud validator use).
DESTINATION_NAME_RE = r'^[A-Za-zÄÖÜäöüß0-9 _\-]{1,40}$'
TRAJECTORY_NAME_RE = r'^[A-Za-zÄÖÜäöüß0-9 _\-]{1,40}$'
# A counter name rides the `[CNT:name=int]` sentinel: no `=`, `[`, `]`.
COUNTER_NAME_RE = r'^[^=\[\]\x00-\x1f]{1,40}$'
TYPE_NAME_RE = r'^[A-Za-z0-9_]{1,24}$'
# A `zeige` name rides the `[VAR:name=json]` sentinel exactly like a counter
# name: no `=`, `[`, `]`, no control character, and — since the React name
# gate (utils/variableName.js) drops a whitespace-only name silently — not
# only whitespace.
SHOWN_NAME_RE = r'^(?=.*\S)[^=\[\]\x00-\x1f\x7f]{1,40}$'
# The rendered [VAR:] payload is cut here, the interpreter's
# _MAX_VAR_PAYLOAD_CHARS (a test pins the two equal; the stubs size their own
# rendering against it).
SHOWN_VALUE_MAX_CHARS = 2000
TOAST_LEVEL_RE = r'^(info|success|warning|error)$'
RUN_TOKEN_RE = r'^[0-9a-f]{32}$'
# Exit-info / paused payloads carry a bounded set of keys.
PAUSED_MAX_LOCALS = 30
EXIT_INFO_MAX_KEYS = 8

# ── Shown values: what the runner renders AND what the robot accepts ──────
# One place for both halves (2026-09-27 review round 2, mi4), so the runner's
# worst case provably fits the server's caps: a mismatch between the two once
# made the server drop a whole `__vars` frame the runner believed delivered,
# and showed none of a breakpoint's 20 variables.
#
# A LIVE value (`__vars`, rendered by the runner's edubotics_debug.safe_render
# without running student code) holds at most LIVE_VALUE_MAX_NODES JSON nodes
# (the `'…'` that ends a cut container included) and LIVE_VALUE_MAX_CHARS
# characters of strings and dict keys, counted exactly as the server counts
# them (code_rpc.shown_values_exceed).
LIVE_VALUE_MAX_NODES = 100
LIVE_VALUE_MAX_CHARS = 1000
# A BREAKPOINT value (`__paused`, rendered on the stopped student thread) is
# JSON text of at most PAUSED_VALUE_MAX_CHARS, else its repr cut to that many
# characters — so it holds at most PAUSED_VALUE_MAX_CHARS // 2 + 1 nodes (a
# node takes at least two characters of JSON: a digit and a separator).
PAUSED_VALUE_MAX_CHARS = 1000
# The server's caps on the values of ONE frame, derived from the two above: a
# frame of PAUSED_MAX_LOCALS values the runner rendered never exceeds them,
# so only a hand-made frame is ever trimmed (code_rpc.fit_shown_values).
SHOWN_FRAME_MAX_NODES = PAUSED_MAX_LOCALS * max(LIVE_VALUE_MAX_NODES,
                                                PAUSED_VALUE_MAX_CHARS // 2 + 1)
SHOWN_FRAME_MAX_CHARS = PAUSED_MAX_LOCALS * max(LIVE_VALUE_MAX_CHARS,
                                                PAUSED_VALUE_MAX_CHARS)
# What stands in for a value there is no room for — on the runner and on the
# server alike, so the name still shows and says why its value does not.
SHOWN_TOO_BIG = '<zu groß>'
# The live values' pace (owner decision R2-O1): the runner sends at most one
# `__vars` per LIVE_VALUES_INTERVAL_S, measured from the previous REPLY; the
# server looks at one per VARS_MIN_INTERVAL_S and answers any other with
# VARS_REPLY_SKIPPED (charged, unrendered). The runner's pace is the slower,
# so an honest runner is never skipped — and when it is, it learns so and
# sends the values again at its next chance.
LIVE_VALUES_INTERVAL_S = 0.5
VARS_MIN_INTERVAL_S = 0.4
VARS_REPLY_SKIPPED = 'skipped'
# `zeige`: the latest value of at most this many names waits to be shown (the
# emit budget is per run); a NEW name past it is dropped, with one German
# warning per run (code_rpc.ZEIGE_TOO_MANY_NAMES_DE).
SHOWN_VAR_NAMES_MAX = 256


@dataclass(frozen=True)
class ApiParam:
    name: str
    kind: str
    arg_key: str
    lo: float | None = None
    hi: float | None = None
    max_len: int | None = None
    pattern: str | None = None
    nullable: bool = False
    lo_open: bool = False
    default: Any = REQUIRED
    asset: str | None = None

    @property
    def required(self) -> bool:
        return self.default is REQUIRED


@dataclass(frozen=True)
class ApiCall:
    name: str
    java_name: str
    block_type: str | None
    table: str                       # 'statement' | 'value' | 'internal' | 'code'
    params: tuple[ApiParam, ...]
    returns: str
    doc_de: str
    budget: str = 'call'             # 'call' | 'perception' (B2)


def _p(name: str, kind: str, arg_key: str | None = None, **kw: Any) -> ApiParam:
    return ApiParam(name, kind, arg_key if arg_key is not None else name, **kw)


def _stmt(name: str, java_name: str, block_type: str, params: tuple[ApiParam, ...],
          doc_de: str, budget: str = 'call') -> ApiCall:
    return ApiCall(name, java_name, block_type, 'statement', params, 'none', doc_de, budget)


def _value(name: str, java_name: str, block_type: str, params: tuple[ApiParam, ...],
           returns: str, doc_de: str, budget: str = 'call') -> ApiCall:
    return ApiCall(name, java_name, block_type, 'value', params, returns, doc_de, budget)


def _internal(name: str, params: tuple[ApiParam, ...], doc_de: str,
              budget: str = 'call') -> ApiCall:
    return ApiCall(name, name, None, 'internal', params, 'none', doc_de, budget)


def _code_only(name: str, java_name: str, params: tuple[ApiParam, ...],
               doc_de: str) -> ApiCall:
    return ApiCall(name, java_name, None, 'code', params, 'none', doc_de, 'call')


_ZIEL = _p('ziel', 'ziel')
_OBJ = _p('obj', 'obj', 'object_type', max_len=24, pattern=TYPE_NAME_RE,
          asset='object')
_TIMEOUT = _p('timeout', 'float', lo=0.0, hi=300.0, default=10.0)
_COUNTER = _p('name', 'str', max_len=40, pattern=COUNTER_NAME_RE, asset='counter')
_DEST_NAME = _p('name', 'str', max_len=40, pattern=DESTINATION_NAME_RE)
_DEST_DEF = _p('name', 'str', max_len=40, pattern=DESTINATION_NAME_RE, asset='place_def')
_DEST_REF = _p('name', 'str', max_len=40, pattern=DESTINATION_NAME_RE, asset='place')


# ══════════════════════════════════════════════════════════════════════════
# The table
# ══════════════════════════════════════════════════════════════════════════

ROBOT_API: tuple[ApiCall, ...] = (
    # ── Motion ────────────────────────────────────────────────────────────
    _stmt('home', 'home', 'edubotics_home', (),
          'Fährt den Arm in die Grundstellung; der Greifer bleibt, wie er ist.'),
    _stmt('open_gripper', 'openGripper', 'edubotics_open_gripper', (),
          'Öffnet den Greifer.'),
    _stmt('close_gripper', 'closeGripper', 'edubotics_close_gripper', (),
          'Schließt den Greifer.'),
    _stmt('move_to', 'moveTo', 'edubotics_move_to',
          (_p('target', 'target', 'destination', asset='place'),),
          'Fährt über das Ziel: ein Punkt [x, y, z] in Metern oder der Name '
          'eines gemerkten Ziels.'),
    _stmt('pickup', 'pickup', 'edubotics_pickup',
          (_p('target', 'target', 'target', asset='place'),),
          'Nimmt an dieser Stelle etwas auf: hinfahren, absenken, Greifer '
          'schließen, anheben.'),
    _stmt('drop_at', 'dropAt', 'edubotics_drop_at',
          (_p('target', 'target', 'destination', asset='place'),),
          'Legt das Gehaltene über dem Ziel ab und öffnet den Greifer.'),
    _stmt('wait', 'waitSeconds', 'edubotics_wait_seconds',
          (_p('seconds', 'float', lo=0.0, hi=300.0),),
          'Wartet so viele Sekunden (höchstens 300).'),
    _stmt('replay', 'replay', 'edubotics_replay_trajectory',
          (_p('name', 'str', max_len=40, pattern=TRAJECTORY_NAME_RE, asset='recording'),
           _p('speed', 'float', lo=0.25, hi=3.0, default=1.0)),
          'Spielt eine aufgenommene Bewegung ab; speed 1.0 ist die Geschwindigkeit '
          'der Aufnahme, höchstens 3.0.'),
    _stmt('move_above', 'moveAbove', 'edubotics_move_above', (_ZIEL,),
          'Fährt in Anfahrhöhe über das gefundene Objekt.'),
    _stmt('descend_to', 'descendTo', 'edubotics_descend_to', (_ZIEL,),
          'Senkt den geöffneten Greifer auf das gefundene Objekt ab.'),
    _stmt('close_on_object', 'closeOnObject', 'edubotics_close_on_object', (_ZIEL,),
          'Schließt den Greifer um das gefundene Objekt.'),
    _stmt('lift', 'lift', 'edubotics_lift', (),
          'Hebt den Greifer gerade nach oben an, so weit der Arm kann.'),
    # ── Named-object grasp ────────────────────────────────────────────────
    _stmt('grasp', 'grasp', 'edubotics_grasp_object', (_OBJ,),
          'Greift das nächste sichtbare Objekt dieses Typs: finden, anfahren, '
          'schließen, prüfen.'),
    _stmt('mark_done', 'markDone', 'edubotics_mark_done', (_ZIEL,),
          'Merkt dieses Objekt als erledigt, damit es nicht noch einmal '
          'gefunden wird.'),
    # ── Destinations ──────────────────────────────────────────────────────
    _stmt('pin', 'pin', 'edubotics_destination_pin',
          (_DEST_DEF,
           _p('x', 'float', lo=-1.0, hi=1.0),
           _p('y', 'float', lo=-1.0, hi=1.0),
           _p('z', 'float', lo=-1.0, hi=1.0)),
          'Merkt den Punkt [x, y, z] in Metern unter diesem Namen als Ziel '
          'für move_to und drop_at.'),
    _stmt('pin_current', 'pinCurrent', 'edubotics_destination_current',
          (_DEST_DEF,),
          'Merkt die Stelle, an der der Greifer gerade steht, unter diesem '
          'Namen als Ziel.'),
    # ── Output ────────────────────────────────────────────────────────────
    _stmt('log', 'log', 'edubotics_log',
          (_p('message', 'text', max_len=2000),),
          'Schreibt eine Zeile ins Protokoll — Zahlen und Texte, höchstens '
          '2000 Zeichen.'),
    _stmt('beep', 'beep', 'edubotics_play_sound', (),
          'Spielt einen kurzen Signalton ab — hörbar im Browser.'),
    _stmt('speak', 'speak', 'edubotics_speak_de',
          (_p('text', 'text', max_len=240),),
          'Liest den Text mit deutscher Stimme vor (höchstens 240 Zeichen).'),
    _stmt('tone', 'tone', 'edubotics_play_tone',
          (_p('freq', 'float', lo=100.0, hi=4000.0, default=880.0),
           _p('seconds', 'float', lo=0.05, hi=5.0, default=0.25)),
          'Spielt einen Ton mit dieser Frequenz in Hertz für so viele Sekunden '
          '(höchstens 5).'),
    _stmt('toast', 'toast', 'edubotics_toast',
          (_p('text', 'text', max_len=240),
           _p('level', 'str', max_len=7, pattern=TOAST_LEVEL_RE, default='info'),
           _p('seconds', 'int', lo=1, hi=15, default=3)),
          'Zeigt eine Meldung auf dem Bildschirm; level ist info, success, '
          'warning oder error, seconds höchstens 15.'),
    # ── Counters ──────────────────────────────────────────────────────────
    _stmt('counter_reset', 'counterReset', 'edubotics_counter_reset', (_COUNTER,),
          'Setzt den Zähler mit diesem Namen auf 0.'),
    _stmt('counter_add', 'counterAdd', 'edubotics_counter_add', (_COUNTER,),
          'Erhöht den Zähler mit diesem Namen um 1.'),
    # ── Values ────────────────────────────────────────────────────────────
    _value('ziel', 'ziel', 'edubotics_destination_ref', (_DEST_REF,), 'name',
           'Gibt den Namen eines gemerkten Ziels für move_to und drop_at zurück; '
           'ein unbekannter Name wird abgelehnt.'),
    _value('sees', 'sees', 'edubotics_see_object', (_OBJ,), 'bool',
           'Prüft, ob gerade ein noch nicht erledigtes Objekt dieses Typs zu '
           'sehen ist.', budget='perception'),
    _value('count', 'count', 'edubotics_count_object', (_OBJ,), 'int',
           'Zählt die sichtbaren, noch nicht erledigten Objekte dieses Typs.',
           budget='perception'),
    _value('wait_until_seen', 'waitUntilSeen', 'edubotics_wait_until_object_seen',
           (_OBJ, _TIMEOUT), 'bool',
           'Wartet, bis ein Objekt dieses Typs zu sehen ist; False, wenn es nach '
           'timeout Sekunden nicht da ist.', budget='perception'),
    _value('wait_until_held', 'waitUntilHeld', 'edubotics_wait_until_held',
           (_TIMEOUT,), 'bool',
           'Wartet, bis der Greifer etwas hält; False, wenn er nach timeout '
           'Sekunden noch leer ist.'),
    _value('find', 'find', 'edubotics_find_object', (_OBJ,), 'ziel',
           'Findet das nächste greifbare Objekt dieses Typs und gibt ein '
           'Greifziel zurück, sonst None.', budget='perception'),
    _value('object_position', 'objectPosition', 'edubotics_object_position',
           (_ZIEL,), 'point',
           'Gibt die Position [x, y, z] des Greifziels in Metern zurück.',
           budget='perception'),
    _value('is_holding', 'isHolding', 'edubotics_grasp_held', (), 'bool',
           'Prüft, ob der Greifer etwas hält.'),
    _value('counter_get', 'counterGet', 'edubotics_counter_get', (_COUNTER,), 'int',
           'Gibt den Wert des Zählers zurück; 0, wenn er noch nie gesetzt wurde.'),
)

_FILE = _p('file', 'str', max_len=80, pattern=CODE_PATH_RE)
_LINE = _p('line', 'int', lo=0, hi=1_000_000)

INTERNAL_METHODS: tuple[ApiCall, ...] = (
    _internal('__hello', (_p('token', 'str', max_len=32, pattern=RUN_TOKEN_RE),),
              'Meldet das Programm mit seinem Lauf-Schlüssel an — das macht die '
              'Bibliothek selbst.'),
    _internal('__paused', (_FILE, _LINE, _p('locals', 'dict', max_len=PAUSED_MAX_LOCALS)),
              'Meldet einen erreichten Haltepunkt und wartet, bis der Lauf '
              'weiterläuft — das macht die Bibliothek selbst.'),
    _internal('__line', (_FILE, _LINE),
              'Meldet die Zeile, die gerade läuft, für die Anzeige — das macht '
              'die Bibliothek selbst.'),
    _internal('__vars', (_FILE, _LINE, _p('locals', 'dict', max_len=PAUSED_MAX_LOCALS)),
              'Meldet die aktuellen Werte der Variablen für den Variablen-Bereich — '
              'das macht die Bibliothek selbst.'),
    _internal('__exit', (_p('info', 'dict', max_len=EXIT_INFO_MAX_KEYS),),
              'Meldet, dass das Programm zu Ende ist oder abgebrochen wurde — '
              'das macht die Bibliothek selbst.'),
    _internal('register_object',
              (_p('name', 'obj', max_len=24, pattern=TYPE_NAME_RE),
               _p('label', 'str', max_len=40),
               _p('tag_ids', 'tagids'),
               _p('hoehe_m', 'float', lo=0.0, hi=0.30, lo_open=True),
               _p('greiftiefe_m', 'float', lo=0.0, hi=0.30),
               _p('greifer_schliessen_rad', 'float', lo=-1.75, hi=1.75, nullable=True),
               _p('anfahrhoehe_m', 'float', lo=0.0, hi=0.30, lo_open=True, nullable=True)),
              'Meldet einen eigenen Objekt-Typ für diesen Lauf an — das macht '
              'Greifobjekt selbst.', budget='perception'),
)

# Public calls with no block and no handler (see the module docstring). The
# server answers them itself (code_rpc.RunSession._code_only), never the arm.
CODE_ONLY_METHODS: tuple[ApiCall, ...] = (
    _code_only('zeige', 'zeige',
               (_p('name', 'str', max_len=40, pattern=SHOWN_NAME_RE, asset='variable'),
                _p('wert', 'value')),
               'Zeigt einen Wert unter diesem Namen im Variablen-Bereich an — '
               'Zahlen, Texte, Listen; höchstens 40 Zeichen Name.'),
)

ROBOT_API_BY_NAME: dict[str, ApiCall] = {c.name: c for c in ROBOT_API}
ROBOT_API_BY_BLOCK_TYPE: dict[str, ApiCall] = {c.block_type: c for c in ROBOT_API}
INTERNAL_METHODS_BY_NAME: dict[str, ApiCall] = {c.name: c for c in INTERNAL_METHODS}
CODE_ONLY_METHODS_BY_NAME: dict[str, ApiCall] = {c.name: c for c in CODE_ONLY_METHODS}
# The public Python surface: the handler rows plus the code-only rows (what a
# misspelling is compared against).
PYTHON_NAMES: tuple[str, ...] = tuple(c.name for c in ROBOT_API + CODE_ONLY_METHODS)


def suggest(name: str) -> str | None:
    """The closest API name to a misspelling, or ``None`` (difflib, one hit)."""
    if not isinstance(name, str) or not name:
        return None
    hits = difflib.get_close_matches(name, PYTHON_NAMES, n=1, cutoff=0.6)
    return hits[0] if hits else None


# ══════════════════════════════════════════════════════════════════════════
# Rendering — the checked-in artifacts
# ══════════════════════════════════════════════════════════════════════════

# Repo-relative paths of the five generated files (test_robot_api_generated
# reads this map, regenerates through it and diffs against it).
GENERATED_PATHS = {
    'python_stub': 'robotis_ai_setup/docker/code_runner/runner/lib/robot.py',
    'java_robot': 'robotis_ai_setup/docker/code_runner/runner/java/edubotics/Robot.java',
    'java_greifobjekt': 'robotis_ai_setup/docker/code_runner/runner/java/edubotics/Greifobjekt.java',
    'java_rpc_client': 'robotis_ai_setup/docker/code_runner/runner/java/edubotics/RpcClient.java',
    'json': 'physical_ai_tools/physical_ai_manager/src/components/Workshop/code/robot_api.json',
}

_GENERATED_NOTICE = (
    'GENERATED FILE — do not edit by hand. Rendered from the table in\n'
    'physical_ai_server/workflow/robot_api.py; test_robot_api_generated.py\n'
    'refuses a drift and documents how to regenerate.'
)


def _py_literal(value: Any) -> str:
    return repr(value)


def _java_literal(value: Any) -> str:
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(float(value))
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if value is None:
        return 'null'
    raise TypeError(f'no Java literal for {value!r}')


def _comment_safe(text: str) -> str:
    """A doc line inside a Javadoc / docstring must not end the comment."""
    return text.replace('*/', '* /').replace('"""', '\'\'\'')


# ── Python stub ───────────────────────────────────────────────────────────

# The zeige() rendering budget, in characters of text spent across the whole
# value (two SHOWN_VALUE_MAX_CHARS: the robot cuts at one, so the student sees
# the cut the robot makes, not the stub's). Every string/key/scalar costs at
# least 4, so the rendered JSON is O(budget), far below MAX_FRAME_BYTES.
_SHOWN_BUDGET_CHARS = 2 * SHOWN_VALUE_MAX_CHARS
# How deep and how wide zeige() walks a value, in both stubs: a list/tuple/
# set/dict (Python) or an array/Iterable (Java) at most this many levels and
# items; past either, the rendering says so with '…'.
_SHOWN_MAX_DEPTH = 3
_SHOWN_MAX_ITEMS = 50

_PY_ARG_MARSHAL = {
    'ziel': '_handle({v})',
    'obj': '_type_name({v})',
    'tagids': 'list({v})',
    'value': '_shown({v})',
}

_PY_RESULT_UNWRAP = {
    'ziel': '_greifziel(r)',
    'point': '_point(r)',
}


def _py_signature(call: ApiCall) -> str:
    parts = []
    for p in call.params:
        parts.append(p.name if p.required else f'{p.name}={_py_literal(p.default)}')
    return ', '.join(parts)


def _py_method(call: ApiCall) -> str:
    args = ', '.join(
        _PY_ARG_MARSHAL.get(p.kind, '{v}').format(v=p.name) for p in call.params)
    kind = _py_literal(call.budget)
    lines = [f'def {call.name}({_py_signature(call)}):']
    lines.append(f'    """{_comment_safe(call.doc_de)}"""')
    call_expr = f"_rpc.call({_py_literal(call.name)}, [{args}], {kind})"
    if call.returns == 'none':
        lines.append(f'    {call_expr}')
    elif call.returns in _PY_RESULT_UNWRAP:
        lines.append(f'    r = {call_expr}')
        lines.append(f'    return {_PY_RESULT_UNWRAP[call.returns]}')
    else:
        lines.append(f'    return {call_expr}')
    return '\n'.join(lines) + '\n'


_PY_STUB_HEAD = '''"""Roboter-API für EduBotics-Programme (Python).

    import robot
    robot.home()
    robot.move_to([0.15, 0.0, 0.05])
    ziel = robot.find("wuerfel")
    if ziel:
        robot.move_above(ziel)

Jede Funktion schickt einen Aufruf an den Roboter und wartet auf die Antwort.
Lehnt der Roboter etwas ab, gibt es einen RobotError mit deutscher Meldung.

{notice}
"""

from __future__ import annotations

import itertools
import json
import os
import socket
import struct
import sys
import threading
import time

# The call budget the robot enforces. This floor is a courtesy — the server
# sleeps on the same numbers, and that is the boundary, never this.
MAX_CALLS_PER_S = {MAX_CALLS_PER_S!r}
BURST = {BURST!r}
PERCEPTION_MAX_PER_S = {PERCEPTION_MAX_PER_S!r}
PERCEPTION_BURST = {PERCEPTION_BURST!r}
MAX_FRAME_BYTES = {MAX_FRAME_BYTES!r}

_SOCKET_ENV = 'CODE_RPC_SOCKET'
_TOKEN_ENV = 'CODE_RUN_TOKEN'

# The public calls — the functions below. Right before each one the
# launcher's live-values hook may report the program's variables (_Rpc.
# before_call); the library's own `__` calls never trigger it.
_PUBLIC_METHODS = frozenset({{
{PUBLIC_METHODS}
}})

_NO_CONNECTION_DE = ('Keine Verbindung zum Roboter — das Programm muss über '
                     'Roboter Studio gestartet werden.')
_CONNECTION_LOST_DE = 'Die Verbindung zum Roboter ist abgebrochen.'
_FRAME_TOO_BIG_DE = 'Der Aufruf ist zu groß für den Roboter.'
_BAD_REPLY_DE = 'Der Roboter hat unverständlich geantwortet.'
_NOT_A_GREIFZIEL_DE = 'Hier wird ein Greifziel von robot.find erwartet.'
_NOT_AN_OBJECT_DE = ('Hier wird ein Objekt-Name (z. B. "wuerfel") oder eine '
                     'Greifobjekt-Klasse erwartet.')


class RobotError(Exception):
    """Der Roboter hat einen Aufruf abgelehnt; str(e) ist die deutsche Meldung."""


class Greifziel:
    """Ein gefundenes Objekt (von robot.find) — ein Handle für die Greif-Schritte."""

    __slots__ = ('_h',)

    def __init__(self, handle):
        self._h = int(handle)

    def __repr__(self):
        return f'Greifziel({{self._h}})'

    def __eq__(self, other):
        return isinstance(other, Greifziel) and other._h == self._h

    def __hash__(self):
        return hash(('Greifziel', self._h))


class _Bucket:
    """A token bucket that SLEEPS until a token is available (never drops)."""

    def __init__(self, rate, burst):
        self._rate = float(rate)
        self._burst = float(burst)
        self._tokens = float(burst)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def take(self):
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self._burst,
                                   self._tokens + (now - self._last) * self._rate)
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self._rate
            time.sleep(wait)


class _Rpc:
    """The one client: framing, token, the rate floor, the reply unwrapping.

    Every socket byte of this module is written or read here, and every
    write is preceded by the rate floor — a structural property the server
    package's tests assert over this file.

    ``before_call`` is the launcher's live-values hook (``None`` outside
    Roboter Studio): called on the calling thread right before every public
    call, it may send one ``__vars`` frame of its own. Whatever it does, the
    call itself goes ahead unchanged: an exception out of it switches the hook
    off for the rest of the run and never reaches the program."""

    def __init__(self):
        self._sock = None
        self._lock = threading.RLock()
        self._next_id = 0
        self._calls = _Bucket(MAX_CALLS_PER_S, BURST)
        self._perception = _Bucket(PERCEPTION_MAX_PER_S, PERCEPTION_BURST)
        self.project_root = None
        self.before_call = None

    def connect(self, path, token, project_root=None):
        """Connect and greet; the launcher calls this, or the first call does
        from the two environment variables."""
        with self._lock:
            if self._sock is not None:
                return
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.connect(path)
            except OSError:
                sock.close()
                raise RobotError(_NO_CONNECTION_DE) from None
            self._sock = sock
            self.project_root = project_root
            self.call('__hello', [token], 'call')

    def _ensure_connected(self):
        if self._sock is not None:
            return
        path = os.environ.get(_SOCKET_ENV)
        token = os.environ.get(_TOKEN_ENV)
        if not path or not token:
            raise RobotError(_NO_CONNECTION_DE)
        self.connect(path, token)

    def _rate_floor(self, kind):
        self._calls.take()
        if kind == 'perception':
            self._perception.take()

    def _position(self):
        """(file, line) of the student's own frame, for the status display."""
        try:
            frame = sys._getframe(3)
        except ValueError:
            return None, None
        path = frame.f_code.co_filename
        if type(path) is not str:
            # A code object compiled with a str SUBCLASS as its file name
            # would run that subclass's own methods here.
            return None, None
        if self.project_root and path.startswith(self.project_root):
            path = os.path.relpath(path, self.project_root)
        else:
            path = os.path.basename(path)
        return path, int(frame.f_lineno)

    def call(self, method, args, kind):
        hook = self.before_call
        if hook is not None and method in _PUBLIC_METHODS:
            try:
                hook()
            except Exception:  # noqa: BLE001 — the live values never break a call
                self.before_call = None
        with self._lock:
            self._ensure_connected()
            self._rate_floor(kind)
            self._next_id += 1
            request = {{'id': self._next_id, 'm': method, 'a': list(args)}}
            file, line = self._position()
            if file is not None:
                request['f'] = file
                request['l'] = line
            data = json.dumps(request, ensure_ascii=False,
                              separators=(',', ':')).encode('utf-8')
            if len(data) > MAX_FRAME_BYTES:
                raise RobotError(_FRAME_TOO_BIG_DE)
            try:
                self._sock.sendall(struct.pack('>I', len(data)) + data)
                reply = self._read_reply()
            except OSError:
                self.close()
                raise RobotError(_CONNECTION_LOST_DE) from None
        if reply.get('ok') is True:
            return reply.get('r')
        raise RobotError(str(reply.get('e') or _BAD_REPLY_DE))

    def _recv_exact(self, n):
        buf = bytearray(n)
        view = memoryview(buf)
        got = 0
        while got < n:
            k = self._sock.recv_into(view[got:], n - got)
            if k == 0:
                raise RobotError(_CONNECTION_LOST_DE)
            got += k
        return bytes(buf)

    def _read_reply(self):
        (length,) = struct.unpack('>I', self._recv_exact(4))
        if length > MAX_FRAME_BYTES:
            raise RobotError(_BAD_REPLY_DE)
        try:
            reply = json.loads(self._recv_exact(length).decode('utf-8'))
        except ValueError:
            raise RobotError(_BAD_REPLY_DE) from None
        if not isinstance(reply, dict):
            raise RobotError(_BAD_REPLY_DE)
        return reply

    def close(self):
        with self._lock:
            sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


_rpc = _Rpc()


def _handle(value):
    if isinstance(value, Greifziel):
        return value._h
    raise RobotError(_NOT_A_GREIFZIEL_DE)


def _type_name(value):
    if isinstance(value, str):
        return value
    if isinstance(value, type) and issubclass(value, Greifobjekt):
        return value.name or value.__name__.lower()
    raise RobotError(_NOT_AN_OBJECT_DE)


def _greifziel(r):
    if isinstance(r, dict) and 'h' in r:
        return Greifziel(r['h'])
    return None


def _point(r):
    if isinstance(r, (list, tuple)) and len(r) == 3:
        return (float(r[0]), float(r[1]), float(r[2]))
    return None


# What zeige() sends: a JSON-safe rendering of any value, bounded as a WHOLE
# (a character budget spent across the tree) so the frame stays far below
# MAX_FRAME_BYTES whatever the program hands over. The robot shows at most
# {SHOWN_VALUE_MAX_CHARS!r} characters of it.
_SHOWN_BUDGET_CHARS = {SHOWN_BUDGET_CHARS!r}
_SHOWN_MAX_DEPTH = {SHOWN_MAX_DEPTH!r}
_SHOWN_MAX_ITEMS = {SHOWN_MAX_ITEMS!r}
_SHOWN_BIG_INT = 2 ** 53
_SHOWN_TOO_BIG_DE = 'sehr große Zahl'


def _shown(value):
    return _shown_part(value, 0, [_SHOWN_BUDGET_CHARS])


def _shown_part(value, depth, budget):
    if budget[0] <= 0:
        return '…'
    budget[0] -= 4
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if -_SHOWN_BIG_INT <= value <= _SHOWN_BIG_INT:
            return value
        try:
            return float(value)
        except OverflowError:
            return _SHOWN_TOO_BIG_DE
    if isinstance(value, float):
        if value != value or value in (float('inf'), float('-inf')):
            return repr(value)
        return value
    if isinstance(value, str):
        text = value[:max(0, min(budget[0], 1000))]
        budget[0] -= len(text)
        return text
    if isinstance(value, Greifziel):
        return repr(value)
    try:
        if depth < _SHOWN_MAX_DEPTH:
            if isinstance(value, (list, tuple, set, frozenset)):
                return [_shown_part(v, depth + 1, budget)
                        for v in itertools.islice(value, _SHOWN_MAX_ITEMS)]
            if isinstance(value, dict):
                out = {{}}
                for k, v in itertools.islice(value.items(), _SHOWN_MAX_ITEMS):
                    key = str(k)[:100]
                    budget[0] -= len(key)
                    out[key] = _shown_part(v, depth + 1, budget)
                return out
        text = repr(value)[:max(0, min(budget[0], 200))]
    except Exception:  # noqa: BLE001 — a hostile __repr__/__str__ must not break zeige
        return '<?>'
    budget[0] -= len(text)
    return text


'''

_PY_STUB_TAIL = '''
class Greifobjekt:
    """Eigener Objekt-Typ: eine Unterklasse anlegen, fertig.

        class Banane(Greifobjekt):
            tag_ids = [30, 31]
            hoehe_m = 0.040
            greiftiefe_m = 0.015

    Optional: name (sonst der Klassenname in Kleinbuchstaben), label (sonst
    der Klassenname), greifer_schliessen_rad, anfahrhoehe_m. Die Klasse wird
    beim Anlegen für diesen Lauf beim Roboter angemeldet; robot.grasp(Banane)
    und robot.sees(Banane) nehmen die Klasse oder ihren Namen.
    """

    name = None
    label = None
    tag_ids = ()
    hoehe_m = None
    greiftiefe_m = None
    greifer_schliessen_rad = None
    anfahrhoehe_m = None

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        name = cls.name or cls.__name__.lower()
        label = cls.label or cls.__name__
        _rpc.call({method!r}, [{args}], {kind!r})
'''


def _py_name_block(names) -> str:
    """Sorted names as the lines of a set display, four spaces in, ≤ 79 wide."""
    lines, line = [], '   '
    for name in sorted(names):
        item = f' {name!r},'
        if len(line) + len(item) > 79:
            lines.append(line)
            line = '   '
        line += item
    lines.append(line)
    return '\n'.join(lines)


def render_python_stub() -> str:
    limits = dataclasses.asdict(RPC_LIMITS)
    head = _PY_STUB_HEAD.format(
        notice=_GENERATED_NOTICE,
        MAX_CALLS_PER_S=limits['MAX_CALLS_PER_S'],
        BURST=limits['BURST'],
        PERCEPTION_MAX_PER_S=limits['PERCEPTION_MAX_PER_S'],
        PERCEPTION_BURST=limits['PERCEPTION_BURST'],
        MAX_FRAME_BYTES=limits['MAX_FRAME_BYTES'],
        SHOWN_VALUE_MAX_CHARS=SHOWN_VALUE_MAX_CHARS,
        SHOWN_BUDGET_CHARS=_SHOWN_BUDGET_CHARS,
        SHOWN_MAX_DEPTH=_SHOWN_MAX_DEPTH,
        SHOWN_MAX_ITEMS=_SHOWN_MAX_ITEMS,
        PUBLIC_METHODS=_py_name_block(c.name for c in ROBOT_API + CODE_ONLY_METHODS),
    )
    methods = '\n'.join(_py_method(c) for c in ROBOT_API + CODE_ONLY_METHODS)
    row = INTERNAL_METHODS_BY_NAME['register_object']
    # The seven positionals, in the row's order: the first two come from the
    # class name, the rest are the class attributes named like the row.
    attrs = {'name': 'name', 'label': 'label'}
    args = ', '.join(
        attrs.get(p.name) or ('list(cls.tag_ids)' if p.kind == 'tagids' else f'cls.{p.name}')
        for p in row.params)
    tail = _PY_STUB_TAIL.format(method=row.name, args=args, kind=row.budget)
    return head + methods + tail


# ── Java ──────────────────────────────────────────────────────────────────

# kind → the Java parameter alternatives (type, expression that marshals it
# into the Object[] the client encodes). Autoboxing covers the primitives.
_JAVA_ALTS = {
    'float': (('double', '{v}'),),
    'int': (('int', '{v}'),),
    'str': (('String', '{v}'),),
    'text': (('Object', '{v}'),),
    'bool': (('boolean', '{v}'),),
    'point': (('double[]', '{v}'),),
    'target': (('double[]', '{v}'), ('String', '{v}')),
    'ziel': (('Greifziel', '{v}.handle()'),),
    'tagids': (('int[]', '{v}'),),
    'obj': (('String', '{v}'), ('Greifobjekt', '{v}.name()')),
    # zeige(): the primitives plus exactly ONE reference overload, Object,
    # which RpcClient.shownObject dispatches at run time (String, every array
    # type, a List, a Greifziel …). Two reference overloads would make
    # `Robot.zeige("x", null)` ambiguous — a compile error a student cannot
    # read (2026-09-27 review, n3). Every rendering is bounded and JSON-safe
    # (a non-finite double becomes text, arrays and lists are JSON lists
    # sharing one budget) before EduJson frames it.
    'value': (('int', '{v}'), ('long', '{v}'), ('double', 'RpcClient.shownDouble({v})'),
              ('boolean', '{v}'), ('char', 'String.valueOf({v})'),
              ('Object', 'RpcClient.shownObject({v})')),
}

_JAVA_RETURN = {
    'none': ('void', None),
    'bool': ('boolean', 'RpcClient.asBoolean(r)'),
    'int': ('int', 'RpcClient.asInt(r)'),
    'point': ('double[]', 'RpcClient.asPoint(r)'),
    'name': ('String', 'RpcClient.asString(r)'),
    'ziel': ('Greifziel', 'toGreifziel(r)'),
}


def _java_camel(name: str) -> str:
    head, *rest = name.split('_')
    return head + ''.join(w[:1].upper() + w[1:] for w in rest)


def _java_overloads(call: ApiCall) -> list[str]:
    """Every overload of one row: the cartesian product of the parameter
    alternatives (``target`` takes a point or a name, ``obj`` a name or a
    Greifobjekt) times the trailing-default prefixes (Java has no defaults)."""
    import itertools

    n = len(call.params)
    first_default = n
    for i in range(n - 1, -1, -1):
        if call.params[i].required:
            break
        first_default = i
    ret_type, unwrap = _JAVA_RETURN[call.returns]
    out: list[str] = []
    for prefix in range(n, first_default - 1, -1):
        alts = [_JAVA_ALTS[p.kind] for p in call.params[:prefix]]
        for combo in itertools.product(*alts):
            params = ', '.join(
                f'{jtype} {_java_camel(p.name)}'
                for (jtype, _expr), p in zip(combo, call.params[:prefix]))
            marshalled = [expr.format(v=_java_camel(p.name))
                          for (_jtype, expr), p in zip(combo, call.params[:prefix])]
            marshalled += [_java_literal(p.default) for p in call.params[prefix:]]
            args = ', '.join(marshalled)
            body = (f'RpcClient.call({_java_literal(call.name)}, '
                    f'new Object[] {{{args}}}, {_java_literal(call.budget)})')
            lines = [f'    /** {_comment_safe(call.doc_de)} */',
                     f'    public static {ret_type} {call.java_name}({params}) {{']
            if unwrap is None:
                lines.append(f'        {body};')
            else:
                lines.append(f'        Object r = {body};')
                lines.append(f'        return {unwrap};')
            lines.append('    }')
            out.append('\n'.join(lines))
    return out


_JAVA_ROBOT_HEAD = '''package edubotics;

/**
 * Roboter-API für EduBotics-Programme (Java).
 *
 * <pre>
 * Robot.home();
 * Robot.moveTo(new double[] {{0.15, 0.0, 0.05}});
 * Robot.Greifziel ziel = Robot.find("wuerfel");
 * if (ziel != null) Robot.moveAbove(ziel);
 * </pre>
 *
 * Jede Methode schickt einen Aufruf an den Roboter und wartet auf die Antwort.
 * Lehnt der Roboter etwas ab, gibt es eine RpcClient.RobotError mit deutscher
 * Meldung.
 *
 * {notice}
 */
public final class Robot {{

    private Robot() {{
    }}

    /** Ein gefundenes Objekt (von Robot.find) — ein Handle für die Greif-Schritte. */
    public static final class Greifziel {{
        private final long handle;

        Greifziel(long handle) {{
            this.handle = handle;
        }}

        long handle() {{
            return handle;
        }}

        @Override
        public String toString() {{
            return "Greifziel(" + handle + ")";
        }}

        @Override
        public boolean equals(Object other) {{
            return other instanceof Greifziel && ((Greifziel) other).handle == handle;
        }}

        @Override
        public int hashCode() {{
            return Long.hashCode(handle);
        }}
    }}

    private static Greifziel toGreifziel(Object r) {{
        if (r == null) {{
            return null;
        }}
        return new Greifziel(RpcClient.asLong(EduJson.asObject(r).get("h")));
    }}

'''


def render_java_robot() -> str:
    notice = _GENERATED_NOTICE.replace('\n', '\n * ')
    body = '\n\n'.join(
        overload for call in ROBOT_API + CODE_ONLY_METHODS
        for overload in _java_overloads(call))
    return _JAVA_ROBOT_HEAD.format(notice=notice) + body + '\n}\n'


_JAVA_GREIFOBJEKT = '''package edubotics;

import java.util.Locale;

/**
 * Eigener Objekt-Typ: anlegen, fertig.
 *
 * <pre>
 * Greifobjekt banane = new Greifobjekt("Banane", new int[] {{30, 31}}, 0.040, 0.015);
 * Robot.grasp(banane);
 * </pre>
 *
 * Der Name ist das Label in Kleinbuchstaben; zwei optionale Zahlen am Ende
 * sind greiferSchliessenRad und anfahrhoeheM. Das Objekt wird beim Anlegen
 * für diesen Lauf beim Roboter angemeldet.
 *
 * {notice}
 */
public final class Greifobjekt {{

    private final String name;

    public Greifobjekt(String label, int[] tagIds, double hoeheM, double greiftiefeM) {{
        this(label, tagIds, hoeheM, greiftiefeM, null, null);
    }}

    public Greifobjekt(String label, int[] tagIds, double hoeheM, double greiftiefeM,
                       double greiferSchliessenRad) {{
        this(label, tagIds, hoeheM, greiftiefeM, Double.valueOf(greiferSchliessenRad), null);
    }}

    public Greifobjekt(String label, int[] tagIds, double hoeheM, double greiftiefeM,
                       double greiferSchliessenRad, double anfahrhoeheM) {{
        this(label, tagIds, hoeheM, greiftiefeM, Double.valueOf(greiferSchliessenRad),
             Double.valueOf(anfahrhoeheM));
    }}

    private Greifobjekt(String label, int[] tagIds, double hoeheM, double greiftiefeM,
                        Double greiferSchliessenRad, Double anfahrhoeheM) {{
        this.name = label.toLowerCase(Locale.ROOT);
        RpcClient.call({method}, new Object[] {{{args}}}, {kind});
    }}

    /** Der Name, unter dem der Roboter diesen Typ kennt. */
    public String name() {{
        return name;
    }}
}}
'''


def render_java_greifobjekt() -> str:
    row = INTERNAL_METHODS_BY_NAME['register_object']
    java_of = {'name': 'this.name', 'label': 'label'}
    args = ', '.join(java_of.get(p.name, _java_camel(p.name)) for p in row.params)
    return _JAVA_GREIFOBJEKT.format(
        notice=_GENERATED_NOTICE.replace('\n', '\n * '),
        method=_java_literal(row.name), args=args, kind=_java_literal(row.budget))


_JAVA_RPC_CLIENT = '''package edubotics;

import java.io.IOException;
import java.net.StandardProtocolFamily;
import java.net.UnixDomainSocketAddress;
import java.nio.ByteBuffer;
import java.nio.channels.SocketChannel;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Der eine Client: Rahmen, Schlüssel, Aufruf-Bremse, Antwort-Auswertung.
 *
 * {notice}
 */
public final class RpcClient {{

    static final double MAX_CALLS_PER_S = {MAX_CALLS_PER_S};
    static final int BURST = {BURST};
    static final double PERCEPTION_MAX_PER_S = {PERCEPTION_MAX_PER_S};
    static final int PERCEPTION_BURST = {PERCEPTION_BURST};
    static final int MAX_FRAME_BYTES = {MAX_FRAME_BYTES};

    static final String SOCKET_ENV = "CODE_RPC_SOCKET";
    static final String TOKEN_ENV = "CODE_RUN_TOKEN";

    static final String NO_CONNECTION_DE =
        "Keine Verbindung zum Roboter — das Programm muss über Roboter Studio gestartet werden.";
    static final String CONNECTION_LOST_DE = "Die Verbindung zum Roboter ist abgebrochen.";
    static final String FRAME_TOO_BIG_DE = "Der Aufruf ist zu groß für den Roboter.";
    static final String BAD_REPLY_DE = "Der Roboter hat unverständlich geantwortet.";

    /** Der Roboter hat einen Aufruf abgelehnt; getMessage() ist die deutsche Meldung. */
    public static final class RobotError extends RuntimeException {{
        private static final long serialVersionUID = 1L;

        RobotError(String message) {{
            super(message);
        }}
    }}

    /** A token bucket that SLEEPS until a token is available (never drops). */
    private static final class Bucket {{
        private final double rate;
        private final double burst;
        private double tokens;
        private long lastNanos;

        Bucket(double rate, int burst) {{
            this.rate = rate;
            this.burst = burst;
            this.tokens = burst;
            this.lastNanos = System.nanoTime();
        }}

        synchronized void take() {{
            while (true) {{
                long now = System.nanoTime();
                tokens = Math.min(burst, tokens + (now - lastNanos) / 1e9 * rate);
                lastNanos = now;
                if (tokens >= 1.0) {{
                    tokens -= 1.0;
                    return;
                }}
                long waitMillis = (long) Math.ceil((1.0 - tokens) / rate * 1000.0);
                try {{
                    Thread.sleep(Math.max(1L, waitMillis));
                }} catch (InterruptedException e) {{
                    Thread.currentThread().interrupt();
                    return;
                }}
            }}
        }}
    }}

    private static final Object LOCK = new Object();
    private static final Bucket CALLS = new Bucket(MAX_CALLS_PER_S, BURST);
    private static final Bucket PERCEPTION = new Bucket(PERCEPTION_MAX_PER_S, PERCEPTION_BURST);
    private static SocketChannel channel;
    private static String socketPath;
    private static String token;
    private static long nextId;

    private RpcClient() {{
    }}

    /** Where to connect and how to greet; the launcher sets it, else the two environment variables. */
    static void configure(String path, String runToken) {{
        synchronized (LOCK) {{
            socketPath = path;
            token = runToken;
        }}
    }}

    private static void ensureConnected() {{
        if (channel != null) {{
            return;
        }}
        if (socketPath == null || token == null) {{
            socketPath = System.getenv(SOCKET_ENV);
            token = System.getenv(TOKEN_ENV);
        }}
        if (socketPath == null || token == null) {{
            throw new RobotError(NO_CONNECTION_DE);
        }}
        SocketChannel ch;
        try {{
            ch = SocketChannel.open(StandardProtocolFamily.UNIX);
            ch.connect(UnixDomainSocketAddress.of(socketPath));
        }} catch (IOException | UnsupportedOperationException e) {{
            throw new RobotError(NO_CONNECTION_DE);
        }}
        channel = ch;
        call("__hello", new Object[] {{token}}, "call");
    }}

    private static void rateFloor(String kind) {{
        CALLS.take();
        if ("perception".equals(kind)) {{
            PERCEPTION.take();
        }}
    }}

    static Object call(String method, Object[] args, String kind) {{
        Map<String, Object> reply;
        synchronized (LOCK) {{
            ensureConnected();
            rateFloor(kind);
            nextId++;
            Map<String, Object> request = new LinkedHashMap<>();
            request.put("id", nextId);
            request.put("m", method);
            request.put("a", Arrays.asList(args));
            byte[] frame = encodeFrame(request);
            try {{
                writeFully(ByteBuffer.wrap(frame));
                reply = EduJson.asObject(readFrame());
            }} catch (IOException e) {{
                close();
                throw new RobotError(CONNECTION_LOST_DE);
            }}
        }}
        if (Boolean.TRUE.equals(reply.get("ok"))) {{
            return reply.get("r");
        }}
        Object e = reply.get("e");
        throw new RobotError(e == null ? BAD_REPLY_DE : String.valueOf(e));
    }}

    /** u32 big-endian length + the JSON object as UTF-8 (umlauts raw, compact). */
    static byte[] encodeFrame(Object obj) {{
        byte[] body = EduJson.encode(obj).getBytes(StandardCharsets.UTF_8);
        if (body.length > MAX_FRAME_BYTES) {{
            throw new RobotError(FRAME_TOO_BIG_DE);
        }}
        ByteBuffer out = ByteBuffer.allocate(4 + body.length);
        out.putInt(body.length);
        out.put(body);
        return out.array();
    }}

    private static void writeFully(ByteBuffer buf) throws IOException {{
        while (buf.hasRemaining()) {{
            channel.write(buf);
        }}
    }}

    private static ByteBuffer readExact(int n) throws IOException {{
        ByteBuffer buf = ByteBuffer.allocate(n);
        while (buf.hasRemaining()) {{
            if (channel.read(buf) < 0) {{
                throw new IOException("closed");
            }}
        }}
        buf.flip();
        return buf;
    }}

    private static Object readFrame() throws IOException {{
        int length = readExact(4).getInt();
        if (length < 0 || length > MAX_FRAME_BYTES) {{
            throw new RobotError(BAD_REPLY_DE);
        }}
        ByteBuffer body = readExact(length);
        String text = StandardCharsets.UTF_8.decode(body).toString();
        Object reply;
        try {{
            reply = EduJson.decode(text);
        }} catch (IllegalArgumentException e) {{
            throw new RobotError(BAD_REPLY_DE);
        }}
        if (!(reply instanceof Map)) {{
            throw new RobotError(BAD_REPLY_DE);
        }}
        return reply;
    }}

    static void close() {{
        SocketChannel ch;
        synchronized (LOCK) {{
            ch = channel;
            channel = null;
        }}
        if (ch != null) {{
            try {{
                ch.close();
            }} catch (IOException e) {{
                // nothing left to do with a socket that will not close
            }}
        }}
    }}

    static boolean asBoolean(Object r) {{
        return Boolean.TRUE.equals(r);
    }}

    static long asLong(Object r) {{
        if (r instanceof Number) {{
            return ((Number) r).longValue();
        }}
        throw new RobotError(BAD_REPLY_DE);
    }}

    static int asInt(Object r) {{
        return (int) asLong(r);
    }}

    static String asString(Object r) {{
        if (r instanceof String) {{
            return (String) r;
        }}
        throw new RobotError(BAD_REPLY_DE);
    }}

    // ── zeige(): bounded, JSON-safe renderings of a shown value ─────────────

    static final int SHOWN_MAX_ITEMS = {SHOWN_MAX_ITEMS};
    static final int SHOWN_MAX_DEPTH = {SHOWN_MAX_DEPTH};
    static final int SHOWN_MAX_CHARS = {SHOWN_TEXT_MAX_CHARS};
    // A value's texts and nodes share ONE character budget (the Python stub's
    // _SHOWN_BUDGET_CHARS; every node costs at least 4): fifty long texts or
    // a nested array must not become a frame the robot cannot take — or one
    // over MAX_FRAME_BYTES, refused in the student's own program.
    static final int SHOWN_BUDGET_CHARS = {SHOWN_BUDGET_CHARS};
    static final String SHOWN_CUT = "…";

    static Object shownDouble(double v) {{
        if (Double.isNaN(v) || Double.isInfinite(v)) {{
            return String.valueOf(v);
        }}
        return v;
    }}

    static Object shownObject(Object o) {{
        return shownValue(o, 0, new int[] {{SHOWN_BUDGET_CHARS}});
    }}

    // One node of a shown value, charged to the shared budget like the
    // Python stub's _shown_part. EVERY array type (int[], long[], String[],
    // char[], Object[], an array of arrays …) and every Iterable is a JSON
    // list — at most SHOWN_MAX_ITEMS items and SHOWN_MAX_DEPTH levels, cut
    // with "…" — never the JVM's "[J@1b6d3586" (review round 3, nb6).
    static Object shownValue(Object o, int depth, int[] budget) {{
        if (budget[0] <= 0) {{
            return SHOWN_CUT;
        }}
        budget[0] -= 4;
        if (o == null || o instanceof Boolean || o instanceof Integer || o instanceof Long
                || o instanceof Short || o instanceof Byte) {{
            return o;
        }}
        if (o instanceof String) {{
            return shownText((String) o, budget);
        }}
        if (o instanceof Character) {{
            return String.valueOf(o);
        }}
        if (o instanceof Number) {{
            return shownDouble(((Number) o).doubleValue());
        }}
        boolean array = o.getClass().isArray();
        if (array || o instanceof Iterable) {{
            if (depth >= SHOWN_MAX_DEPTH) {{
                return SHOWN_CUT;
            }}
            List<Object> out = new java.util.ArrayList<>();
            if (array) {{
                int n = java.lang.reflect.Array.getLength(o);
                for (int i = 0; i < n; i++) {{
                    if (out.size() >= SHOWN_MAX_ITEMS || budget[0] <= 0) {{
                        out.add(SHOWN_CUT);
                        break;
                    }}
                    out.add(shownValue(java.lang.reflect.Array.get(o, i), depth + 1, budget));
                }}
            }} else {{
                for (Object item : (Iterable<?>) o) {{
                    if (out.size() >= SHOWN_MAX_ITEMS || budget[0] <= 0) {{
                        out.add(SHOWN_CUT);
                        break;
                    }}
                    out.add(shownValue(item, depth + 1, budget));
                }}
            }}
            return out;
        }}
        return shownText(String.valueOf(o), budget);
    }}

    static String shownText(String s, int[] budget) {{
        int n = Math.max(0, Math.min(s.length(), Math.min(SHOWN_MAX_CHARS, budget[0])));
        budget[0] -= n;
        return s.substring(0, n);
    }}

    static double[] asPoint(Object r) {{
        if (r == null) {{
            return null;
        }}
        List<Object> items = EduJson.asArray(r);
        if (items.size() != 3) {{
            throw new RobotError(BAD_REPLY_DE);
        }}
        double[] out = new double[3];
        for (int i = 0; i < 3; i++) {{
            Object v = items.get(i);
            if (!(v instanceof Number)) {{
                throw new RobotError(BAD_REPLY_DE);
            }}
            out[i] = ((Number) v).doubleValue();
        }}
        return out;
    }}
}}
'''


def render_java_rpc_client() -> str:
    limits = dataclasses.asdict(RPC_LIMITS)
    return _JAVA_RPC_CLIENT.format(
        notice=_GENERATED_NOTICE.replace('\n', '\n * '),
        MAX_CALLS_PER_S=_java_literal(limits['MAX_CALLS_PER_S']),
        BURST=_java_literal(limits['BURST']),
        PERCEPTION_MAX_PER_S=_java_literal(limits['PERCEPTION_MAX_PER_S']),
        PERCEPTION_BURST=_java_literal(limits['PERCEPTION_BURST']),
        MAX_FRAME_BYTES=_java_literal(limits['MAX_FRAME_BYTES']),
        SHOWN_TEXT_MAX_CHARS=_java_literal(1000),
        SHOWN_BUDGET_CHARS=_java_literal(_SHOWN_BUDGET_CHARS),
        SHOWN_MAX_DEPTH=_java_literal(_SHOWN_MAX_DEPTH),
        SHOWN_MAX_ITEMS=_java_literal(_SHOWN_MAX_ITEMS),
    )


def render_java_files() -> dict[str, str]:
    return {
        'Robot.java': render_java_robot(),
        'Greifobjekt.java': render_java_greifobjekt(),
        'RpcClient.java': render_java_rpc_client(),
    }


# ── JSON ──────────────────────────────────────────────────────────────────

def _param_json(p: ApiParam) -> dict[str, Any]:
    out: dict[str, Any] = {'name': p.name, 'kind': p.kind, 'required': p.required}
    if not p.required:
        out['default'] = p.default
    for key in ('lo', 'hi', 'max_len', 'pattern'):
        value = getattr(p, key)
        if value is not None:
            out[key] = value
    if p.lo_open:
        out['lo_open'] = True
    if p.nullable:
        out['nullable'] = True
    if p.asset is not None:
        out['asset'] = p.asset
    return out


def _call_json(c: ApiCall) -> dict[str, Any]:
    return {
        'name': c.name,
        'java_name': c.java_name,
        'block_type': c.block_type,
        'table': c.table,
        'budget': c.budget,
        'returns': c.returns,
        'doc_de': c.doc_de,
        'params': [_param_json(p) for p in c.params],
    }


def export_json() -> str:
    """The editor's copy: names, Java names, params, docs, returns, limits."""
    limits: dict[str, Any] = dict(dataclasses.asdict(RPC_LIMITS))
    limits.update(CODE_PROJECT_LIMITS)
    doc = {
        'generated_by': 'physical_ai_server/workflow/robot_api.py',
        'methods': [_call_json(c) for c in ROBOT_API + CODE_ONLY_METHODS],
        'internal': [_call_json(c) for c in INTERNAL_METHODS],
        'limits': limits,
    }
    return json.dumps(doc, ensure_ascii=False, indent=2) + '\n'


def render_all() -> dict[str, str]:
    """Every generated artifact, keyed by repo-relative path."""
    java = render_java_files()
    return {
        GENERATED_PATHS['python_stub']: render_python_stub(),
        GENERATED_PATHS['java_robot']: java['Robot.java'],
        GENERATED_PATHS['java_greifobjekt']: java['Greifobjekt.java'],
        GENERATED_PATHS['java_rpc_client']: java['RpcClient.java'],
        GENERATED_PATHS['json']: export_json(),
    }
