// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.

// The Inferenz page's cloud session (features/cloudInference/controller.js) as
// React state. The snapshot never carries a token: the connect token and the
// edge token live only inside the controller's start() for one robot call.

import { useSyncExternalStore } from 'react';

import { getSnapshot, subscribe } from '../features/cloudInference/controller';

/**
 * @returns {{phase: string, sessionId: ?string, trainingId: ?number, summary: ?object,
 *            sessionS: ?number, message: string, modelRefused: boolean, seq: number}}
 */
export default function useCloudInferenceSession() {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}
