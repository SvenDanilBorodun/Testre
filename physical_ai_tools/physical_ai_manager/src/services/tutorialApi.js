/*
 * Copyright 2025 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

import { CLOUD_API_URL, assertCloudApiConfigured } from './cloudConfig';

const DEFAULT_TIMEOUT_MS = 15_000;

class TutorialApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = 'TutorialApiError';
    this.status = status;
  }
}

async function apiRequest(endpoint, method, accessToken, body = null) {
  if (!accessToken) {
    throw new TutorialApiError('Nicht angemeldet.', 401);
  }
  assertCloudApiConfigured();
  const headers = {
    'Content-Type': 'application/json',
    Authorization: `Bearer ${accessToken}`,
  };
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), DEFAULT_TIMEOUT_MS);
  const options = { method, headers, signal: controller.signal };
  if (body) options.body = JSON.stringify(body);
  let response;
  try {
    response = await fetch(`${CLOUD_API_URL}${endpoint}`, options);
  } catch (err) {
    clearTimeout(timer);
    throw new TutorialApiError(
      err.name === 'AbortError'
        ? 'Server hat zu lange gebraucht.'
        : 'Verbindung zum Server fehlgeschlagen.',
      0,
    );
  }
  clearTimeout(timer);
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new TutorialApiError(
      error.detail || `Server-Fehler (${response.status}).`,
      response.status,
    );
  }
  return response.json();
}

export async function listTutorialProgress(accessToken) {
  return apiRequest('/me/tutorial-progress', 'GET', accessToken);
}

export async function updateTutorialProgress(accessToken, tutorialId, data) {
  return apiRequest(
    `/me/tutorial-progress/${encodeURIComponent(tutorialId)}`,
    'PATCH',
    accessToken,
    data,
  );
}
