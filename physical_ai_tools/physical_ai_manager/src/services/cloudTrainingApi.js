const API_URL = process.env.REACT_APP_CLOUD_API_URL;

async function apiRequest(endpoint, method, accessToken, body = null) {
  const headers = {
    'Content-Type': 'application/json',
    Authorization: `Bearer ${accessToken}`,
  };

  const options = { method, headers };
  if (body) {
    options.body = JSON.stringify(body);
  }

  const response = await fetch(`${API_URL}${endpoint}`, options);

  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(error.detail || `Request failed: ${response.status}`);
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
