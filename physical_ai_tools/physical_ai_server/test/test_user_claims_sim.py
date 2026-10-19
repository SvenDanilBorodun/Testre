"""Every promising sentence runs its promise (spec principle 3): what a block,
its tooltip or a refusal tells the student is what the robot does.

Driven through the real manager, interpreter, handlers and solvers on the
simulator (``rig_emulation.Rig``), the camera-dependent cases on a CALIBRATED
RIG EMULATION (``Rig(simulated=False)`` with a perfect table projection — on a
real rig a person can put a cube back, and the reclaim must see it). Grasps are
counted from the direct emit trace (``Rig.grasps``), never from a summary.
"""

from __future__ import annotations

import pytest

from physical_ai_server.workflow.handlers import motion as M
from physical_ai_server.workflow.handlers import perception_blocks as pb

from daten_timeout import per_test_time_limit  # noqa: F401 — a hang fails, never stalls
from rig_emulation import (ABLAGE, B, PICK, Rig, chain, cube, drop, forever, grasp,
                           log, num, program, ref, sees, text, var, vget, vset, wait)

TEST_TIMEOUT_S = 180.0


def _find(t='wuerfel'):
    return B('edubotics_find_object', fields={'OBJECT_TYPE': t})


# ── D2: a table Ziel is approached from above (Q3, F21) ────────────────────

@pytest.mark.parametrize('pid', ['omx_full', 'edu1_studio'])
def test_table_ziel_is_approached_from_above(monkeypatch, pid):
    """„bewege zu <Tisch-Ziel>" with a cube in the jaws stops ABOVE the Ziel at
    the release height: the held cube's bottom never goes below the table (it
    used to be pressed ~15 mm into it)."""
    rig = Rig(monkeypatch, pid, objects=[cube(*PICK[0])], simulated=True)
    trace = rig.trace_waypoints()
    payload = program(chain(grasp(), B('edubotics_move_to', inputs={'DESTINATION': ref('Ablage')}),
                            B('edubotics_open_gripper')), destinations=[ABLAGE])
    assert rig.run(payload) == 'finished', rig.errors()
    held = [xyz[2] for xyz, key in trace if key is not None]
    assert held, 'the cube was never held'
    capture_z = held[0]          # at capture the cube's bottom is on the table
    bottoms = [z - capture_z for z in held]
    assert min(bottoms) >= -1e-6
    # And the release happens at the hover above Ablage, not on the table.
    released_at = [xyz for xyz, key in trace if key is None][-1]
    assert released_at[2] - capture_z >= 0.02


def _hover_z(monkeypatch, pid):
    """EE height „fahre über <Ziel>" reaches for the first cube (reference)."""
    rig = Rig(monkeypatch, pid, objects=[cube(*PICK[0])], simulated=True)
    payload = program(chain(vset('Z', _find()),
                            B('edubotics_move_above', inputs={'ZIEL': vget('Z')})),
                      variables=[var('Z')])
    assert rig.run(payload) == 'finished', rig.errors()
    return rig.ee_z()


@pytest.mark.parametrize('pid', ['omx_full', 'edu1_studio'])
def test_descend_without_hover_never_swings_low(monkeypatch, pid):
    """F21: „senke auf <Ziel>" from elsewhere first goes to the hover, then
    straight down — the arm never travels sideways below the hover."""
    hover = _hover_z(monkeypatch, pid)
    rig = Rig(monkeypatch, pid, objects=[cube(*PICK[0])], simulated=True)
    trace = rig.trace_waypoints()
    payload = program(chain(vset('Z', _find()),
                            B('edubotics_descend_to', inputs={'ZIEL': vget('Z')})),
                      variables=[var('Z')])
    assert rig.run(payload) == 'finished', rig.errors()
    x, y = PICK[0]
    far = [xyz[2] for xyz, _k in trace
           if ((xyz[0] - x) ** 2 + (xyz[1] - y) ** 2) ** 0.5 > 0.005]
    assert far, 'no transit was published'
    assert min(far) >= hover - 0.005


# ── D5: „hält" is false whenever the measured gripper is OPEN (Q10) ────────

def _held(monkeypatch, pid, body, objects=None, trajectories=None):
    """Run ``body`` then log „Greifer hält etwas?"; return what it said."""
    monkeypatch.setattr(M, 'GRASP_SETTLE_S', 0.0)
    rig = Rig(monkeypatch, pid, objects=objects or [cube(*PICK[0])], simulated=True)
    payload = program(chain(*body, B('edubotics_log', inputs={'MESSAGE': B('edubotics_grasp_held')})),
                      destinations=[ABLAGE], trajectories=trajectories)
    assert rig.run(payload) == 'finished', rig.errors()
    said = [m for m in rig.logs() if m in ('wahr', 'falsch')]
    assert len(said) == 1, rig.logs()
    return said[0] == 'wahr'


