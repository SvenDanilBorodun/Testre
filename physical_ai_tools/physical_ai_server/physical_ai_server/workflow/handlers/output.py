#!/usr/bin/env python3
#
# Copyright 2025 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Output-category block handlers.

``edubotics_log`` emits a string into the workflow status log strip the
React UI subscribes to. ``edubotics_play_sound`` is a one-shot
notification beep (the actual sound is played by the React layer when
it sees a ``[SOUND]`` log token — keeps the audio decisions out of the
ROS node).

Phase-2 additions:
- ``edubotics_speak_de`` emits ``[SPEAK:text]`` so the React layer can
  read the text out via window.speechSynthesis (de-DE voice).
- ``edubotics_play_tone`` emits ``[TONE:freq:seconds]`` for a
  parameterized beep.
- ``edubotics_toast`` emits ``[TOAST:level:seconds:text]`` so the React
  layer shows an on-screen react-hot-toast popup (severity → toast type).
"""

from __future__ import annotations

import math
import os
import re
import time
from typing import Any

from physical_ai_server.workflow.student_text import student_text


def _env_float(name: str, default: float) -> float:
    """Read a float knob; empty/whitespace and malformed values → ``default``.

    Empty counts as UNSET because compose forwards optional knobs as
    ``${VAR:-}`` — the EDUBOTICS_GRASP_HELD_MAX_RAD scar (see
    handlers/motion._safe_float, whose contract this mirrors; a local copy keeps
    output.py importable without pulling in the motion module)."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


# Cap speak text so the React side doesn't queue minutes of audio if a
# loop fires the speak block thousands of times.
MAX_SPEAK_CHARS = 240
# Cap a single log message so a student emitting `str(huge_list)` can't
# DoS the WorkflowStatus realtime channel. Audit round-3 §AG.
MAX_LOG_CHARS = 2000
TONE_FREQ_MIN = 100
TONE_FREQ_MAX = 4000
TONE_SECONDS_MIN = 0.05
TONE_SECONDS_MAX = 5.0
# Toast bounds (mirror the React-side clamp in useRosTopicSubscription).
MAX_TOAST_CHARS = 240
TOAST_SECONDS_MIN = 1
TOAST_SECONDS_MAX = 15
_TOAST_LEVELS = ('info', 'success', 'warning', 'error')


# ── German-aware value formatting (Rule §1) ──────────────────────────────────
# „melde", „sage" and „zeige Meldung" all used a bare ``str(message)``, so every
# non-text value reached the student as raw Python. Measured 2026-09-07:
#
#   melde <wahr>                 -> 'True'                       (should be „wahr")
#   melde <3>                    -> '3.0'
#   melde <finde Würfel>         -> 'Detection(centroid_px=(320, 240), bbox_px=…,
#                                    aruco_id=20, …)'
#   sage  <finde Würfel>         -> [SPEAK:Detection(…)] — READ ALOUD in de-DE
#   melde <Position von Ziel>    -> "{'x': 0.15, 'y': 0.0, 'z': 0.015}"
#
# while „verbinde(wahr, 3)" already printed „wahr3" correctly, because text_join
# is the ONE caller of the interpreter's German-aware ``_to_text``. Their value
# inputs carry no Blockly check, so a Greifziel plugs straight into „sage" in two
# drags. german_detail_lint.py's AST scopes structurally cannot see any of this.
#
# The student-facing stringifier now lives in ``workflow/student_text.py`` and is
# SHARED with ``interpreter._to_text`` (the „verbinde"-Block) and
# ``interpreter._jsonable`` (the Variablen-Tafel). It used to live HERE and be
# reached from the interpreter by a lazy import, which left the third
# stringifier — ``text_join`` — un-German: `sage <verbinde("Ich sehe ", finde
# Würfel)>` read a Python repr ALOUD. One module, imported normally by both, no
# cycle, no third opinion. The alias keeps this file's three call sites and
# their tests reading exactly as they did.
_student_text = student_text


