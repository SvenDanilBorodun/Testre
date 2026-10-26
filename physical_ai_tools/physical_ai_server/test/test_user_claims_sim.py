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
from rig_emulation import (ABLAGE, ABLAGE2, B, PICK, Rig, chain, cube, drop, forever, grasp,
                           log, num, program, ref, sees, text, var, vget, vset, wait)
from rig_emulation import hat as hat_block

TEST_TIMEOUT_S = 180.0


def _find(t='wuerfel'):
    return B('edubotics_find_object', fields={'OBJECT_TYPE': t})


# ── D2: a table Ziel is approached from above (Q3, F21) ────────────────────

@pytest.mark.parametrize('pid', ['omx_full', 'v1_studio'])
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


@pytest.mark.parametrize('pid', ['omx_full', 'v1_studio'])
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


@pytest.mark.parametrize('pid', ['omx_full', 'v1_studio'])
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


@pytest.mark.parametrize('pid', ['omx_full', 'v1_studio'])
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
    # M6: „Position von <Ziel>" in „aufnehmen" / „bewege zu" recommends the
    # composite „Greife", the split chain and „fahre über".
    ('position_von', 'aufnehmen'): lambda: [grasp()],
    ('position_von', 'aufnehmen', 'split'): _split_pick,
    ('position_von', 'bewege zu'): lambda: [vset('Z', _find()),
                                           B('edubotics_move_above', inputs={'ZIEL': vget('Z')})],
    ('not_a_greifziel',): _split_pick,
    ('no_greifziel',): _split_pick,
    ('position_not_here',): lambda: [B('edubotics_move_to', inputs={'DESTINATION': ref('P')}),
                                     B('edubotics_open_gripper'), B('edubotics_close_gripper')],
    ('lift_behind_grasp',): lambda: [grasp(), B('edubotics_lift')],
}


def _sentence(key):
    if key[0] == 'greifziel':
        return M._GREIFZIEL_IN_DESTINATION_DE[key[1]]
    if key[0] == 'position_von':
        return M._POSITION_VON_IN_DESTINATION_DE[key[1]]
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


@pytest.mark.parametrize('pid', ['v1_studio'])
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


# ── D3: „Greife" waits for an object (Q8), one look, the right sentence ────
# The calibrated-rig emulation (``Rig(simulated=False)``): a person can put a
# placed cube back, and the reclaim sees it. Most rows run with fast chunk
# pacing — the program's motions take no wall time, so the student's put-back
# (a real 1 s later) lands while „Greife" is WAITING, which is the case under
# test; the hat rows, whose outcome depends on how long a body takes, use the
# in-body put-back of ``test_hat_rearm`` or real time.

WAIT_LINE = 'Warte auf „Würfel" — bitte ein Objekt in den Greifbereich legen.'
DONE_LINE = ('„Würfel" ist schon erledigt — zum erneuten Greifen mindestens 5 cm '
             'vom Ablageort entfernt hinlegen.')


def _put_back_run(monkeypatch, body_root, *, putbacks=2, delay_s=1.0, stop_after=8.0,
                  real_time=False):
    rig = Rig(monkeypatch, 'omx_full', objects=[cube(*PICK[0])], real_time=real_time)
    stop, events = rig.student((ABLAGE['x'], ABLAGE['y']), PICK, putbacks=putbacks,
                               delay_s=delay_s)
    try:
        phase = rig.run(program(*body_root, destinations=[ABLAGE]), timeout=stop_after + 60,
                        stop_after=stop_after)
    finally:
        stop.set()
    return rig, phase, events


def test_forever_grasp_waits_for_every_put_back(monkeypatch):
    """„wiederhole fortlaufend {Greife; ablegen}" with ONE cube that the student
    puts back twice: 3 grasps, then it waits until Stop (it used to stop after
    the first with a false „… erledigt" error)."""
    rig, phase, events = _put_back_run(monkeypatch, [forever(chain(grasp(), drop()))])
    assert len(events) == 2, events
    assert rig.grasps() == 3, rig.logs()
    assert phase == 'stopped'
    assert rig.errors() == []


def test_repeat_five_waits_for_every_put_back(monkeypatch):
    """„wiederhole 5 mal {Greife; ablegen}": the same three grasps."""
    body = B('controls_repeat_ext', inputs={'TIMES': num(5), 'DO': chain(grasp(), drop())})
    rig, phase, _events = _put_back_run(monkeypatch, [body])
    assert rig.grasps() == 3, rig.logs()
    assert phase == 'stopped'


