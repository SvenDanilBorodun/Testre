"""coco_classes.py is the single source of truth for the editor's
object-class dropdown. The frontend has a hardcoded mirror in
``messages_de.js``; a Jest test diffs the two on the JS side. This
Python-side test guards against silent server-side renames."""

from __future__ import annotations

from physical_ai_server.workflow.coco_classes import (
    ALLOWED_CLASS_LABELS,
    COCO_CLASSES,
    ID_TO_LABEL,
)


EXPECTED_LABELS = (
    'Flasche', 'Tasse', 'Gabel', 'Löffel', 'Schüssel', 'Banane',
    'Apfel', 'Orange', 'Karotte', 'Brokkoli', 'Maus', 'Fernbedienung',
    'Handy', 'Buch', 'Schere', 'Teddybär',
)


def test_class_labels_unchanged():
    """If you intentionally change this list, also update the frontend
    OBJECT_CLASSES array in messages_de.js, and bump the Jest sync test."""
    assert tuple(ALLOWED_CLASS_LABELS) == EXPECTED_LABELS


def test_id_label_round_trip():
    for label, coco_id in COCO_CLASSES.items():
        assert ID_TO_LABEL[coco_id] == label


def test_no_duplicate_ids():
    ids = list(COCO_CLASSES.values())
    assert len(ids) == len(set(ids))