# ── Emission rate limit (Rule §1 / classroom sanity) ─────────────────────────
# Every server-side clamp bounds ONE message; none bounded the RATE. Measured
# 2026-09-07 inside „wiederhole fortlaufend" over a 3 s window: play_sound
# 17.3/s, play_tone 18.0/s, toast 17.7/s, speak_de 17.7/s — one emission per
# loop iteration, for as long as the loop runs. On the React side that is
# ~265 stacked toasts over the editor and the run-control strip (including the
# Stopp button) in 15 s, ``speechSynthesis.cancel()+speak()`` 17×/s, and 17 new
# OscillatorNodes per second.
#
# The limiter is per-KIND and per-RUN (state lives on the ctx, which is rebuilt
# for every run), so „melde" cannot starve „sage". Beyond the budget the
# emission is DROPPED, and exactly ONE German [WARNUNG] per kind per run says so
# — dropping silently would be its own defect.
#
# ONE-VARIABLE ROLLBACK: ``EDUBOTICS_OUTPUT_MAX_PER_S=0`` disables the limit
# entirely, and the token bucket below keeps that meaning EXACTLY. It is the
# SUSTAINED rate. env-forwarding-guard: forwarded in
# robotis_ai_setup/docker/docker-compose.yml + docker-compose.opi.yml.
OUTPUT_MAX_PER_S = max(0.0, _env_float('EDUBOTICS_OUTPUT_MAX_PER_S', 5.0))

# A BURST is what hurts. A STRAIGHT CHAIN of „melde" blocks is not a burst — it
# is a program, and it is the FIRST program a twelve-year-old writes. A minimum-
# interval gate cannot tell the two apart, because Blockly executes sequential
# blocks MICROSECONDS apart: five „melde" blocks in a row printed ONE line and
# blamed the student for it („Zu viele „melde"-Meldungen"). A bucket can:
# OUTPUT_BURST back-to-back emissions are free, then OUTPUT_MAX_PER_S sustained,
# so the measured defect (17–18 emissions/s SUSTAINED inside „wiederhole
# fortlaufend") still throttles to 5/s after ~3 s while a straight-line program
# of ≤50 outputs per kind is untouched — and the „Zu viele" warning now only ever
# fires when it is TRUE.
#
# The per-kind split the first revision deferred, reduced to ONE extra number
# with a measurement behind it. 50 was right for the three kinds whose cost is
# WALL-CLOCK (a toast stacks, a „sage" blocks the speech queue, a „klang" is an
# OscillatorNode) and wrong for the one whose cost is VOLUME:
# „wiederhole 100 mal { melde <Zähler> }" — the first loop a twelve-year-old
# writes — printed 100 lines before this round and 50 after, so it LOOKED like
# the program stopped at 50.
#
# Three measurements say a log burst belongs an order of magnitude higher:
#  1. The React Protokoll already caps itself at 200 lines, NEWEST-wins
#     (workshopSlice.js: `state.log.slice(-200)`). So the display cost was
#     already bounded — and a server bucket keeps the OLDEST lines, i.e. it
#     throws away exactly the half of a loop the student is watching for.
#  2. The measured 17–18 emissions/s is not something a student can exceed: it
#     IS `interpreter.FOREVER_MIN_CYCLE_S = 0.05` (~20 Hz), a pre-existing floor.
#     The unbounded shape is „wiederhole N mal", itself capped at
#     MAX_LOOP_ITERATIONS = 10 000.
#  3. NONE of the three harms measured for this limiter involve „melde": 265
#     stacked toasts, speechSynthesis 17×/s, 17 OscillatorNodes/s.
# 500 is 2.5× the Protokoll's own cap, so every classroom loop survives intact,
# while a 10 000-iteration flood is still cut by 95 %.
OUTPUT_BURST = 50
OUTPUT_BURST_LOG = 500

# Which kinds pay wall-clock (OUTPUT_BURST) and which pay volume.
_BURST_BY_KIND = {'melde': OUTPUT_BURST_LOG}

# Every one ends „Das Programm läuft normal weiter." — the same clause the
# unshowable-variable warning carries. A twelve-year-old meets this MID-LOOP
# („zähle von 1 bis 100: melde i" lands here at 50) and a bare „Zu viele …"
# under a [WARNUNG] reads as „dein Programm ist kaputt". Only the OUTPUT is
# reduced; the program is untouched.
_RATE_LIMIT_NOTICE_DE = {
    # „melde" says the rest is GONE. The others genuinely show/play a reduced
    # stream; a dropped log line is not shown later, and „nur noch ein Teil"
    # invites a student to scroll for it.
    'melde': 'Sehr viele „melde"-Zeilen — die weiteren werden nicht mehr '
             'angezeigt. Das Programm läuft normal weiter.',
    'sage': 'Zu viele „sage"-Ansagen — es wird nur noch ein Teil vorgelesen. '
            'Das Programm läuft normal weiter.',
    'ton': 'Zu viele Töne — es wird nur noch ein Teil abgespielt. '
           'Das Programm läuft normal weiter.',
    'klang': 'Zu viele Klänge — es wird nur noch ein Teil abgespielt. '
             'Das Programm läuft normal weiter.',
    'meldung': 'Zu viele Meldungen — es wird nur noch ein Teil angezeigt. '
               'Das Programm läuft normal weiter.',
}