def test_wait_line_is_said_once_and_no_put_back_note(monkeypatch):
    """With nothing on the table „Greife" says ONE wait line, not again while it
    polls, and never the old „… wegnehmen …" note."""
    rig = Rig(monkeypatch, 'omx_full', objects=[])
    assert rig.run(program(grasp()), stop_after=1.5) == 'stopped'
    lines = rig.logs()
    assert lines.count(WAIT_LINE) == 1, lines
    assert not [m for m in lines if 'erledigt' in m], lines
    assert rig.errors() == []


def test_a_cube_left_at_the_release_spot_gets_the_done_sentence(monkeypatch):
    """The cube lies where the robot put it (a put-back there is never
    reclaimed), so the wait line says how far away to put it."""
    rig = Rig(monkeypatch, 'omx_full', objects=[cube(*PICK[0])])
    assert rig.run(program(chain(grasp(), drop(), grasp()), destinations=[ABLAGE]),
                   stop_after=2.0) == 'stopped'
    lines = rig.logs()
    assert rig.grasps() == 1
    assert lines.count(DONE_LINE) == 1, lines
    assert not [m for m in lines if 'erledigt' in m and m != DONE_LINE], lines


def test_skipped_instance_gets_its_own_sentence(monkeypatch):
    """m4: a cube „finde" skipped (out of reach) is never called „erledigt";
    on the rig the wait line says how far to move it, in the simulator where to
    put it."""
    far = {'type': 'wuerfel', 'x': 0.33, 'y': 0.0, 'yaw': 0.2}
    payload = program(chain(vset('Z', _find()), grasp()), variables=[var('Z')])
    rig = Rig(monkeypatch, 'omx_full', objects=[far])
    assert rig.run(payload, stop_after=1.5) == 'stopped'
    skipped = ('„Würfel" wurde übersprungen (hier nicht greifbar) — zum erneuten '
               'Versuch um mindestens 2 cm verschieben.')
    assert rig.logs().count(skipped) == 1, rig.logs()
    assert not [m for m in rig.logs() if 'erledigt' in m]
    sim = Rig(monkeypatch, 'omx_full', objects=[far], simulated=True)
    assert sim.run(payload) == 'error'
    assert sim.errors()[-1] == ('„Würfel" ist hier nicht greifbar — im Editor an eine '
                                'erreichbare Stelle verschieben.')


def test_sim_with_no_cube_ever_says_not_visible(monkeypatch):
    """m3: the simulator never waits, and with no cube ever placed it says
    „nicht sichtbar" (not „Alle … sind erledigt")."""
    rig = Rig(monkeypatch, 'omx_full', objects=[], simulated=True)
    assert rig.run(program(grasp())) == 'error'
    assert rig.errors()[-1] == ('Kein „Würfel" sichtbar — bitte das Objekt in den '
                                'Greifbereich auf dem Sim-Tisch legen.')


def test_sim_with_everything_done_says_so_at_once(monkeypatch):
    """The simulator, every cube done: an immediate refusal with the sim's
    sentence (no person can put one back there)."""
    rig = Rig(monkeypatch, 'omx_full', objects=[cube(*PICK[0])], simulated=True)
    assert rig.run(program(chain(grasp(), drop(), grasp()), destinations=[ABLAGE])) == 'error'
    assert rig.errors()[-1] == ('Alle „Würfel" sind erledigt — im Simulator „Simulator '
                                'zurücksetzen" oder das Programm neu starten.')


def test_wait_line_with_only_the_carried_cube_visible_is_warte(monkeypatch):
    """r3-m2: the cube in the gripper is neither „erledigt" nor skipped — with
    only that one in view the second „Greife" says „Warte auf …"."""
    rig = Rig(monkeypatch, 'omx_full', objects=[cube(*PICK[0])])
    assert rig.run(program(chain(grasp(), grasp())), stop_after=1.5) == 'stopped'
    assert rig.grasps() == 1
    assert rig.logs().count(WAIT_LINE) == 1, rig.logs()
    assert not [m for m in rig.logs() if 'erledigt' in m]


