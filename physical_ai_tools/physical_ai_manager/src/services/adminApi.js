import { apiRequest } from './apiClient';

export const listTeachers = (token) => apiRequest('/admin/teachers', 'GET', token);

export const createTeacher = (token, body) =>
  apiRequest('/admin/teachers', 'POST', token, body);

export const setTeacherCredits = (token, teacherId, credits) =>
  apiRequest(`/admin/teachers/${teacherId}/credits`, 'PATCH', token, { credits });

export const resetTeacherPassword = (token, teacherId, newPassword) =>
  apiRequest(`/admin/teachers/${teacherId}/password`, 'POST', token, {
    new_password: newPassword,
  });

export const deleteTeacher = (token, teacherId) =>
  apiRequest(`/admin/teachers/${teacherId}`, 'DELETE', token);