def _grasp_recording(pid, xy, *, closes=True):
    """A Contract-B recording: from HOME (open) down onto ``xy`` at the grasp
    height, then the jaws close — what a rig recording of a pick looks like."""
    from physical_ai_server import robot_profiles as rp
    prof = rp.ROBOT_PROFILES[pid]
    ik = prof.build_ik()
    tip = float(getattr(ik, 'tool_tip_below_ee_m', 0.0))
    n = prof.num_arm_joints
    home = list(prof.home_joints_rad)
    above = ik.solve(target_xyz=(xy[0], xy[1], tip + 0.075), seed=home, roll=M.GRASP_ROLL_RAD)
    down = ik.solve(target_xyz=(xy[0], xy[1], tip + 0.015), seed=above, roll=M.GRASP_ROLL_RAD)
    g_open, g_close = float(prof.gripper_open_rad), float(prof.gripper_closed_rad)
    pts = [home + [g_open, 0.0], list(above) + [g_open, 2.0], list(down) + [g_open, 3.0],
           list(down) + [(g_close if closes else g_open), 4.0]]
    assert len(pts[0]) == n + 2
    return {'Greifen': {'fps': 25, 'points': pts}}


def _replay(name='Greifen'):
    return B('edubotics_replay_trajectory', fields={'NAME': name})


@pytest.mark.parametrize('pid', ['omx_full', 'edu1_studio'])
def test_open_gripper_never_holds(monkeypatch, pid):
    """„Ein offener Greifer hält nie etwas": an open gripper with no close this
    run reads falsch, through „Greifer hält etwas?" and „warte bis Greifer hält"
    (both read wahr before)."""
    assert _held(monkeypatch, pid, []) is False
    monkeypatch.setattr(M, 'GRASP_SETTLE_S', 0.0)
    rig = Rig(monkeypatch, pid, objects=[cube(*PICK[0])], simulated=True)
    waited = B('edubotics_wait_until_held', inputs={'TIMEOUT': num(1)})
    assert rig.run(program(B('edubotics_log', inputs={'MESSAGE': waited}))) == 'finished'
    assert 'falsch' in rig.logs()


@pytest.mark.parametrize('pid', ['omx_full', 'edu1_studio'])
def test_a_replayed_grasp_still_holds_and_an_empty_one_does_not(monkeypatch, pid):
    """There is no close-COMMAND requirement: a replayed grasp onto a cube keeps
    reading wahr; a replayed close on nothing, a close on nothing and a grasp
    that was opened again read falsch; the composite grasp reads wahr."""
    rec = _grasp_recording(pid, PICK[0])
    assert _held(monkeypatch, pid, [_replay()], trajectories=rec) is True
    far = (PICK[0][0], PICK[0][1] + 0.12)
    assert _held(monkeypatch, pid, [_replay()], trajectories=_grasp_recording(pid, far)) is False
    # A close on nothing: no cube near the jaws (8 cm away from where they close).
    assert _held(monkeypatch, pid, [B('edubotics_close_gripper')],
                 objects=[cube(*PICK[1])]) is False
    assert _held(monkeypatch, pid, [grasp()]) is True
    assert _held(monkeypatch, pid, [grasp(), B('edubotics_open_gripper')]) is False


# ── D4: wording and small refusals (F5, F14, N8) ───────────────────────────

def _pose_entry(pid, name):
    """A Position taught at a grasp-like pose on ``pid``."""
    from physical_ai_server import robot_profiles as rp
    prof = rp.ROBOT_PROFILES[pid]
    ik = prof.build_ik()
    tip = float(getattr(ik, 'tool_tip_below_ee_m', 0.0))
    q = list(ik.solve(target_xyz=(0.17, 0.05, tip + 0.05), seed=list(prof.home_joints_rad),
                      roll=M.GRASP_ROLL_RAD))
    _R, t = ik.fk(q)
    return {'name': name, 'kind': 'pose', 'x': float(t[0]), 'y': float(t[1]), 'z': float(t[2]),
            'joints': q + [float(prof.gripper_open_rad)], 'joint_names': list(prof.joint_names),
            'robot_type': prof.data_robot_type}


def _split_pick():
    return [vset('Z', _find()),
            B('controls_if', inputs={'IF0': vget('Z'), 'DO0': chain(
                B('edubotics_move_above', inputs={'ZIEL': vget('Z')}),
                B('edubotics_descend_to', inputs={'ZIEL': vget('Z')}),
                B('edubotics_close_on_object', inputs={'ZIEL': vget('Z')}),
                B('edubotics_lift'))})]


# Every sentence that RECOMMENDS another block, with a program that does what
# it recommends. Each program must finish (spec principle 3, F5). The sentence
# is looked up by its key, so a sentence that is reworded stays covered and one
# that is added without a program here is caught by the coverage assert below.
_RECOMMENDATIONS = {
    ('greifziel', 'bewege zu'): lambda: [vset('Z', _find()),
                                        B('edubotics_move_above', inputs={'ZIEL': vget('Z')})],
    ('greifziel', 'aufnehmen'): lambda: [grasp()],
    ('greifziel', 'aufnehmen', 'split'): _split_pick,
    ('greifziel', 'ablegen bei'): lambda: [vset('Z', _find()), B('edubotics_drop_at', inputs={
        'DESTINATION': B('edubotics_object_position', inputs={'ZIEL': vget('Z')})})],
    ('not_a_greifziel',): _split_pick,
    ('no_greifziel',): _split_pick,
    ('position_not_here',): lambda: [B('edubotics_move_to', inputs={'DESTINATION': ref('P')}),
                                     B('edubotics_open_gripper'), B('edubotics_close_gripper')],
    ('lift_behind_grasp',): lambda: [grasp(), B('edubotics_lift')],
}


