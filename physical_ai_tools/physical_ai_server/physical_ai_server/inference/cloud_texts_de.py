#!/usr/bin/env python3
#
# Copyright 2026 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
"""Every Cloud-Inferenz sentence the robot shows a student (spec §5.3, Rule §1).

German with literal umlauts; the wire carries only error CODES (cloud_protocol.ERROR_CODES), and
the robot words them here. A sentence never interpolates a token, a URL or a library message.
``image_size_de`` builds its ``×`` with ``%`` so the sign stays inside one text constant (F-4: a
lone ``×`` constant is an icon glyph to the shipped-Python glyph test). test_cloud_texts_de.py is
the fence (german-strings-lint does not scan this file).
"""

from physical_ai_server.inference import cloud_protocol

STARTED_DE = 'Cloud-Inferenz gestartet.'
STOPPED_DE = 'Cloud-Inferenz gestoppt.'
NOT_RUNNING_DE = 'Gerade läuft keine Cloud-Inferenz.'
NOT_INFERABLE_DE = 'Dieser Roboter kann keine trainierten Modelle ausführen.'
BAD_SESSION_DE = 'Die Cloud-Sitzung ist ungültig. Bitte starte die Inferenz neu.'   # connect or edge token malformed
LEADER_ACTIVE_DE = ('Die Cloud-Inferenz kann nicht starten, solange der Leader-Arm aktiv ist. Bitte zuerst '
                    'in den Follower-Modus wechseln (Leader-Schalter im Inferenz-Tab) und erneut starten.')
LEADER_BECAME_ACTIVE_DE = ('Die Cloud-Inferenz wurde beendet, weil der Leader-Arm aktiv geworden ist. Bitte '
                           'den Follower-Modus einschalten und erneut starten.')
START_FAILED_DE = 'Die Cloud-Inferenz konnte nicht gestartet werden. Bitte versuche es erneut.'
SESSION_INVALID_DE = 'Die Cloud-Sitzung ist ungültig oder abgelaufen. Bitte starte die Inferenz neu.'
EXPIRED_DE = 'Die Zeit für diesen Cloud-Lauf ist abgelaufen.'
SUPERSEDED_DE = ('Dieser Lauf wurde beendet, weil du an einem anderen Roboter oder in einem anderen '
                 'Fenster einen neuen Cloud-Lauf gestartet hast.')
CLIENT_GONE_DE = 'Die Inferenz-Seite antwortet nicht mehr (Fenster geschlossen?). Der Cloud-Lauf wurde beendet.'
ENDED_DE = 'Der Cloud-Lauf wurde beendet.'
REPLACED_DE = 'Die Cloud-Sitzung wird jetzt von einem anderen Roboter-Prozess benutzt.'
SESSION_CHECK_FAILED_DE = 'Die Cloud konnte die Sitzung gerade nicht prüfen. Bitte versuche es gleich noch einmal.'
MODEL_LOAD_FAILED_DE = 'Das Modell konnte in der Cloud nicht geladen werden.'
MODEL_TYPE_DE = 'Dieses Modell kann noch nicht in der Cloud ausgeführt werden – in der Cloud laufen bisher nur ACT-Modelle.'
CAMERAS_DE = 'Die Kameras des Roboters passen nicht zu den Kameras, mit denen das Modell trainiert wurde.'
IMAGE_SIZE_GENERIC_DE = 'Die Kamerabilder haben nicht die Auflösung, mit der das Modell trainiert wurde.'
STATE_DE = 'Die Gelenkwerte des Roboters passen nicht zum Modell.'
BAD_FRAME_DE = 'Die Cloud hat eine Nachricht des Roboters abgelehnt. Bitte die Roboter-Software aktualisieren.'
VERSION_ROBOT_OLD_DE = 'Roboter-Software aktualisieren, um die Cloud-Inferenz zu nutzen.'
VERSION_CLOUD_OLD_DE = 'Die Cloud-Inferenz wird gerade aktualisiert. Bitte versuche es später erneut.'
UNREACHABLE_DE = 'Die Cloud ist nicht erreichbar. Bitte die Internetverbindung des Roboters prüfen.'
CLOUD_UNAVAILABLE_DE = 'Die Cloud-Inferenz ist gerade nicht verfügbar. Bitte versuche es später erneut.'
LOADING_TIMEOUT_DE = 'Die Cloud-GPU ist nicht rechtzeitig gestartet. Bitte versuche es erneut.'
NONFINITE_DE = 'Die Cloud hat ungültige Werte geschickt – die Inferenz wurde beendet.'
MALFORMED_DE = 'Die Cloud hat einen unvollständigen Plan geschickt – die Inferenz wurde beendet.'
INTERNAL_DE = 'Bei der Cloud-Inferenz ist auf dem Roboter ein Fehler aufgetreten.'


