"""Roboter-API für EduBotics-Programme (Python).

    import robot
    robot.home()
    robot.move_to([0.15, 0.0, 0.05])
    ziel = robot.find("wuerfel")
    if ziel:
        robot.move_above(ziel)

Jede Funktion schickt einen Aufruf an den Roboter und wartet auf die Antwort.
Lehnt der Roboter etwas ab, gibt es einen RobotError mit deutscher Meldung.

GENERATED FILE — do not edit by hand. Rendered from the table in
physical_ai_server/workflow/robot_api.py; test_robot_api_generated.py
refuses a drift and documents how to regenerate.
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
MAX_CALLS_PER_S = 200.0
BURST = 50
PERCEPTION_MAX_PER_S = 20.0
PERCEPTION_BURST = 5
MAX_FRAME_BYTES = 65536

_SOCKET_ENV = 'CODE_RPC_SOCKET'
_TOKEN_ENV = 'CODE_RUN_TOKEN'

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
        return f'Greifziel({self._h})'

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
    package's tests assert over this file."""

    def __init__(self):
        self._sock = None
        self._lock = threading.RLock()
        self._next_id = 0
        self._calls = _Bucket(MAX_CALLS_PER_S, BURST)
        self._perception = _Bucket(PERCEPTION_MAX_PER_S, PERCEPTION_BURST)
        self.project_root = None

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
        if self.project_root and path.startswith(self.project_root):
            path = os.path.relpath(path, self.project_root)
        else:
            path = os.path.basename(path)
        return path, int(frame.f_lineno)

    def call(self, method, args, kind):
        with self._lock:
            self._ensure_connected()
            self._rate_floor(kind)
            self._next_id += 1
            request = {'id': self._next_id, 'm': method, 'a': list(args)}
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
# 2000 characters of it.
_SHOWN_BUDGET_CHARS = 4000
_SHOWN_MAX_DEPTH = 3
_SHOWN_MAX_ITEMS = 50
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
                out = {}
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


def home():
    """Fährt den Arm in die Grundstellung; der Greifer bleibt, wie er ist."""
    _rpc.call('home', [], 'call')

def open_gripper():
    """Öffnet den Greifer."""
    _rpc.call('open_gripper', [], 'call')

def close_gripper():
    """Schließt den Greifer."""
    _rpc.call('close_gripper', [], 'call')

def move_to(target):
    """Fährt über das Ziel: ein Punkt [x, y, z] in Metern oder der Name eines gemerkten Ziels."""
    _rpc.call('move_to', [target], 'call')

def pickup(target):
    """Nimmt an dieser Stelle etwas auf: hinfahren, absenken, Greifer schließen, anheben."""
    _rpc.call('pickup', [target], 'call')

def drop_at(target):
    """Legt das Gehaltene über dem Ziel ab und öffnet den Greifer."""
    _rpc.call('drop_at', [target], 'call')

def wait(seconds):
    """Wartet so viele Sekunden (höchstens 300)."""
    _rpc.call('wait', [seconds], 'call')

def replay(name, speed=1.0):
    """Spielt eine aufgenommene Bewegung ab; speed 1.0 ist die Geschwindigkeit der Aufnahme, höchstens 3.0."""
    _rpc.call('replay', [name, speed], 'call')

def move_above(ziel):
    """Fährt in Anfahrhöhe über das gefundene Objekt."""
    _rpc.call('move_above', [_handle(ziel)], 'call')

def descend_to(ziel):
    """Senkt den geöffneten Greifer auf das gefundene Objekt ab."""
    _rpc.call('descend_to', [_handle(ziel)], 'call')

def close_on_object(ziel):
    """Schließt den Greifer um das gefundene Objekt."""
    _rpc.call('close_on_object', [_handle(ziel)], 'call')

def lift():
    """Hebt den Greifer gerade nach oben an, so weit der Arm kann."""
    _rpc.call('lift', [], 'call')

def grasp(obj):
    """Greift das nächste sichtbare Objekt dieses Typs: finden, anfahren, schließen, prüfen."""
    _rpc.call('grasp', [_type_name(obj)], 'call')