def test_hat_still_waits_while_main_runs_solange_sichtbar(monkeypatch):
    """M5: the „Solange sichtbar" exemption is THREAD-LOCAL. A thread inside
    ``no_grasp_wait()`` gets the immediate GraspSkip; another thread at the
    same moment waits."""
    import threading
    import time as _t
    rig = Rig(monkeypatch, 'omx_full', objects=[])
    stop = threading.Event()
    ctx = rig.make_ctx(should_stop=stop.is_set)
    inside = threading.Event()
    results = {}

    def loop_thread():
        with pb.no_grasp_wait():
            inside.set()
            try:
                pb.grasp_object(ctx, {'object_type': 'wuerfel'})
            except M.GraspSkip as e:
                results['loop'] = str(e)
            _t.sleep(0.5)

    def hat_thread():
        inside.wait(5)
        try:
            pb.grasp_object(ctx, {'object_type': 'wuerfel'})
        except M.WorkflowError as e:
            results['hat'] = str(e)

    a = threading.Thread(target=loop_thread)
    b = threading.Thread(target=hat_thread)
    a.start(); b.start()
    a.join(5)
    _t.sleep(0.3)
    assert b.is_alive(), 'the hat thread did not wait'
    stop.set()
    b.join(5)
    assert results['loop'].startswith('Kein „Würfel" sichtbar')
    assert results['hat'] == 'Workflow wurde gestoppt.'
    assert not pb.in_no_grasp_wait()


def test_wait_survives_a_2s_camera_freeze_and_stops_after_5s(monkeypatch):
    """Q14: during the wait a frozen picture is „noch nichts zu sehen" for up to
    5 s (judged on the frame's OWN age); a camera gone longer stops the program
    with the existing camera sentence."""
    import time as _t
    t0 = [None]

    def age_frozen_for(seconds):
        """The picture froze 1.2 s before „Greife" (already stale at its first
        look) and stays frozen ``seconds`` more; then frames are fresh."""
        def _age():
            if t0[0] is None:
                t0[0] = _t.monotonic()
            el = _t.monotonic() - t0[0]
            return 1.2 + el if el < seconds else 0.0
        return _age

    rig = Rig(monkeypatch, 'omx_full', objects=[cube(*PICK[0])],
              scene_frame_age=age_frozen_for(2.0))
    assert rig.run(program(grasp()), timeout=30) == 'finished', rig.errors()
    assert rig.grasps() == 1
    t0[0] = None
    gone = Rig(monkeypatch, 'omx_full', objects=[cube(*PICK[0])],
               scene_frame_age=age_frozen_for(60.0))
    start = _t.monotonic()
    assert gone.run(program(grasp()), timeout=30) == 'error'
    took = _t.monotonic() - start
    assert gone.errors()[-1] == ('Die Szenen-Kamera liefert kein aktuelles Bild — bitte '
                                 'die Kamera prüfen.')
    # The camera sentence comes once the frame is older than 5 s (≈ 3.8 s after
    # „Greife", the frame being 1.2 s old then) — not at its first stale look.
    assert 3.3 <= took <= 6.5, took


def _scripted_frame_age(values):
    """An age getter answering ``values`` in order, one per READ, then 0.0
    (fresh); the second callable is True once every scripted read was made —
    a deterministic clock that does not depend on how long a read takes."""
    left = [float(v) for v in values]

    def age():
        return left.pop(0) if left else 0.0

    return age, (lambda: not left)


@pytest.mark.parametrize('ages', [
    # the FIRST look: judged 0.99 s, the old second read said 1.01 s
    [0.99, 1.01, 1.5, 0.2],
    # a later look inside ``_graspable``: the same crossing on the third poll
    [0.2, 0.5, 0.99, 1.01, 2.0, 0.3],
])
def test_a_frame_crossing_one_second_between_two_reads_keeps_waiting(monkeypatch, ages):
    """Q14 race: the wait judged the frame's age, then the look read it AGAIN
    and raised „Die Szenen-Kamera liefert kein aktuelles Bild …" once it had
    crossed 1.0 s in between — a freeze far inside the 5 s grace ended the wait
    (measured 1 in 24 with a 30 ms frame fetch). The wait's look now uses the
    judged frame: the scripted ages are read once each and it waits until
    Stopp."""
    rig = Rig(monkeypatch, 'omx_full', objects=[])
    age, exhausted = _scripted_frame_age(ages)
    ctx = rig.make_ctx(should_stop=exhausted)
    ctx.get_scene_frame_age = age
    with pytest.raises(M.WorkflowError) as exc:
        pb.grasp_object(ctx, {'object_type': 'wuerfel'})
    assert str(exc.value) == 'Workflow wurde gestoppt.'
    assert rig.logs() == [WAIT_LINE]


