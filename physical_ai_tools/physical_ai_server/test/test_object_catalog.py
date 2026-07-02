"""Object-catalog validation tests for the named-object grasp workflow.

Pure stdlib — runs without a container. Covers schema validation, the
type↔tag_ids / tag_id↔recipe indices, env-tunable tag size, the German fail-loud
paths (invalid catalog, unknown type, duplicate tag id), and the fixed,
fleet-wide object set (:func:`fixed_catalog`).
"""

from __future__ import annotations

import pytest

from physical_ai_server.workflow.object_catalog import (
    ObjectCatalog,
    ObjectCatalogError,
    fixed_catalog,
    parse_catalog,
)


def _valid_dict() -> dict:
    return {
        'tag_size_m': 0.024,
        'types': {
            'banane': {
                'label_de': 'Banane',
                'tag_ids': [20, 21, 22, 23, 24],
                'object_height_m': 0.040,
                'grasp_depth_m': 0.015,
                'gripper_close_rad': -0.30,
                'approach_clear_m': 0.06,
            },
            'wuerfel_blau': {
                'label_de': 'Blauer Würfel',
                'tag_ids': [30],
                'object_height_m': 0.030,
                'grasp_depth_m': 0.012,
                'gripper_close_rad': -0.25,
            },
        },
    }


# ── happy path + indices ─────────────────────────────────────────────────────
def test_parse_valid_builds_indices():
    cat = parse_catalog(_valid_dict())
    assert isinstance(cat, ObjectCatalog)
    assert cat.tag_size_m == pytest.approx(0.024)
    assert cat.type_names() == ['banane', 'wuerfel_blau']  # catalog order
    assert cat.labels() == [('banane', 'Banane'), ('wuerfel_blau', 'Blauer Würfel')]
    assert cat.tag_ids_for_type('banane') == (20, 21, 22, 23, 24)
    assert cat.tag_ids_for_type('wuerfel_blau') == (30,)
    assert cat.all_tag_ids() == frozenset({20, 21, 22, 23, 24, 30})


def test_recipe_for_tag_maps_each_copy_to_its_type():
    cat = parse_catalog(_valid_dict())
    for tid in (20, 21, 22, 23, 24):
        r = cat.recipe_for_tag(tid)
        assert r is not None and r.type_name == 'banane'
    r30 = cat.recipe_for_tag(30)
    assert r30 is not None and r30.type_name == 'wuerfel_blau'
    # unknown tag in scene -> ignored, not an error
    assert cat.recipe_for_tag(999) is None


def test_recipe_fields_preserved():
    cat = parse_catalog(_valid_dict())
    r = cat.recipe_for_type('banane')
    assert r.label_de == 'Banane'
    assert r.object_height_m == pytest.approx(0.040)
    assert r.grasp_depth_m == pytest.approx(0.015)
    assert r.gripper_close_rad == pytest.approx(-0.30)
    assert r.approach_clear_m == pytest.approx(0.06)


def test_approach_clear_defaults_when_missing():
    cat = parse_catalog(_valid_dict())
    # wuerfel_blau omits approach_clear_m -> default 0.06
    assert cat.recipe_for_type('wuerfel_blau').approach_clear_m == pytest.approx(0.06)


# ── tag-size resolution / override ───────────────────────────────────────────
def test_tag_size_from_env_when_catalog_omits(monkeypatch):
    monkeypatch.setenv('EDUBOTICS_TAG_SIZE_M', '0.030')
    data = _valid_dict()
    del data['tag_size_m']
    assert parse_catalog(data).tag_size_m == pytest.approx(0.030)


def test_catalog_tag_size_overrides_env(monkeypatch):
    monkeypatch.setenv('EDUBOTICS_TAG_SIZE_M', '0.030')
    # catalog has 0.024 -> wins over env 0.030
    assert parse_catalog(_valid_dict()).tag_size_m == pytest.approx(0.024)


def test_tag_size_default_when_nothing_set(monkeypatch):
    monkeypatch.delenv('EDUBOTICS_TAG_SIZE_M', raising=False)
    data = _valid_dict()
    del data['tag_size_m']
    assert parse_catalog(data).tag_size_m == pytest.approx(0.024)


def test_bad_env_tag_size_falls_back(monkeypatch):
    monkeypatch.setenv('EDUBOTICS_TAG_SIZE_M', 'not-a-number')
    data = _valid_dict()
    del data['tag_size_m']
    assert parse_catalog(data).tag_size_m == pytest.approx(0.024)


@pytest.mark.parametrize('bad', ['nan', 'inf', '-inf'])
def test_nonfinite_env_tag_size_falls_back(monkeypatch, bad):
    # NaN/Inf slip past a bare "<= 0" check (NaN comparisons are always False) —
    # the loader must reject them explicitly so a bad env can't poison the
    # tag-edge sanity gate (which compares the measured side to tag_size_m).
    monkeypatch.setenv('EDUBOTICS_TAG_SIZE_M', bad)
    data = _valid_dict()
    del data['tag_size_m']
    assert parse_catalog(data).tag_size_m == pytest.approx(0.024)


@pytest.mark.parametrize('bad', [float('nan'), float('inf'), float('-inf')])
def test_nonfinite_catalog_tag_size_fails(bad):
    data = _valid_dict()
    data['tag_size_m'] = bad
    with pytest.raises(ObjectCatalogError, match='tag_size_m'):
        parse_catalog(data)


