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

export async function startCloudTraining(accessToken, { datasetName, modelType, trainingParams }) {
  return apiRequest('/trainings/start', 'POST', accessToken, {
    dataset_name: datasetName,
    model_type: modelType,
    training_params: trainingParams,
  });
}

export async function cancelCloudTraining(accessToken, trainingId) {
  return apiRequest('/trainings/cancel', 'POST', accessToken, {
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