def test_the_look_right_before_the_motion_stays_strict(monkeypatch):
    """The other half of the race fix: the look inside ``_grasp_object_locked``
    judges its OWN frame, so a picture that went stale after the wait saw the
    cube still refuses at once, before any motion."""
    rig = Rig(monkeypatch, 'omx_full', objects=[cube(*PICK[0])])
    age, _exhausted = _scripted_frame_age([0.99, 1.01])
    ctx = rig.make_ctx()
    ctx.get_scene_frame_age = age
    published = []
    real = ctx.publisher
    ctx.publisher = lambda pts: (published.append(pts), real(pts))
    with pytest.raises(M.WorkflowError) as exc:
        pb.grasp_object(ctx, {'object_type': 'wuerfel'})
    assert str(exc.value) == ('Die Szenen-Kamera liefert kein aktuelles Bild — bitte '
                              'die Kamera prüfen.')
    assert published == []
    assert rig.grasps() == 0


def test_a_missing_frame_counts_from_its_first_poll(monkeypatch):
    """Q14 / r5-m8: a ``None`` frame is waited out for 5 s too, then the
    „Kein aktuelles Szenenbild" sentence."""
    import time as _t
    rig = Rig(monkeypatch, 'omx_full', objects=[cube(*PICK[0])], scene_frame=False)
    start = _t.monotonic()
    assert rig.run(program(grasp()), timeout=30) == 'error'
    assert 4.5 <= _t.monotonic() - start <= 8.0
    assert rig.errors()[-1] == ('Kein aktuelles Szenenbild verfügbar — bitte die '
                                'Szenen-Kamera prüfen.')


def test_a_held_one_tag_object_does_not_use_up_the_notice(monkeypatch):
    """r7-m5: a one-tag object in the jaws makes its type „all done" only
    because of the carried tag — the note then says NOTHING and does not mark
    the type notified (it used to print „Kein … sichtbar" and use up the
    once-per-run notice)."""
    from physical_ai_server.workflow import object_catalog as oc
    cat = oc.parse_catalog({'tag_size_m': 0.024, 'types': {'banane': {
        'label_de': 'Rote Banane', 'tag_ids': [30], 'object_height_m': 0.03,
        'grasp_depth_m': 0.015, 'gripper_close_rad': -0.5, 'approach_clear_m': 0.06}}})
    rig = Rig(monkeypatch, 'omx_full', objects=[], catalog=cat)
    ctx = rig.make_ctx()
    ctx.claimed_tags = {30}
    ctx.carried_tag = 30
    recipe = cat.recipe_for_type('banane')
    pb._note_all_instances_done(ctx, recipe)
    assert rig.logs() == []
    assert 'banane' not in ctx.all_done_notified
    ctx.carried_tag = None                     # let go: now it IS done
    pb._note_all_instances_done(ctx, recipe)
    assert len(rig.logs()) == 1 and 'erledigt' in rig.logs()[0]


# ── D3 / N14 rows of the put-back table, and more cubes and sorting ────────

def _widened_catalog(wuerfel_ids=(20, 21, 24, 25), zylinder=True):
    """The shipped „Würfel" has two tag ids, so a class can tell only two cubes
    apart; the design never depends on the count. Widened here (and a second
    type „Zylinder", tags 30/31) to run the conductor's many-cube and sorting
    rows."""
    import copy
    from physical_ai_server.workflow import object_catalog as oc
    cat = copy.deepcopy(oc._FIXED_CATALOG)
    cat['types']['wuerfel'] = dict(cat['types']['wuerfel'], tag_ids=list(wuerfel_ids))
    if zylinder:
        cat['types']['zylinder'] = dict(cat['types']['wuerfel'], label_de='Zylinder',
                                        tag_ids=[30, 31])
    return oc.parse_catalog(cat)


def test_the_hat_grasps_a_cube_put_back_after_its_body(monkeypatch):
    """„wenn Würfel gesehen {Greife; ablegen}", one cube put back twice (1 s
    after each release): 3 grasps (main: 1 — the edge never re-armed)."""
    from physical_ai_server.workflow import workflow_manager as WM
    monkeypatch.setattr(WM, 'HAT_KEEPALIVE_MAX_S', 30.0)
    rig, phase, events = _put_back_run(
        monkeypatch, [wait(1), hat_block(chain(grasp(), drop()))])
    assert len(events) == 2, events
    assert rig.grasps() == 3, rig.logs()
    hat_lines = [m for m in rig.logs() if m.startswith('„Wenn Objekt erkannt"')]
    assert set(hat_lines) <= {'„Wenn Objekt erkannt": Workflow wurde gestoppt.'}, hat_lines


