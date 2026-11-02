// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// The camera station on the Aufnahme page (station spec v6.13, §9.2, D13):
// while this tab holds the station claim the robot refuses a recording start,
// so Start is off with its own block kind `station`, the banner says why and
// offers „Station verlassen" (disabled while a real program runs). On a
// station rig the switched-off leader sentence ends with the way back. Manual
// rigs: every sentence and block exactly as before.

import TaskPhase from '../../../../constants/taskPhases';
import { VIEW, deriveRecordView, deriveStartBlock } from '../phaseModel';
import { deriveProblems, firstProblem, stalledSourceProblem, stationProblem } from '../problems';
import RECORD_COPY from '../recordCopy';

const READY_STATUS = { topicReceived: true, phase: TaskPhase.READY, running: false };
const FORM = { numEpisodes: 3, episodeTime: 10, warmupTime: 5, resetTime: 5 };
const LEADER_STALLED = [{ kind: 'leader', name: 'leader', verdict: 'stalled' }];
const FOLLOWER_ONLY_BRIDGE = { available: true, followerOnly: true };

describe('the station block kind', () => {
  it('is the first match, before the disk, a stalled source and the token', () => {
    const block = deriveStartBlock({
      station: { leaveDisabled: false },
      disk: { verdict: 'low', free: 1e9, startFloor: 3e9 },
      verdicts: LEADER_STALLED,
      hfToken: 'none',
    });
    expect(block.kind).toBe('station');
    expect(block.problem).toEqual({
      kind: 'bad',
      textDe: 'Roboter Studio nutzt gerade die Kamerastation.',
      stationLeave: true,
      stationLeaveDisabled: false,
    });
  });

  it('turns Start off and shows the banner with „Station verlassen"', () => {
    const m = deriveRecordView({
      heartbeat: 'connected', status: READY_STATUS, form: FORM, station: { leaveDisabled: false },
    });
    expect(m.view).toBe(VIEW.READY);
    expect(m.startBlock.kind).toBe('station');
    expect(m.buttons.find((b) => b.id === 'start').disabled).toBe(true);
    const p = firstProblem({ view: m.view, heartbeat: 'connected', startBlock: m.startBlock });
    expect(p.textDe).toBe(RECORD_COPY.problem.station);
    expect(p.stationLeave).toBe(true);
  });

  it('outranks the stalled leader on omx_full, which a session always has (D7, §9.2)', () => {
    // During a session the leader is necessarily off, so its source reads
    // stalled; the banner must still show the station block and its button.
    const m = deriveRecordView({
      heartbeat: 'connected', status: READY_STATUS, form: FORM, station: { leaveDisabled: false },
      verdicts: LEADER_STALLED, bridge: FOLLOWER_ONLY_BRIDGE, stationRig: true,
    });
    expect(m.startBlock.kind).toBe('station');
    const list = deriveProblems({
      view: m.view, heartbeat: 'connected', startBlock: m.startBlock,
      verdicts: LEADER_STALLED, bridge: FOLLOWER_ONLY_BRIDGE, stationRig: true,
    });
    expect(list[0].textDe).toBe(RECORD_COPY.problem.station);
    expect(list[0].stationLeave).toBe(true);
    // Listed once, and the leader's own sentence still follows it.
    expect(list.filter((p) => p.stationLeave)).toHaveLength(1);
    expect(list[1].textDe).toBe(RECORD_COPY.problem.leaderOffStation);
  });

  it('offers the button disabled while a real program runs', () => {
    expect(stationProblem({ leaveDisabled: true }).stationLeaveDisabled).toBe(true);
    expect(RECORD_COPY.problem.stationLeave).toBe('Station verlassen');
    expect(RECORD_COPY.problem.stationLeaveBlocked).toBe('Stoppe zuerst dein Programm.');
  });

  it('is absent without a station claim: manual rigs are unchanged', () => {
    expect(stationProblem(null)).toBe(null);
    const m = deriveRecordView({ heartbeat: 'connected', status: READY_STATUS, form: FORM });
    expect(m.startBlock).toBe(null);
    expect(deriveStartBlock({ verdicts: LEADER_STALLED, bridge: FOLLOWER_ONLY_BRIDGE }).problem.textDe)
      .toBe(RECORD_COPY.problem.leaderOff);
  });
});

describe('the switched-off leader on a station rig (§9.2)', () => {
  it('ends with „… im Roboter Studio („Station verlassen und Leader einschalten“)"', () => {
    const p = stalledSourceProblem(LEADER_STALLED[0], { bridge: FOLLOWER_ONLY_BRIDGE, stationRig: true });
    expect(p.textDe).toBe(RECORD_COPY.problem.leaderOffStation);
    expect(p.textDe.endsWith('im Roboter Studio („Station verlassen und Leader einschalten“)')).toBe(true);
    // The manual rig keeps its sentence.
    expect(stalledSourceProblem(LEADER_STALLED[0], { bridge: FOLLOWER_ONLY_BRIDGE }).textDe)
      .toBe(RECORD_COPY.problem.leaderOff);
  });

  it('reaches the banner and the Start block through stationRig', () => {
    const block = deriveStartBlock({ verdicts: LEADER_STALLED, bridge: FOLLOWER_ONLY_BRIDGE, stationRig: true });
    expect(block.problem.textDe).toBe(RECORD_COPY.problem.leaderOffStation);
    const list = deriveProblems({ verdicts: LEADER_STALLED, bridge: FOLLOWER_ONLY_BRIDGE, stationRig: true });
    expect(list[0].textDe).toBe(RECORD_COPY.problem.leaderOffStation);
  });
});
