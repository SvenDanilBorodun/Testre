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

// Same shape as `workflowApi.js` STATUS_MESSAGES_DE — translates the
// API's English fallback strings to German for student-facing toasts.
// Audit round-3 §AO — tutorialApi previously surfaced the raw English
// "Too many requests" message on 429.
const STATUS_MESSAGES_DE = {
  400: 'Anfrage ungültig.',
  401: 'Sitzung abgelaufen — bitte erneut anmelden.',
  403: 'Aktion nicht erlaubt.',
  404: 'Tutorial-Eintrag nicht gefunden.',
  413: 'Anfrage zu groß.',
  429: 'Zu viele Anfragen — bitte kurz warten.',
  500: 'Server-Fehler — bitte später erneut versuchen.',
  502: 'Server nicht erreichbar.',
  503: 'Server überlastet — bitte später erneut versuchen.',
  504: 'Server hat zu lange gebraucht.',
};

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
    // Prefer the API's own German detail (FastAPI returns German for
    // 4xx in this project). On English fallbacks (rate-limit
    // middleware, opaque 5xx), substitute the German status table.
    const detail = typeof error.detail === 'string' ? error.detail : '';
    const looksLocalized = /[äöüß]/i.test(detail) || /Workflow|Tutorial|nicht|ist/.test(detail);
    const fallback = STATUS_MESSAGES_DE[response.status] || `Server-Fehler (${response.status}).`;
    throw new TutorialApiError(
      looksLocalized && detail ? detail : fallback,
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
