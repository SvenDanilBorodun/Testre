#!/usr/bin/env python3
#
# Copyright 2025 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Curated subset of the COCO-80 dropdown that students see in Roboter Studio.

Single source of truth — the frontend mirror in
``components/Workshop/blocks/messages_de.js`` is verified by a Jest test.
Excluded for safety/ambiguity: ``knife``, ``fire hydrant``,
``sandwich``/``pizza``/``donut``/``cake`` (visually inconsistent at table
scale), animals, vehicles.
"""

# German label -> COCO 0-indexed class id (Megvii YOLOX, COCO 80 classes)
COCO_CLASSES: dict[str, int] = {
    'Flasche': 39,
    'Tasse': 41,
    'Gabel': 42,
    'Löffel': 44,
    'Schüssel': 45,
    'Banane': 46,
    'Apfel': 47,
    'Orange': 49,
    'Karotte': 51,
    'Brokkoli': 50,
    'Maus': 64,
    'Fernbedienung': 65,
    'Handy': 67,
    'Buch': 73,
    'Schere': 76,
    'Teddybär': 77,
}

ID_TO_LABEL: dict[int, str] = {v: k for k, v in COCO_CLASSES.items()}

ALLOWED_CLASS_LABELS: tuple[str, ...] = tuple(COCO_CLASSES.keys())
ALLOWED_CLASS_IDS: tuple[int, ...] = tuple(COCO_CLASSES.values())
