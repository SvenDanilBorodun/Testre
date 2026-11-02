import { CLOUD_API_URL, assertCloudApiConfigured } from './cloudConfig';

async function apiRequest(endpoint, method, accessToken, body = null) {
  assertCloudApiConfigured();
  const headers = {
    'Content-Type': 'application/json',
    Authorization: `Bearer ${accessToken}`,
  };

  const options = { method, headers };
  if (body) {
    options.body = JSON.stringify(body);
  }

  const response = await fetch(`${CLOUD_API_URL}${endpoint}`, options);

  if (!response.ok) {
    // Same status-carrying contract as services/apiClient.js — callers
    // (e.g. the quota refetch) can branch on err.status instead of parsing
    // an opaque message string.
    let detail;
    try {
      detail = (await response.json()).detail;
    } catch {
      detail = response.statusText;
    }
    const err = new Error(detail || `HTTP ${response.status}`);
    err.status = response.status;
    err.detail = detail;
    throw err;
  }

  return response.json();
}

export async function getQuota(accessToken) {
  return apiRequest('/trainings/quota', 'GET', accessToken);
}

// A fresh v4 UUID for an `idempotency_key` / `request_id` (credit system v2).
// `crypto.randomUUID` exists only in a SECURE context, and the Orange Pi serves
// this app over plain http on the LAN, so the fallback builds the same v4 UUID
// from `crypto.getRandomValues`, which every context has.
export function newRequestId() {
  const c = globalThis.crypto;
  if (c && typeof c.randomUUID === 'function') return c.randomUUID();
  const b = new Uint8Array(16);
  c.getRandomValues(b);
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  const h = Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}

// `idempotencyKey` (credit system v2, spec §8.2): one fresh UUID per confirmed
// start dialog. A replay of the same key answers the row it created
// (`replay: true`) instead of reserving hours twice.
export async function startCloudTraining(accessToken, { datasetName, modelType, trainingParams, idempotencyKey }) {
  const body = {
    dataset_name: datasetName,
    model_type: modelType,
    training_params: trainingParams,
  };
  if (idempotencyKey) body.idempotency_key = idempotencyKey;
  return apiRequest('/trainings/start', 'POST', accessToken, body);
}

// What „Abbrechen" would do right now (spec §8.2): `{training_id, trained,
// checkpoint_step, next_checkpoint_step, final_ready, reserved_hours,
// charged_hours_now, hours_back_now, outcome}`. Advisory only.
export async function getStopPreview(accessToken, trainingId) {
  return apiRequest(`/trainings/${encodeURIComponent(trainingId)}/stop-preview`, 'GET', accessToken);
}

export async function cancelCloudTraining(accessToken, trainingId) {
  return apiRequest('/trainings/cancel', 'POST', accessToken, {
    training_id: trainingId,
  });
}

// „Fortsetzen" (owner E32–E37): continue a capped training with a new
// reservation of training hours (the answer carries `reserved_hours`).
// The thrown error carries err.status and err.detail (the API's German sentence).
export async function continueCloudTraining(accessToken, trainingId) {
  return apiRequest('/trainings/continue', 'POST', accessToken, {
    training_id: trainingId,
  });
}

export async function getTrainingJobs(accessToken) {
  return apiRequest('/trainings/list', 'GET', accessToken);
}

export async function getTrainingStatus(accessToken, trainingId) {
  return apiRequest(`/trainings/${trainingId}`, 'GET', accessToken);
}

export async function getPolicies(accessToken) {
  return apiRequest('/policies', 'GET', accessToken);
}
