#!/usr/bin/env python3
#
# Copyright 2026 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""M1 end to end: a hat and the main stack call ONE „Funktion" at the same time.

``fahre(ziel)`` waits 0.3 s, logs its argument and drives there. The main stack
sends the event and calls ``fahre("Start")``; the hat it wakes calls
``fahre("Ablage")``. Both calls overlap inside the wait. On 47aea6fa the
parameter was ONE shared variable: the log read „Ablage, MAIN-ENDE", the hat's
own call failed with „Block hat kein Ziel erhalten." (main's restore had popped
its binding) and the arm never reached „Start". Each call now has its own frame.

The simulated arm on ``rig_emulation.Rig`` (``omx_full``, real-time pacing, so
the two bodies really interleave).
"""

from __future__ import annotations

from rig_emulation import ABLAGE, B, Rig, chain, num, program, text, var, vget

START = {'name': 'Start', 'kind': 'pin', 'x': 0.18, 'y': -0.10, 'z': 0.0}


def _fahre():
    return {'type': 'procedures_defnoreturn', 'id': 'pd', 'fields': {'NAME': 'fahre'},
            'extraState': {'params': [{'name': 'ziel', 'id': 'v_ziel'}]},
            'inputs': {'STACK': {'block': chain(
                B('edubotics_wait_seconds', inputs={'SECONDS': num(0.3)}),
                B('edubotics_log', inputs={'MESSAGE': vget('ziel')}),
                B('edubotics_move_to', inputs={'DESTINATION': vget('ziel')}))}}}


def _call(value, cid):
    return {'type': 'procedures_callnoreturn', 'id': cid,
            'extraState': {'name': 'fahre', 'params': ['ziel']},
            'inputs': {'ARG0': {'block': text(value)}}}


def _reached(trace, dest, tol=0.005):
    return any(abs(p[0][0] - dest['x']) < tol and abs(p[0][1] - dest['y']) < tol
               for p in trace)


def test_a_hat_and_the_main_stack_each_drive_to_their_own_argument(monkeypatch):
    main = chain(B('edubotics_broadcast', fields={'EVENT_NAME': 'x'}),
                 _call('Start', 'cm'),
                 B('edubotics_log', inputs={'MESSAGE': text('MAIN-ENDE')}))
    hat = B('edubotics_when_broadcast', fields={'EVENT_NAME': 'x'}, nxt=_call('Ablage', 'ch'))
    rig = Rig(monkeypatch, 'omx_full', simulated=True, real_time=True)
    trace = rig.trace_waypoints()
    rig.run(program(main, hat, _fahre(), variables=[var('ziel')],
                    destinations=[ABLAGE, START]),
            timeout=30, stop_after=8)
    student_lines = [line for line in rig.logs() if line in ('Start', 'Ablage')]
    assert sorted(student_lines) == ['Ablage', 'Start'], rig.logs()
    assert not any('kein Ziel' in line for line in rig.logs()), rig.logs()
    assert not rig.errors(), rig.errors()
    assert _reached(trace, START), 'the arm never reached „Start"'
    assert _reached(trace, ABLAGE), 'the arm never reached „Ablage"'
