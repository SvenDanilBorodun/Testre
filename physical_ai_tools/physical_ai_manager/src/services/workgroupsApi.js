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

export const adjustWorkgroupCredits = (token, workgroupId, delta) =>
  apiRequest(`/teacher/workgroups/${workgroupId}/credits`, 'POST', token, {
    delta,
  });
