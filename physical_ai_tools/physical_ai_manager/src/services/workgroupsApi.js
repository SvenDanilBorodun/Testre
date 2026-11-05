import { apiRequest } from './apiClient';

// Workgroups (Arbeitsgruppen) inside a classroom. Mirrors the shape of
// teacherApi.js. All endpoints require teacher role on the server side.

export const listWorkgroups = (token, classroomId) =>
  apiRequest(`/teacher/classrooms/${classroomId}/workgroups`, 'GET', token);

export const createWorkgroup = (token, classroomId, name) =>
  apiRequest(`/teacher/classrooms/${classroomId}/workgroups`, 'POST', token, {
    name,
  });

export const getWorkgroup = (token, workgroupId) =>
  apiRequest(`/teacher/workgroups/${workgroupId}`, 'GET', token);

export const renameWorkgroup = (token, workgroupId, name) =>
  apiRequest(`/teacher/workgroups/${workgroupId}`, 'PATCH', token, { name });

export const deleteWorkgroup = (token, workgroupId) =>
  apiRequest(`/teacher/workgroups/${workgroupId}`, 'DELETE', token);

export const addWorkgroupMember = (token, workgroupId, studentId) =>
  apiRequest(`/teacher/workgroups/${workgroupId}/members`, 'POST', token, {
    student_id: studentId,
  });

export const removeWorkgroupMember = (token, workgroupId, studentId) =>
  apiRequest(
    `/teacher/workgroups/${workgroupId}/members/${studentId}`,
    'DELETE',
    token
  );

// Training hours of a group (credit system v2, spec §8.3): a delta with the
// client's `request_id`, exactly like a student's.
export const changeWorkgroupHours = (token, workgroupId, { delta, request_id }) =>
  apiRequest(`/teacher/workgroups/${workgroupId}/credits`, 'POST', token, {
    delta,
    request_id,
  });

// One per-member hour limit for the whole group (D5): 1…1000, or null for none.
export const setMemberLimit = (token, workgroupId, hours) =>
  apiRequest(`/teacher/workgroups/${workgroupId}/member-limit`, 'PUT', token, {
    hours,
  });

export const getWorkgroupCreditHistory = (token, workgroupId, { before, limit } = {}) => {
  const params = new URLSearchParams();
  if (before !== null && before !== undefined) params.set('before', String(before));
  if (limit) params.set('limit', String(limit));
  const qs = params.toString();
  return apiRequest(
    `/teacher/workgroups/${workgroupId}/credit-history${qs ? `?${qs}` : ''}`,
    'GET',
    token
  );
};

// Last 100 trainings spawned by any (current or former) member of the
// group, latest first. Includes started_by_* attribution so the panel
// can show "Gestartet von <name>".
export const listWorkgroupTrainings = (token, workgroupId) =>
  apiRequest(`/teacher/workgroups/${workgroupId}/trainings`, 'GET', token);
