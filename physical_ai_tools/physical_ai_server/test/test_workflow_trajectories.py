"""Batch 2b — CONTRACT C: the ``trajectories`` sibling of the workflow_json +
the WorkflowManager persistence API. Uses the REAL WorkflowManager class
(constructed via ``__new__`` so no ROS deps are touched)."""

from __future__ import annotations

import json

import pytest

from physical_ai_server.workflow.workflow_manager import (
    WorkflowContext,
    WorkflowManager,
)


def _wfm():
    wfm = WorkflowManager.__new__(WorkflowManager)
    wfm._persisted_trajectories = {}
    return wfm


# --- _parse_trajectories (CONTRACT C) --------------------------------------

def test_parse_trajectories_reads_top_level_sibling():
    payload = json.dumps({
        'blocks': [],
        'trajectories': {'Winken': {'fps': 25, 'points': [[0, 0, 0, 0, 0, 0, 0.0]]}},
    })
    out = WorkflowManager._parse_trajectories(payload)
    assert set(out) == {'Winken'}
    assert out['Winken']['fps'] == 25


def test_parse_trajectories_defensive():
    # missing / non-dict / malformed → {}; malformed ENTRIES dropped.
    assert WorkflowManager._parse_trajectories('{}') == {}
    assert WorkflowManager._parse_trajectories('not json') == {}
    assert WorkflowManager._parse_trajectories(json.dumps({'trajectories': 5})) == {}
    assert WorkflowManager._parse_trajectories(json.dumps([1, 2])) == {}
    # A non-dict VALUE ('bad': 3) is dropped; a dict-valued entry is kept.
    mixed = json.dumps({'trajectories': {'good': {'fps': 25, 'points': []},
                                         'bad': 3}})
    assert WorkflowManager._parse_trajectories(mixed) == {'good': {'fps': 25, 'points': []}}


# --- set_trajectory / get_trajectories -------------------------------------

def test_set_get_trajectory_roundtrip():
    wfm = _wfm()
    traj = {'fps': 25, 'points': [[0, 0, 0, 0, 0, 0, 0.0], [0.1, 0, 0, 0, 0, 0, 0.4]]}
    wfm.set_trajectory('Bahn', traj)
    assert wfm.get_trajectories() == {'Bahn': traj}
    # get returns a copy — mutating it doesn't corrupt the store.
    wfm.get_trajectories()['Bahn'] = None
    assert wfm.get_trajectories()['Bahn'] == traj


def test_set_trajectory_ignores_bad_input():
    wfm = _wfm()
    wfm.set_trajectory('', {'fps': 25})       # empty name
    wfm.set_trajectory('X', 'not a dict')     # non-dict trajectory
    assert wfm.get_trajectories() == {}


# --- WorkflowContext default -----------------------------------------------

def test_context_trajectories_defaults_empty():
    ctx = WorkflowContext(publisher=lambda _p: None)
    assert ctx.trajectories == {}
    # Independent instances (default_factory), not a shared mutable default.
    ctx2 = WorkflowContext(publisher=lambda _p: None)
    ctx.trajectories['x'] = 1
    assert ctx2.trajectories == {}


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-q']))
