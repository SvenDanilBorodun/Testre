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
"""

from __future__ import annotations

from typing import Any


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


def log(ctx, args: dict[str, Any]) -> None:
    message = args.get('message')
    if message is None:
        message = ''
    text = str(message)
    if len(text) > MAX_LOG_CHARS:
        text = text[:MAX_LOG_CHARS] + ' …'
    # Strip ALL bracket-sentinel chars so a student's log payload can't
    # spoof [VAR:...] / [SPEAK:...] / [TONE:...] / [SOUND] tokens into
    # the React-side debug panel.
    text = text.replace('[', '(').replace(']', ')')
    ctx.log(text)


def play_sound(ctx, args: dict[str, Any]) -> None:
    ctx.log('[SOUND]')


def speak_de(ctx, args: dict[str, Any]) -> None:
    text = args.get('text')
    if text is None:
        text = ''
    text = str(text)
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
        return
    # Forbid both brackets so a malicious or innocent string can't
    # inject another sentinel (e.g., a student typing "[SOUND]" inside
    # their speak text would otherwise trigger a beep). Audit §B6.
    text = text.replace('[', ' ').replace(']', ' ')
    ctx.log(f'[SPEAK:{text}]')


def play_tone(ctx, args: dict[str, Any]) -> None:
    try:
        freq = float(args.get('freq', 880))
    except (TypeError, ValueError):
        freq = 880.0
    try:
        seconds = float(args.get('seconds', 0.25))
    except (TypeError, ValueError):
        seconds = 0.25
    freq = max(TONE_FREQ_MIN, min(TONE_FREQ_MAX, freq))
    seconds = max(TONE_SECONDS_MIN, min(TONE_SECONDS_MAX, seconds))
    ctx.log(f'[TONE:{freq:.1f}:{seconds:.3f}]')