def mark_done(ziel):
    """Merkt dieses Objekt als erledigt, damit es nicht noch einmal gefunden wird."""
    _rpc.call('mark_done', [_handle(ziel)], 'call')

def pin(name, x, y, z):
    """Merkt den Punkt [x, y, z] in Metern unter diesem Namen als Ziel für move_to und drop_at."""
    _rpc.call('pin', [name, x, y, z], 'call')

def pin_current(name):
    """Merkt die Stelle, an der der Greifer gerade steht, unter diesem Namen als Ziel."""
    _rpc.call('pin_current', [name], 'call')

def log(message):
    """Schreibt eine Zeile ins Protokoll — Zahlen und Texte, höchstens 2000 Zeichen."""
    _rpc.call('log', [message], 'call')

def beep():
    """Spielt einen kurzen Signalton ab — hörbar im Browser."""
    _rpc.call('beep', [], 'call')

def speak(text):
    """Liest den Text mit deutscher Stimme vor (höchstens 240 Zeichen)."""
    _rpc.call('speak', [text], 'call')

def tone(freq=880.0, seconds=0.25):
    """Spielt einen Ton mit dieser Frequenz in Hertz für so viele Sekunden (höchstens 5)."""
    _rpc.call('tone', [freq, seconds], 'call')

def toast(text, level='info', seconds=3):
    """Zeigt eine Meldung auf dem Bildschirm; level ist info, success, warning oder error, seconds höchstens 15."""
    _rpc.call('toast', [text, level, seconds], 'call')

def counter_reset(name):
    """Setzt den Zähler mit diesem Namen auf 0."""
    _rpc.call('counter_reset', [name], 'call')

def counter_add(name):
    """Erhöht den Zähler mit diesem Namen um 1."""
    _rpc.call('counter_add', [name], 'call')

def ziel(name):
    """Gibt den Namen eines gemerkten Ziels für move_to und drop_at zurück; ein unbekannter Name wird abgelehnt."""
    return _rpc.call('ziel', [name], 'call')

def sees(obj):
    """Prüft, ob gerade ein noch nicht erledigtes Objekt dieses Typs zu sehen ist."""
    return _rpc.call('sees', [_type_name(obj)], 'perception')

def count(obj):
    """Zählt die sichtbaren, noch nicht erledigten Objekte dieses Typs."""
    return _rpc.call('count', [_type_name(obj)], 'perception')

def wait_until_seen(obj, timeout=10.0):
    """Wartet, bis ein Objekt dieses Typs zu sehen ist; False, wenn es nach timeout Sekunden nicht da ist."""
    return _rpc.call('wait_until_seen', [_type_name(obj), timeout], 'perception')

def wait_until_held(timeout=10.0):
    """Wartet, bis der Greifer etwas hält; False, wenn er nach timeout Sekunden noch leer ist."""
    return _rpc.call('wait_until_held', [timeout], 'call')

def find(obj):
    """Findet das nächste greifbare Objekt dieses Typs und gibt ein Greifziel zurück, sonst None."""
    r = _rpc.call('find', [_type_name(obj)], 'perception')
    return _greifziel(r)

def object_position(ziel):
    """Gibt die Position [x, y, z] des Greifziels in Metern zurück."""
    r = _rpc.call('object_position', [_handle(ziel)], 'perception')
    return _point(r)

def is_holding():
    """Prüft, ob der Greifer etwas hält."""
    return _rpc.call('is_holding', [], 'call')

def counter_get(name):
    """Gibt den Wert des Zählers zurück; 0, wenn er noch nie gesetzt wurde."""
    return _rpc.call('counter_get', [name], 'call')

def zeige(name, wert):
    """Zeigt einen Wert unter diesem Namen im Variablen-Bereich an — Zahlen, Texte, Listen; höchstens 40 Zeichen Name."""
    _rpc.call('zeige', [name, _shown(wert)], 'call')

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
        _rpc.call('register_object', [name, label, list(cls.tag_ids), cls.hoehe_m, cls.greiftiefe_m, cls.greifer_schliessen_rad, cls.anfahrhoehe_m], 'perception')
