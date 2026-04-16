import { apiRequest } from './apiClient';

export async function getMe(accessToken) {
  return apiRequest('/me', 'GET', accessToken);
}
