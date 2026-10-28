import { apiRequest } from './apiClient';

export const listTeachers = (token) => apiRequest('/admin/teachers', 'GET', token);

export const createTeacher = (token, body) =>
  apiRequest('/admin/teachers', 'POST', token, body);

// Training hours are moved by DELTAS only (credit system v2, decision 8): a
// grant (`delta > 0`) or a revoke (`delta < 0`) of a teacher's free pool hours,
// with an optional reason and the client's `request_id` (a replay of the same
// change answers the same; a reuse for another change is a 409). There is no
// absolute „set" any more.
export const changeTeacherCredits = (token, teacherId, { delta, reason, request_id }) => {
  const body = { delta, request_id };
  if (typeof reason === 'string' && reason.trim()) body.reason = reason.trim();
  return apiRequest(`/admin/teachers/${teacherId}/credits`, 'POST', token, body);
};

// An admin's compensation (D4): hours straight into the student or group
// account that paid a training, funded from outside the teacher pool, with a
// mandatory reason the teacher sees in the history.
export const compensate = (
  token,
  { target_kind, target_id, hours, reason, training_id, request_id }
) => {
  const body = { target_kind, target_id, hours, reason, request_id };
  if (training_id !== null && training_id !== undefined) body.training_id = training_id;
  return apiRequest('/admin/credit-grants', 'POST', token, body);
};

// Erase the free text of one reason (Art. 17); the ledger row stays.
export const eraseReason = (token, reasonId) =>
  apiRequest(`/admin/credit-reasons/${encodeURIComponent(reasonId)}`, 'DELETE', token);

// `scope`: 'teacher_all' (with `teacher_id`) or 'all'; newest first, paged by `before`.
export const getCreditHistory = (token, { scope, teacher_id, before, limit } = {}) => {
  const params = new URLSearchParams();
  if (scope) params.set('scope', scope);
  if (teacher_id) params.set('teacher_id', teacher_id);
  if (before !== null && before !== undefined) params.set('before', String(before));
  if (limit) params.set('limit', String(limit));
  const qs = params.toString();
  return apiRequest(`/admin/credit-history${qs ? `?${qs}` : ''}`, 'GET', token);
};

export const resetTeacherPassword = (token, teacherId, newPassword) =>
  apiRequest(`/admin/teachers/${teacherId}/password`, 'POST', token, {
    new_password: newPassword,
  });

export const deleteTeacher = (token, teacherId) =>
  apiRequest(`/admin/teachers/${teacherId}`, 'DELETE', token);
