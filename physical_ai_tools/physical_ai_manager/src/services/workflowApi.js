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

async function apiRequest(endpoint, method, accessToken, body = null) {
  const headers = {
    'Content-Type': 'application/json',
    Authorization: `Bearer ${accessToken}`,
  };
  const options = { method, headers };
  if (body) options.body = JSON.stringify(body);
  const response = await fetch(`${API_URL}${endpoint}`, options);
  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(error.detail || `Request failed: ${response.status}`);
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