def image_size_de(name, w, h, want_w, want_h):     # F-4: '%' keeps the × inside a text constant
    return ('Kamera „%s“ liefert %d×%d Pixel, das Modell wurde mit %d×%d Pixeln trainiert. '
            'Bitte die Kamera-Auflösung prüfen.' % (name, w, h, want_w, want_h))


def not_jpeg_de(name):
    return f'Kamera „{name}“ liefert kein JPEG-Bild.'


def stall_de(seconds):        # R-15: renders the CLAMPED stall_end_s
    return (f'Seit {seconds:.0f} Sekunden kam kein neuer Plan aus der Cloud – die Inferenz wurde beendet. '
            'Bitte die Internetverbindung prüfen.')


def no_data_de(source):       # R-13: source = f'Kamera „{name}“' or 'Der Follower-Arm'
    return f'Vom Roboter kamen keine vollständigen Daten: {source} liefert nichts. Bitte prüfen und erneut starten.'


def image_too_big_de(name):
    return f'Das Bild der Kamera „{name}“ ist zu groß für die Cloud.'


def state_dim_de(got, want):
    return f'Das Modell erwartet {got} Gelenkwerte, der Roboter hat {want}.'


def action_dim_de(got, want):
    return f'Das Modell liefert {got} Gelenkwerte, der Roboter erwartet {want}.'


# A wire code (retry=False) -> its sentence. 'version' and 'ended' are decided below.
_SERVER_CODE_DE = {
    'bad_frame': BAD_FRAME_DE,
    'auth': SESSION_INVALID_DE,
    'replaced': REPLACED_DE,
    'unavailable': SESSION_CHECK_FAILED_DE,
    'model': MODEL_LOAD_FAILED_DE,
    'model_type': MODEL_TYPE_DE,
    'cameras': CAMERAS_DE,
    'shape': IMAGE_SIZE_GENERIC_DE,
    'state': STATE_DE,
}
# The DB end reason an `ended` frame carries -> its sentence; a deliberate end has none.
_ENDED_REASON_DE = {
    'user_stop': '',
    'robot_stop': '',
    'expired': EXPIRED_DE,
    'superseded': SUPERSEDED_DE,
    'client_gone': CLIENT_GONE_DE,
}
# The client's own end kinds (EndInfo kind 'robot').
_ROBOT_KIND_DE = {
    'unreachable': UNREACHABLE_DE,
    'cloud_unavailable': CLOUD_UNAVAILABLE_DE,
    'internal': INTERNAL_DE,
}


def end_sentence(end) -> str:
    """The sentence for a client EndInfo ``(kind, code, reason, server_v)``; ``''`` for a deliberate
    end (``ended`` with ``user_stop``/``robot_stop``). Never raises; anything unknown reads as
    INTERNAL_DE (a server code it has no sentence for: ENDED_DE)."""
    kind = getattr(end, 'kind', None)
    code = getattr(end, 'code', None)
    reason = getattr(end, 'reason', None)
    if kind == 'server':
        if code == 'ended':
            return _ENDED_REASON_DE.get(reason, ENDED_DE) if type(reason) is str else ENDED_DE
        if code == 'version':
            server_v = getattr(end, 'server_v', None)
            try:
                newer_robot = bool(server_v) and cloud_protocol.PROTOCOL_VERSION > max(server_v)
            except (TypeError, ValueError):
                newer_robot = False
            return VERSION_CLOUD_OLD_DE if newer_robot else VERSION_ROBOT_OLD_DE
        if type(code) is str and code in _SERVER_CODE_DE:
            return _SERVER_CODE_DE[code]
        return ENDED_DE if type(code) is str else INTERNAL_DE
    if kind == 'robot':
        if code == 'session_gone':          # D38: a frameless 401/404 from Modal's proxy
            return ENDED_DE if reason == 'after_ready' else SESSION_INVALID_DE
        if type(code) is str and code in _ROBOT_KIND_DE:
            return _ROBOT_KIND_DE[code]
    return INTERNAL_DE


def schedule_end_sentence(kind, stall_end_s) -> str:
    """The sentence for a cloud_schedule end: 'stall' | 'loading_timeout' | 'nonfinite' | 'malformed'."""
    if kind == 'stall':
        return stall_de(stall_end_s)
    return {'loading_timeout': LOADING_TIMEOUT_DE, 'nonfinite': NONFINITE_DE,
            'malformed': MALFORMED_DE}.get(kind, INTERNAL_DE)