def _rate_ok(ctx, kind: str) -> bool:
    """True when this ``kind`` may emit now; False when it is over budget.

    A TOKEN BUCKET, not a minimum-interval gate. See ``OUTPUT_BURST`` for why:
    the interval gate could not tell a five-block straight-line program from the
    17/s spin it was written to stop, and dropped four of the five lines.

    State is per-KIND and per-RUN (it lives on the ctx, which is rebuilt for
    every run), so „melde" cannot starve „sage". Fails OPEN on any error — an
    output block must never break because its own limiter did."""
    if OUTPUT_MAX_PER_S <= 0.0:
        return True
    try:
        state = getattr(ctx, '_output_rate_state', None)
        if state is None:
            state = {}
            ctx._output_rate_state = state
        now = time.monotonic()
        burst = float(_BURST_BY_KIND.get(kind, OUTPUT_BURST))
        tokens, last = state.get(kind, (burst, now))
        tokens = min(burst, tokens + max(0.0, now - last) * OUTPUT_MAX_PER_S)
        if tokens < 1.0:
            state[kind] = (tokens, now)
            if not state.get(('warned', kind)):
                state[('warned', kind)] = True
                notice = _RATE_LIMIT_NOTICE_DE.get(kind)
                if notice:
                    ctx.log(f'[WARNUNG] {notice}')
            return False
        state[kind] = (tokens - 1.0, now)
        return True
    except Exception:  # noqa: BLE001 — the limiter never breaks an output block
        return True


def log(ctx, args: dict[str, Any]) -> None:
    if not _rate_ok(ctx, 'melde'):
        return
    # German-aware, not str(): see _student_text.
    text = _student_text(args.get('message'))
    if len(text) > MAX_LOG_CHARS:
        # Truncate so the RESULT is MAX_LOG_CHARS, not MAX_LOG_CHARS + 2. The
        # old form emitted 2002 characters for a 2000-character cap; measured
        # 2026-09-07, and the constant is the thing the WorkflowStatus channel
        # is sized against.
        text = text[:MAX_LOG_CHARS - 2] + ' …'
    # Strip ALL bracket-sentinel chars so a student's log payload can't
    # spoof [VAR:...] / [SPEAK:...] / [TONE:...] / [SOUND] tokens into
    # the React-side debug panel.
    text = text.replace('[', '(').replace(']', ')')
    # …and collapse newlines + the Unicode line/paragraph separators, exactly as
    # speak_de and toast already do — a DIFFERENT and, as first written, weaker
    # mechanism than either (see below). „melde" was the one output block that
    # emitted them verbatim, so `melde("a\n[FEHLER] …")` forged a second,
    # error-looking line in the Protokoll. destinations.py justifies its own name
    # validator as preventing precisely that injection, which is what made the
    # omission here inconsistent rather than merely untidy.
    # ONE exhaustive substitution, not a list of characters somebody remembered.
    # The four hand-picked replaces let U+000B VT, U+000C FF and U+0085 NEL
    # through — and FF is a CSS Text segment break, so a consumer rendering with
    # `white-space: pre-wrap` would show exactly the forged second line this
    # strip exists to prevent. `\s` is every Python whitespace character by
    # construction (VT, FF, NEL, U+2028/9 included), so it cannot fall behind a
    # character nobody thought of. Deliberately NOT `' '.join(text.split())`,
    # which speak_de and toast use: that also collapses RUNS of spaces, and a
    # student's „melde"-Text keeps its own spacing.
    text = re.sub(r'\s', ' ', text)
    ctx.log(text)


def play_sound(ctx, args: dict[str, Any]) -> None:
    if not _rate_ok(ctx, 'klang'):
        return
    ctx.log('[SOUND]')


