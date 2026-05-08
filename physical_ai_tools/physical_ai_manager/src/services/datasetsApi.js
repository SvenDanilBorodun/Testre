import { apiRequest } from './apiClient';

// Discovery registry for HF Hub datasets. Group siblings see each other's
// uploaded datasets via this layer; the HF repo itself stays under the
// owner's HF user.

export const listDatasets = (token) =>
  apiRequest('/datasets', 'GET', token);

export const registerDataset = (token, payload) =>
  apiRequest('/datasets', 'POST', token, payload);

export const updateDataset = (token, datasetId, payload) =>
  apiRequest(`/datasets/${datasetId}`, 'PATCH', token, payload);

export const deleteDataset = (token, datasetId) =>
  apiRequest(`/datasets/${datasetId}`, 'DELETE', token);
