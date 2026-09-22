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

// Mid-training checkpoint steps the Modal worker mirrored to the model
// repo (leLab-comparison PR-5a). Teacher-only diagnostics.
export const listStudentInferenceRuns = (token, studentId) =>
  apiRequest(`/teacher/students/${studentId}/inference-runs`, 'GET', token);

export const listTrainingCheckpoints = (token, studentId, trainingId) =>
  apiRequest(
    `/teacher/students/${studentId}/trainings/${trainingId}/checkpoints`,
    'GET',
    token
  );

// ---------- Roboter Studio: a student's programs + „Abgaben" (migration 040) ----------
//
// Decision A3: a teacher sees (a) the programs as they stand and (b) the
// snapshots the student handed in. Read-only, and per STUDENT — every one of
// the four routes sits behind `_assert_student_owned` and is scoped on the
// row's own student column, never on `workflows.classroom_id` (routes/teacher.py).

export const listStudentWorkflows = (token, studentId) =>
  apiRequest(`/teacher/students/${studentId}/workflows`, 'GET', token);

export const getStudentWorkflow = (token, studentId, workflowId) =>
  apiRequest(`/teacher/students/${studentId}/workflows/${workflowId}`, 'GET', token);

export const listStudentSubmissions = (token, studentId) =>
  apiRequest(`/teacher/students/${studentId}/submissions`, 'GET', token);

export const getStudentSubmission = (token, studentId, submissionId) =>
  apiRequest(`/teacher/students/${studentId}/submissions/${submissionId}`, 'GET', token);

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