def speak_de(ctx, args: dict[str, Any]) -> None:
    if not _rate_ok(ctx, 'sage'):
        return
    # German-aware, not str(): a Greifziel used to be READ ALOUD as
    # 'Detection(centroid_px=…)'. See _student_text.
    text = _student_text(args.get('text'))
    # Truncate FIRST so a multi-MB input doesn't go through the full
    # replace/strip chain. Audit round-3 §AF.
    if len(text) > MAX_SPEAK_CHARS:
        text = text[:MAX_SPEAK_CHARS]
    # Strip newlines AND Unicode line/paragraph separators (the
    # [SPEAK:..] sentinel uses dotall regex on the React side, but
    # other consumers parse line-by-line). Replace with spaces so the
    # spoken sentence still flows naturally.
    text = (
        text.replace('\r', ' ')
            .replace('\n', ' ')
            .replace(' ', ' ')
            .replace(' ', ' ')
    )
    # Collapse runs of whitespace introduced by the replaces.
    text = ' '.join(text.split())
    if not text:
        # „sage" with empty/whitespace/None text emitted NOTHING AT ALL — no
        # sentinel and no log line, so a student who wired an empty variable in
        # saw a block that appeared to do nothing and had no way to tell whether
        # it had run. Say so instead of failing silent.
        ctx.log('[WARNUNG] „sage" hat keinen Text bekommen — es wird nichts '
                'vorgelesen.')
        return
    # Forbid both brackets so a malicious or innocent string can't
    # inject another sentinel (e.g., a student typing "[SOUND]" inside
    # their speak text would otherwise trigger a beep). Audit §B6.
    text = text.replace('[', ' ').replace(']', ' ')
    ctx.log(f'[SPEAK:{text}]')


def play_tone(ctx, args: dict[str, Any]) -> None:
    if not _rate_ok(ctx, 'ton'):
        return
    try:
        freq = float(args.get('freq', 880))
    except (TypeError, ValueError):
        freq = 880.0
    try:
        seconds = float(args.get('seconds', 0.25))
    except (TypeError, ValueError):
        seconds = 0.25
    # NaN falls through BOTH ends of a min/max clamp and lands on the UPPER
    # limit — the same limit-seeking pattern CLAUDE.md flags in the edu6 driver.
    # Measured 2026-09-07: freq=nan and freq=inf both emitted
    # [TONE:4000.0:5.000], i.e. the loudest tone this block can make, for the
    # longest it can make it. toast's int(round(nan)) raises and correctly falls
    # back to its default; this one did not. Reachable the same way as
    # wait_seconds' NaN — variables_get carries no Blockly output type.
    if not math.isfinite(freq):
        freq = 880.0
    if not math.isfinite(seconds):
        seconds = 0.25
    freq = max(TONE_FREQ_MIN, min(TONE_FREQ_MAX, freq))
    seconds = max(TONE_SECONDS_MIN, min(TONE_SECONDS_MAX, seconds))
    ctx.log(f'[TONE:{freq:.1f}:{seconds:.3f}]')


def toast(ctx, args: dict[str, Any]) -> None:
    """Emit a ``[TOAST:level:seconds:text]`` sentinel the React layer renders as
    a react-hot-toast popup. Frontend-only effect — like speak_de / play_tone the
    ROS node only logs the sentinel; ``level`` ∈ {info, success, warning, error}
    maps to the toast severity, ``seconds`` is the display duration."""
    if not _rate_ok(ctx, 'meldung'):
        return
    level = str(args.get('level', 'info')).strip().lower()
    if level not in _TOAST_LEVELS:
        level = 'info'
    # `math.isfinite` FIRST, exactly as play_tone does. The shipped play_tone
    # comment claimed „toast's int(round(nan)) raises and correctly falls back";
    # that holds for nan (ValueError) and NOT for inf — `int(round(inf))` raises
    # OverflowError, which this `except` never named, so „zeige Meldung … für
    # <inf> Sekunden" aborted the run with an ENGLISH Python message on a
    # student-facing surface („cannot convert float infinity to integer",
    # Rule §1). SECONDS is a clamped field_number in the block, so the reachable
    # path is a hand-built /workflow/start payload — untrusted-surface
    # hardening, and rosbridge authenticates nobody.
    try:
        raw = float(args.get('seconds', 3))
        seconds = int(round(raw)) if math.isfinite(raw) else 3
    except (TypeError, ValueError, OverflowError):
        seconds = 3
    seconds = max(TOAST_SECONDS_MIN, min(TOAST_SECONDS_MAX, seconds))
    # German-aware, not str(): see _student_text.
    text = _student_text(args.get('text'))
    # Truncate FIRST so a multi-MB input doesn't go through the full strip chain.
    if len(text) > MAX_TOAST_CHARS:
        text = text[:MAX_TOAST_CHARS]
    # Collapse newlines (the [TOAST:..] token is single-line) and strip the
    # bracket chars so a student's text can't break the sentinel or spoof
    # another token (e.g. typing "[SOUND]" into the message).
    text = (
        text.replace('\r', ' ')
            .replace('\n', ' ')
            .replace('[', '(')
            .replace(']', ')')
    )
    text = ' '.join(text.split())
    ctx.log(f'[TOAST:{level}:{seconds}:{text}]')
