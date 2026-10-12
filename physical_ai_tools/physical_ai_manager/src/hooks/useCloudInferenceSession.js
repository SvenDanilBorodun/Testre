// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

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
