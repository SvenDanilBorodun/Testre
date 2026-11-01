// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// The React side of the camera station controller (station spec v6.13, §9.1):
// the snapshot through useSyncExternalStore, the host StudentApp mounts once,
// and the Roboter-Studio page's visit report. Pure decisions the other pages
// read live in gating.js.

import { useEffect, useSyncExternalStore } from 'react';

import * as controller from './controller';

/** The whole snapshot (re-renders on every controller change). */
export function useStationSnapshot() {
  return useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot);
}

/**
 * One derived value; the component re-renders only when IT changes. Use a
 * selector that returns a primitive (the Aufnahme page renders a few times a
 * second at most, never per controller tick).
 * @param {(snapshot: object) => (string|number|boolean|null)} selector
 */
export function useStationValue(selector) {
  const get = () => selector(controller.getSnapshot());
  return useSyncExternalStore(controller.subscribe, get, get);
}

/**
 * StudentApp: start the controller once and feed it the rig context on every
 * change (§9.1: active only on a station rig, not in Pi mode, not while a
 * Jetson is claimed; the resume `status` goes out before the capabilities).
 */
export function useStationHost({
  capabilities, piMode = false, piModeResolved = true, jetsonConnected = false, enabled = true,
}) {
  useEffect(() => {
    if (enabled) controller.start();
  }, [enabled]);
  const stationProtocol = capabilities ? capabilities.station_protocol : undefined;
  const capabilitiesKnown = Boolean(capabilities);
  useEffect(() => {
    if (!enabled) return;
    controller.configure({
      stationProtocol, capabilitiesKnown, piMode, piModeResolved, jetsonConnected,
    });
  }, [enabled, stationProtocol, capabilitiesKnown, piMode, piModeResolved, jetsonConnected]);
}

/**
 * WorkshopPage: report the visit (open while shown) and the leader state the
 * control bridge reports (§9.1 step 1, D7).
 */
export function useStationWorkshop({ isActive, leaderOn = false, leaderProbed = true }) {
  useEffect(() => {
    controller.setWorkshop({ open: Boolean(isActive), leaderOn, leaderProbed });
  }, [isActive, leaderOn, leaderProbed]);
  useEffect(() => () => {
    controller.setWorkshop({ open: false });
  }, []);
}