# ── fail-loud validation ─────────────────────────────────────────────────────
def test_non_dict_top_level_fails():
    with pytest.raises(ObjectCatalogError):
        parse_catalog([1, 2, 3])


def test_missing_types_fails():
    with pytest.raises(ObjectCatalogError):
        parse_catalog({'tag_size_m': 0.024})


def test_empty_types_fails():
    with pytest.raises(ObjectCatalogError):
        parse_catalog({'types': {}})


def test_duplicate_tag_id_across_types_fails():
    data = _valid_dict()
    data['types']['wuerfel_blau']['tag_ids'] = [20]  # collides with banane
    with pytest.raises(ObjectCatalogError, match='mehreren Objekten'):
        parse_catalog(data)


def test_duplicate_tag_id_within_type_fails():
    data = _valid_dict()
    data['types']['banane']['tag_ids'] = [20, 20]
    with pytest.raises(ObjectCatalogError, match='doppelt'):
        parse_catalog(data)


def test_missing_label_fails():
    data = _valid_dict()
    del data['types']['banane']['label_de']
    with pytest.raises(ObjectCatalogError, match='label_de'):
        parse_catalog(data)


def test_empty_tag_ids_fails():
    data = _valid_dict()
    data['types']['banane']['tag_ids'] = []
    with pytest.raises(ObjectCatalogError, match='tag_ids'):
        parse_catalog(data)


def test_non_integer_tag_id_fails():
    data = _valid_dict()
    data['types']['banane']['tag_ids'] = [20, 'x']
    with pytest.raises(ObjectCatalogError):
        parse_catalog(data)


def test_bool_tag_id_rejected():
    # bool is an int subclass — must be rejected explicitly
    data = _valid_dict()
    data['types']['banane']['tag_ids'] = [True]
    with pytest.raises(ObjectCatalogError):
        parse_catalog(data)


def test_negative_object_height_fails():
    data = _valid_dict()
    data['types']['banane']['object_height_m'] = -0.01
    with pytest.raises(ObjectCatalogError, match='object_height_m'):
        parse_catalog(data)


def test_grasp_depth_exceeding_height_fails():
    data = _valid_dict()
    data['types']['banane']['grasp_depth_m'] = 0.050  # > height 0.040
    with pytest.raises(ObjectCatalogError, match='Greiftiefe'):
        parse_catalog(data)


def test_invalid_type_key_fails():
    data = _valid_dict()
    data['types']['blauer würfel'] = data['types'].pop('wuerfel_blau')  # space + umlaut
    with pytest.raises(ObjectCatalogError, match='Schlüssel'):
        parse_catalog(data)


def test_invalid_tag_size_fails():
    data = _valid_dict()
    data['tag_size_m'] = -1
    with pytest.raises(ObjectCatalogError, match='tag_size_m'):
        parse_catalog(data)


def test_unknown_type_lookup_fails():
    cat = parse_catalog(_valid_dict())
    with pytest.raises(ObjectCatalogError, match='Unbekanntes Objekt'):
        cat.recipe_for_type('giraffe')


# ── the fixed, fleet-wide object set (baked into the image) ──────────────────
def test_fixed_catalog_is_valid_and_has_wuerfel(monkeypatch):
    # Every machine and user gets the SAME hardcoded set, with the rig-validated
    # „Würfel" numbers (2026-06-27). Tag size defaults to 0.024 with no env set.
    monkeypatch.delenv('EDUBOTICS_TAG_SIZE_M', raising=False)
    cat = fixed_catalog()
    assert cat.type_names() == ['wuerfel']
    assert cat.labels() == [('wuerfel', 'Würfel')]
    assert cat.tag_size_m == pytest.approx(0.024)
    r = cat.recipe_for_type('wuerfel')
    assert r.tag_ids == (20, 21)
    assert r.object_height_m == pytest.approx(0.030)
    assert r.grasp_depth_m == pytest.approx(0.015)
    assert r.gripper_close_rad == pytest.approx(-0.5)
    assert r.approach_clear_m == pytest.approx(0.06)


def test_fixed_catalog_tag_size_env_tunable(monkeypatch):
    # The fixed set omits tag_size_m on purpose, so EDUBOTICS_TAG_SIZE_M is the
    # live source — a rig printing a different tag size is corrected via env, no
    # image rebuild.
    monkeypatch.setenv('EDUBOTICS_TAG_SIZE_M', '0.030')
    assert fixed_catalog().tag_size_m == pytest.approx(0.030)


def test_fixed_catalog_reread_each_call(monkeypatch):
    # Re-read per call so an env change applies on the next workflow start
    # without a restart.
    monkeypatch.setenv('EDUBOTICS_TAG_SIZE_M', '0.020')
    assert fixed_catalog().tag_size_m == pytest.approx(0.020)
    monkeypatch.setenv('EDUBOTICS_TAG_SIZE_M', '0.024')
    assert fixed_catalog().tag_size_m == pytest.approx(0.024)


def test_fixed_catalog_is_a_fresh_instance_each_call():
    # Stateless: no shared mutable global to corrupt between workflow starts.
    assert fixed_catalog() is not fixed_catalog()
