import { apiRequest } from './apiClient';

export const listClassrooms = (token) =>
  apiRequest('/teacher/classrooms', 'GET', token);

export const createClassroom = (token, name) =>
  apiRequest('/teacher/classrooms', 'POST', token, { name });

export const getClassroom = (token, classroomId) =>
  apiRequest(`/teacher/classrooms/${classroomId}`, 'GET', token);

export const renameClassroom = (token, classroomId, name) =>
  apiRequest(`/teacher/classrooms/${classroomId}`, 'PATCH', token, { name });

export const deleteClassroom = (token, classroomId) =>
  apiRequest(`/teacher/classrooms/${classroomId}`, 'DELETE', token);

export const createStudent = (token, classroomId, body) =>
  apiRequest(`/teacher/classrooms/${classroomId}/students`, 'POST', token, body);

export const patchStudent = (token, studentId, body) =>
  apiRequest(`/teacher/students/${studentId}`, 'PATCH', token, body);

export const deleteStudent = (token, studentId) =>
  apiRequest(`/teacher/students/${studentId}`, 'DELETE', token);

export const resetStudentPassword = (token, studentId, newPassword) =>
  apiRequest(`/teacher/students/${studentId}/password`, 'POST', token, {
    new_password: newPassword,
  });

export const adjustStudentCredits = (token, studentId, delta) =>
  apiRequest(`/teacher/students/${studentId}/credits`, 'POST', token, { delta });

export const listStudentTrainings = (token, studentId) =>
  apiRequest(`/teacher/students/${studentId}/trainings`, 'GET', token);

// ---------- Daily progress entries ----------

export const listProgressEntries = (
  token,
  classroomId,
  { studentId, workgroupId, scope } = {}
) => {
  const params = new URLSearchParams();
  if (studentId) params.set('student_id', studentId);
  if (workgroupId) params.set('workgroup_id', workgroupId);
  if (scope) params.set('scope', scope);
  const qs = params.toString();
  const suffix = qs ? `?${qs}` : '';
  return apiRequest(
    `/teacher/classrooms/${classroomId}/progress-entries${suffix}`,
    'GET',
    token
  );
};

export const createProgressEntry = (token, classroomId, body) =>
  apiRequest(
    `/teacher/classrooms/${classroomId}/progress-entries`,
    'POST',
    token,
    body
  );

export const patchProgressEntry = (token, entryId, note) =>
  apiRequest(`/teacher/progress-entries/${entryId}`, 'PATCH', token, { note });

export const deleteProgressEntry = (token, entryId) =>
  apiRequest(`/teacher/progress-entries/${entryId}`, 'DELETE', token);

// ---------- Classroom Jetson (v2.3.0) ----------
// Teacher wrappers for the classroom-Jetson lifecycle. The read endpoint
// is at /classrooms/{id}/jetson (works for any classroom member; teachers
// are members of their own classrooms). The write endpoints are at
// /teacher/classrooms/{id}/jetson/* and require role=teacher + classroom
// ownership (enforced server-side via _assert_classroom_owned).

export const getClassroomJetson = (token, classroomId) =>
  apiRequest(`/classrooms/${classroomId}/jetson`, 'GET', token);
