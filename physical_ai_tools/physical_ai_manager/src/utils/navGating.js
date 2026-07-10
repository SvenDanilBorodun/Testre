// Pure decision helpers for the student sidebar gating in StudentApp. Extracted
// so the capability filter and the robot-identity navigation gate can be
// unit-tested without rendering the full StudentApp graph — the logic is
// identical to the inline predicates it replaces.

// Capability filter for a sidebar nav item. An item is hidden ONLY when its
// `capabilityKey` is EXPLICITLY false in the server capability manifest.
// Unknown/null caps (pre-first-tick, cloud mode, a pre-capability server image)
// hide NOTHING. In Jetson mode the filter is SKIPPED entirely (D9) —
// capabilities describe the LOCAL rig, and the Jetson keeps its own dedicated
// `jetsonIncompatible` filter, so Training must stay visible there exactly as
// today.
export function isCapabilityVisible(navItem, { jetsonConnected, caps }) {
  return (
    jetsonConnected ||
    !caps ||
    !navItem.capabilityKey ||
    caps[navItem.capabilityKey] !== false
  );
}

// Navigation gate for requireRobotOrRedirect. Returns 'navigate' when the
// student may enter a hardware page, 'wait' when the robot identity hasn't
// arrived yet. The debug flag forces navigation; cloud-only mode ALWAYS
// navigates (no rosbridge ever delivers an identity there, so gating would be a
// permanent dead-end); otherwise a non-empty robotType is required — it is
// boot-set on the server and re-published on the idle identity tick within
// ~2-3 s of connecting, so 'wait' is a brief hold, not a dead-end.
export function robotGateDecision({ debug, cloudOnly, robotType }) {
  if (debug) return 'navigate';
  if (cloudOnly) return 'navigate';
  if (robotType && robotType.trim() !== '') return 'navigate';
  return 'wait';
}