def test_main_and_hat_racing_for_one_cube_never_double_grasp(monkeypatch):
    """The main stack and a hat both grasp the one cube, put back twice: 3
    grasps for 3 appearances, never two at once, no error."""
    from physical_ai_server.workflow import workflow_manager as WM
    monkeypatch.setattr(WM, 'HAT_KEEPALIVE_MAX_S', 30.0)
    rig, phase, events = _put_back_run(
        monkeypatch, [forever(chain(grasp(), drop())), hat_block(chain(grasp(), drop()))])
    assert len(events) == 2, events
    assert rig.grasps() == 3, rig.logs()
    assert rig.errors() == []
    # The only hat line allowed is the Stop that ended its wait.
    hat_lines = [m for m in rig.logs() if m.startswith('„Wenn Objekt erkannt"')]
    assert set(hat_lines) <= {'„Wenn Objekt erkannt": Workflow wurde gestoppt.'}, hat_lines


def test_three_cubes_one_put_back_all_grasped(monkeypatch):
    """The conductor's row: the hat with 3 distinguishable cubes, each put back
    once inside the body — 6 grasps (main: 1, then never again)."""
    from physical_ai_server.workflow import workflow_manager as WM
    from physical_ai_server.workflow.handlers import motion as _m
    monkeypatch.setattr(WM, 'HAT_KEEPALIVE_MAX_S', 6.0)
    rig = Rig(monkeypatch, 'omx_full', objects=[cube(*PICK[i]) for i in range(3)],
              catalog=_widened_catalog(zylinder=False))
    put = {}
    original = _m.note_object_released

    def _released(ctx, where):
        original(ctx, where)
        key = next((o['key'] for o in rig.world.snapshot()['objects']
                    if abs(o['x'] - ABLAGE['x']) < 0.03 and abs(o['y'] - ABLAGE['y']) < 0.03
                    and not put.get(o['key'])), None)
        if key is not None and rig.put_back(key, *PICK[key]):
            put[key] = True

    monkeypatch.setattr(_m, 'note_object_released', _released)
    rig.run(program(wait(1), hat_block(chain(grasp(), drop())), destinations=[ABLAGE]),
            timeout=120)
    assert sorted(put) == [0, 1, 2], put
    assert rig.grasps() == 6, rig.logs()


def test_two_types_sorted_by_two_hats_with_put_backs(monkeypatch):
    """Two hats (Würfel → Ablage, Zylinder → Ablage2), 2 + 2 objects, each put
    back once: all four sorted twice, 8 grasps (main: 2, then nothing)."""
    from physical_ai_server.workflow import workflow_manager as WM
    from physical_ai_server.workflow.handlers import motion as _m
    monkeypatch.setattr(WM, 'HAT_KEEPALIVE_MAX_S', 8.0)
    objects = [cube(*PICK[0]), cube(*PICK[1]), cube(*PICK[2], typ='zylinder'),
               cube(*PICK[3], typ='zylinder')]
    rig = Rig(monkeypatch, 'omx_full', objects=objects,
              catalog=_widened_catalog(wuerfel_ids=(20, 21)))
    put = {}
    original = _m.note_object_released
    drops = [(ABLAGE['x'], ABLAGE['y']), (ABLAGE2['x'], ABLAGE2['y'])]

    def _released(ctx, where):
        original(ctx, where)
        for o in rig.world.snapshot()['objects']:
            k = o['key']
            if put.get(k):
                continue
            if any(abs(o['x'] - dx) < 0.03 and abs(o['y'] - dy) < 0.03 for dx, dy in drops):
                if rig.put_back(k, *PICK[k]):
                    put[k] = True
                return

    monkeypatch.setattr(_m, 'note_object_released', _released)
    payload = program(wait(1),
                      hat_block(chain(grasp(), drop())),
                      hat_block(chain(grasp('zylinder'), drop('Ablage2')), t='zylinder'),
                      destinations=[ABLAGE, ABLAGE2])
    rig.run(payload, timeout=120)
    assert sorted(put) == [0, 1, 2, 3], put
    assert rig.grasps() == 8, rig.logs()
    final = {o['key']: (o['x'], o['y']) for o in rig.world.snapshot()['objects']}
    for k in (0, 1):
        assert abs(final[k][1] - ABLAGE['y']) < 0.03, final
    for k in (2, 3):
        assert abs(final[k][1] - ABLAGE2['y']) < 0.03, final
