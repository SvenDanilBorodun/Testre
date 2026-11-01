// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// The camera station's wire names on the page side (station spec v6.13, §3.3,
// §8, §9). One module, so the preview, the controller and the cross-package
// lockstep test (robotis_ai_setup/tests/test_station_lockstep.py) read ONE
// spelling of each name.
//
// The three ROS services (§8) live on the robot's LOCAL rosbridge only; the
// GUI's Studio-stream lease lives on the Windows control bridge (§3.3), never
// on the Orange Pi (D1: Linux / Pi stations are out of this round).

/** `capabilities.station_protocol` of a station rig (§5.6). */
export const STATION_PROTOCOL = 6;

/** The Studio stream's topic and camera name (§4, §5.4, §9.2 Preview). */
export const STATION_TOPIC = '/station_scene/image_raw/compressed';
export const STATION_CAMERA = 'station_scene';

/** The three services (§8.1–§8.3). */
export const SESSION_SERVICE = '/station/session';
export const SESSION_SERVICE_TYPE = 'physical_ai_interfaces/srv/StationSession';
export const CHECK_SERVICE = '/station/check';
export const CHECK_SERVICE_TYPE = 'physical_ai_interfaces/srv/StationCheck';
export const COMMISSION_SERVICE = '/station/commission';
export const COMMISSION_SERVICE_TYPE = 'physical_ai_interfaces/srv/StationCommission';

/** The GUI's Studio-stream lease (§3.3): start-or-renew `on`, immediate `off`. */
export const STREAM_PATH = '/station/stream';

// Timing (§9.1 steps 0, 3, 6).
export const KEEPALIVE_MS = 2000;
export const KEEPALIVE_TIMEOUT_MS = 1500;
export const ENTER_RETRY_MS = 2000;
export const DUPLICATE_TAB_WAIT_MS = 300;
// How often the resume `status` of step 1 is retried while the local rosbridge
// is not connected yet (a reload lands before the socket is up).
export const RESUME_POLL_MS = 500;
// A one-off command (enter, leave, a check, a commission command): never an
// unbounded wait. The camera check retries frames for up to 2 s (§7.1).
export const COMMAND_TIMEOUT_MS = 8000;
export const GUI_POST_TIMEOUT_MS = 1500;
// The re-solve job and the wizard poll `status` every 2 s (§7.3, §9.2).
export const JOB_POLL_MS = 2000;
// The robot check holds a hand-guide session open while the student guides
// the tip; the existing keepalive rhythm of JogPanel (FE-4).
export const HAND_GUIDE_KEEPALIVE_MS = 15000;

/** The duplicated-tab channel of the per-tab client id (§9.1 step 0; clientId.js). */
export const BROADCAST_CHANNEL = 'edubotics-station';

/** A check result that says nothing about the camera's pose (§7.1). */
export const CHECK_RETRY_RESULTS = Object.freeze(['NO_FRAME', 'WRONG_SIZE', 'BUSY']);

/** A job is over (§8.2 / §8.3 `job_state`). */
export const JOB_END_STATES = Object.freeze(['done', 'failed', 'stale']);

/** Is this robot a camera station (§5.6, §9.2)? Unknown capabilities: no. */
export function isStationRig(capabilities) {
  return Boolean(capabilities) && capabilities.station_protocol === STATION_PROTOCOL;
}
