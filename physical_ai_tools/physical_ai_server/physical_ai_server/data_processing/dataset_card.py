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

"""The dataset README (Aufnahme 2.0 round 5, spec §7.3, owner decisions O5 + D6).

LeRobot's own card (``lerobot.datasets.utils.create_lerobot_dataset_card``,
the template shipped inside the lerobot wheel), rebuilt on EVERY upload so a
resumed dataset's README matches its ``meta/info.json``. It names the repo_id;
a PUBLIC dataset's card carries an explicit ``license: apache-2.0`` (LeRobot's
default card has no license line, and a private dataset gets none).

The README's YAML front matter is load-bearing: ``huggingface_hub`` validates
it (``POST /api/validate-yaml``) before the commit, and an invalid one made
``upload_large_folder`` retry the commit forever.
"""

from __future__ import annotations

from typing import Iterable, Optional

PUBLIC_LICENSE = 'apache-2.0'


def card_tags(tags: Optional[Iterable]) -> list:
    """Non-empty string tags, first occurrence wins (LeRobot adds ``LeRobot``)."""
    out = []
    for tag in tags or ():
        if tag is None:
            continue
        text = str(tag).strip()
        if text and text not in out:
            out.append(text)
    return out


def build_dataset_card(repo_id: str, info: dict, tags: Optional[Iterable], public: bool) -> str:
    """The README.md text for ``repo_id`` (``info`` = the dataset's meta/info.json)."""
    from lerobot.datasets.utils import create_lerobot_dataset_card

    kwargs = {'license': PUBLIC_LICENSE} if public else {}
    card = create_lerobot_dataset_card(
        tags=card_tags(tags), dataset_info=info, repo_id=repo_id, **kwargs)
    return str(card)
