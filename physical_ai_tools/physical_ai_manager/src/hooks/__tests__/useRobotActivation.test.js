/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 */

// The wire between activation_agent.py and the Startseite.
//
// The agent publishes std_msgs/String carrying JSON (the same
// no-interfaces-rebuild trick /sim/objects uses), so the parse is the contract.
// Two things it must never do: crash the page on a malformed payload, and turn
// „we have not heard from the agent" into „the robot is not activated" — the
// second is the Start page's whole doctrine, in a different file.

import { parseActivationState, IDLE, ACTIVE, ACTIVATING, FAILED } from '../useRobotActivation';

describe('parseActivationState', () => {
  it('reads a full agent payload', () => {
    const parsed = parseActivationState(JSON.stringify({
      state: 'active', step: '', message: 'Der Roboter ist aktiv.',
      robot_type: 'omx_full', has_leader: true, required: true, seq: 12,
    }));
    expect(parsed).toEqual({
      state: ACTIVE,
      step: '',
      message: 'Der Roboter ist aktiv.',
      robotType: 'omx_full',
      hasLeader: true,
      required: true,
    });
  });

  it('accepts every state the agent can be in', () => {
    for (const state of [IDLE, ACTIVATING, ACTIVE, FAILED]) {
      expect(parseActivationState(JSON.stringify({ state }))?.state).toBe(state);
    }
  });

  it('returns null — not a default object — for anything untrustworthy', () => {
    // null is UNKNOWN. An object here would be read as „not activated", which
    // is a claim about the student's rig that nobody checked.
    expect(parseActivationState('')).toBeNull();
    expect(parseActivationState(undefined)).toBeNull();
    expect(parseActivationState('not json')).toBeNull();
    expect(parseActivationState('[]')).toBeNull();
    expect(parseActivationState('null')).toBeNull();
    expect(parseActivationState('"a string"')).toBeNull();
    expect(parseActivationState(JSON.stringify({ state: 'bogus' }))).toBeNull();
    expect(parseActivationState(JSON.stringify({ nostate: 1 }))).toBeNull();
  });

  it('reports an omitted has_leader as UNKNOWN, not as a guess', () => {
    // It used to default to `true`, on the reasoning that an old agent is
    // „more likely than not" to have a leader. The fleet says otherwise —
    // three of the four shipped profiles are follower-only — so the guess was
    // wrong for most rigs. `null` hands the question to
    // ActivationCard::resolveHasLeader, which asks the capability manifest.
    const parsed = parseActivationState(JSON.stringify({ state: 'idle' }));
    expect(parsed.hasLeader).toBeNull();
    // `required` keeps its default: absent ⇒ the gate is on, which is the
    // safe world and the one every current image is in.
    expect(parsed.required).toBe(true);
  });

  it('honours an EXPLICIT false on both', () => {
    const parsed = parseActivationState(JSON.stringify({
      state: 'idle', has_leader: false, required: false,
    }));
    expect(parsed.hasLeader).toBe(false);
    expect(parsed.required).toBe(false);
  });

  it('coerces non-string text fields to empty rather than rendering objects', () => {
    const parsed = parseActivationState(JSON.stringify({
      state: 'idle', step: 3, message: { a: 1 }, robot_type: null,
    }));
    expect(parsed.step).toBe('');
    expect(parsed.message).toBe('');
    expect(parsed.robotType).toBe('');
  });
});
