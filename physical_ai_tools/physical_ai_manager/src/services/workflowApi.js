/*
 * Copyright 2025 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

const API_URL = process.env.REACT_APP_CLOUD_API_URL;

const DEFAULT_TIMEOUT_MS = 30_000;

// HTTP-status → student-facing German message. The FastAPI side
// returns English "detail" strings; we'd rather show consistent,
// kid-friendly German. Specific cases that the cloud API distinguishes
// (rate limit, validation error) flow through with their detail
// preserved so the editor can show the actual reason.
const STATUS_MESSAGES_DE = {
  401: 'Sitzung abgelaufen — bitte erneut einloggen.',
  403: 'Diese Aktion ist für deinen Zugang nicht erlaubt.',
  404: 'Nicht gefunden.',
  409: 'Konflikt — bitte Seite neu laden und nochmals versuchen.',
  413: 'Workflow ist zu groß zum Speichern.',
  429: 'Zu viele Anfragen — bitte einen Moment warten.',
};

class WorkflowApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = 'WorkflowApiError';
    this.status = status;
  }
}

async function apiRequest(endpoint, method, accessToken, body = null) {
  // Audit §3.22b — explicit guard against literal "Bearer undefined"
  // headers when a re-login race lands here without a token.
  if (!accessToken) {
    throw new WorkflowApiError('Nicht angemeldet — bitte erneut einloggen.', 401);
  }
  const headers = {
    'Content-Type': 'application/json',
    Authorization: `Bearer ${accessToken}`,
  };
  // Audit §3.22a — fixed 30 s timeout via AbortController so a hung
  // Railway cold-start doesn't pin a fetch indefinitely.
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), DEFAULT_TIMEOUT_MS);
  const options = { method, headers, signal: controller.signal };
  if (body) options.body = JSON.stringify(body);
  let response;
  try {
    response = await fetch(`${API_URL}${endpoint}`, options);
  } catch (err) {
    clearTimeout(timer);
    if (err.name === 'AbortError') {
      throw new WorkflowApiError(
        'Server hat zu lange gebraucht — bitte erneut versuchen.',
        0,
      );
    }
    throw new WorkflowApiError(
      'Verbindung zum Server fehlgeschlagen.',
      0,
    );
  }
  clearTimeout(timer);
  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }));
    // Audit §3.10 — prefer a German status message over the FastAPI
    // detail string when the detail is just an English HTTP reason.
    // For 4xx that the API distinguishes (validation), the detail is
    // German already and is preserved.
    const fallback = STATUS_MESSAGES_DE[response.status]
      || `Server-Fehler (${response.status}).`;
    const detail = (error && error.detail) || '';
    const looksLocalized = /[äöüß]/i.test(detail);
    throw new WorkflowApiError(
      looksLocalized ? detail : fallback,
      response.status,
    );
  }
  return response.json();
}

export async function listWorkflows(accessToken) {
  return apiRequest('/workflows', 'GET', accessToken);
}

export async function getWorkflow(accessToken, workflowId) {
  return apiRequest(`/workflows/${workflowId}`, 'GET', accessToken);
}

export async function createWorkflow(accessToken, payload) {
  return apiRequest('/workflows', 'POST', accessToken, payload);
}

export async function updateWorkflow(accessToken, workflowId, payload) {
  return apiRequest(`/workflows/${workflowId}`, 'PATCH', accessToken, payload);
}

export async function deleteWorkflow(accessToken, workflowId) {
  return apiRequest(`/workflows/${workflowId}`, 'DELETE', accessToken);
}

export async function cloneWorkflow(accessToken, workflowId) {
  return apiRequest(`/workflows/${workflowId}/clone`, 'POST', accessToken);
}

export async function listClassroomTemplates(accessToken, classroomId) {
  return apiRequest(`/teacher/classrooms/${classroomId}/workflow-templates`, 'GET', accessToken);
}

export async function publishClassroomTemplate(accessToken, classroomId, payload) {
  return apiRequest(`/teacher/classrooms/${classroomId}/workflow-templates`, 'POST', accessToken, payload);
}

export async function deleteClassroomTemplate(accessToken, classroomId, templateId) {
  return apiRequest(`/teacher/classrooms/${classroomId}/workflow-templates/${templateId}`, 'DELETE', accessToken);
}