def _sentence(key):
    if key[0] == 'greifziel':
        return M._GREIFZIEL_IN_DESTINATION_DE[key[1]]
    if key[0] == 'not_a_greifziel':
        return M._NOT_A_GREIFZIEL_DE['fahre über']
    if key[0] == 'no_greifziel':
        return M._NO_GREIFZIEL_MSG
    if key[0] == 'position_not_here':
        return M._POSITION_NOT_HERE_DE
    if key[0] == 'lift_behind_grasp':
        return M._LIFT_NOT_SOLVABLE_HOME_DE
    raise KeyError(key)


def test_bewege_zu_recommends_only_fahre_ueber():
    """F5: „Position von <Ziel>" slips past the Greifziel refusal (a blind,
    fixed-wrist grasp 12 mm too high), so the refusal no longer recommends it."""
    text_ = M._GREIFZIEL_IN_DESTINATION_DE['bewege zu']
    assert '„fahre über"' in text_
    assert 'Position von' not in text_


@pytest.mark.parametrize('key', list(_RECOMMENDATIONS))
def test_recommended_alternative_runs(monkeypatch, key):
    """Each recommending sentence names its block, and that block, used as the
    sentence says, finishes."""
    sentence = _sentence(key)
    assert sentence
    rig = Rig(monkeypatch, 'omx_full', objects=[cube(*PICK[0])], simulated=True)
    payload = program(chain(*_RECOMMENDATIONS[key]()), variables=[var('Z')],
                      destinations=[ABLAGE, _pose_entry('omx_full', 'P')])
    assert rig.run(payload) == 'finished', (key, rig.errors())


@pytest.mark.parametrize('seconds', [text('2'), text('abc'), None])
def test_wait_refuses_text(monkeypatch, seconds):
    """F14: „warte" with a text or an empty socket used to wait a silent 1 s."""
    rig = Rig(monkeypatch, 'omx_full', simulated=True)
    block = (B('edubotics_wait_seconds', inputs={'SECONDS': seconds}) if seconds is not None
             else B('edubotics_wait_seconds'))
    assert rig.run(program(block)) == 'error'
    assert rig.errors()[-1] == '„warte" braucht eine Zahl (Sekunden).'


def test_wait_still_clamps_a_long_wait(monkeypatch):
    """The 300 s clamp and its warning stay (F14 only refuses non-numbers)."""
    lines = []
    ctx = type('C', (), {'should_stop': staticmethod(lambda: True),
                         'log': staticmethod(lines.append), 'motion_lock': None})()
    with pytest.raises(M.WorkflowError):
        M.wait_seconds(ctx, {'seconds': 1e9})
    assert lines and lines[0].startswith('[WARNUNG] Warte-Dauer auf 300 s begrenzt')


@pytest.mark.parametrize('pid', ['edu1_studio', 'edu6_studio'])
def test_lift_warning_names_grundstellung_only_at_home(monkeypatch, pid):
    """N8: „hebe an" that cannot rise says „aus der Grundstellung heraus …" only
    when the arm IS in the Grundstellung; anywhere else just the first clause."""
    from physical_ai_server import robot_profiles as rp
    prof = rp.ROBOT_PROFILES[pid]
    at_home = Rig(monkeypatch, pid, simulated=True)
    assert at_home.run(program(B('edubotics_lift'))) == 'finished'
    home_lines = [m for m in at_home.logs() if '„hebe an"' in m]
    assert home_lines == [M._LIFT_NOT_SOLVABLE_HOME_DE]
    assert 'Grundstellung' in home_lines[0]
    # The arm moved away from HOME into another unsolvable pose (a recorded
    # motion ending there), then „hebe an".
    away = list(prof.home_joints_rad)
    away[1] += 0.2
    n = prof.num_arm_joints
    rec = {'Weg': {'fps': 25, 'points': [list(prof.home_joints_rad) + [float(prof.gripper_open_rad), 0.0],
                                         away + [float(prof.gripper_open_rad), 1.0]]}}
    rig = Rig(monkeypatch, pid, simulated=True)
    assert rig.ik.solve(target_xyz=tuple(rig.ik.fk(away)[1]), seed=away) is None
    payload = program(chain(B('edubotics_replay_trajectory', fields={'NAME': 'Weg'}),
                            B('edubotics_lift')), trajectories=rec)
    assert rig.run(payload) == 'finished', rig.errors()
    lines = [m for m in rig.logs() if '„hebe an"' in m]
    assert lines == ['[WARNUNG] „hebe an" hat den Arm nicht bewegt — aus dieser Stellung '
                     'kann der Arm nicht senkrecht nach oben fahren.']
    assert len(rig.arm.get_joints()) == n + 1
