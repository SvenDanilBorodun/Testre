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
"""

from __future__ import annotations

from typing import Any


def log(ctx, args: dict[str, Any]) -> None:
    message = args.get('message')
    if message is None:
        message = ''
    ctx.log(str(message))


def play_sound(ctx, args: dict[str, Any]) -> None:
    ctx.log('[SOUND]')
