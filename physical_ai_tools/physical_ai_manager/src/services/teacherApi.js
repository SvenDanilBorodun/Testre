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

// ---------- Lessons ----------

export const listLessons = (token, classroomId) =>
  apiRequest(`/teacher/classrooms/${classroomId}/lessons`, 'GET', token);

export const createLesson = (token, classroomId, body) =>
  apiRequest(
    `/teacher/classrooms/${classroomId}/lessons`,
    'POST',
    token,
    body
  );

export const patchLesson = (token, lessonId, body) =>
  apiRequest(`/teacher/lessons/${lessonId}`, 'PATCH', token, body);

export const deleteLesson = (token, lessonId) =>
  apiRequest(`/teacher/lessons/${lessonId}`, 'DELETE', token);

export const listLessonProgress = (token, lessonId) =>
  apiRequest(`/teacher/lessons/${lessonId}/progress`, 'GET', token);

export const upsertLessonProgress = (token, lessonId, studentId, body) =>
  apiRequest(
    `/teacher/lessons/${lessonId}/students/${studentId}/progress`,
    'PUT',
    token,
    body
  );
