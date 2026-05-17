import { createSlice } from '@reduxjs/toolkit';

const initialState = {
  classrooms: [],
  classroomsLoading: false,
  selectedClassroomId: null,
  selectedClassroom: null, // full detail with students
  classroomLoading: false,
  studentTrainings: {}, // { studentId: [trainings] }
  // Classroom Jetson (v2.3.0). Keyed by classroomId so switching
  // between classrooms doesn't blow away in-flight state. Each entry:
  //   { info: JetsonInfo | null, loading: bool, error: string | null,
  //     fetchedAt: ms, lastPairingCode: { code, expiresAt } | null }
  // info=null means "no Jetson paired" (the API returned 404).
  // lastPairingCode is set by regeneratePairingCode so the modal can
  // show the fresh code to the teacher.
  jetsonByClassroom: {},
};

const teacherSlice = createSlice({
  name: 'teacher',
  initialState,
  reducers: {
    setClassrooms: (state, action) => {
      state.classrooms = action.payload;
    },
    setClassroomsLoading: (state, action) => {
      state.classroomsLoading = action.payload;
    },
    selectClassroom: (state, action) => {
      state.selectedClassroomId = action.payload;
      if (!action.payload) state.selectedClassroom = null;
    },
    setSelectedClassroom: (state, action) => {
      state.selectedClassroom = action.payload;
    },
    setClassroomLoading: (state, action) => {
      state.classroomLoading = action.payload;
    },
    setStudentTrainings: (state, action) => {
      const { studentId, trainings } = action.payload;
      state.studentTrainings[studentId] = trainings;
    },
    upsertStudentInSelected: (state, action) => {
      if (!state.selectedClassroom) return;
      const updated = action.payload;
      const idx = state.selectedClassroom.students.findIndex((s) => s.id === updated.id);
      if (idx === -1) {
        state.selectedClassroom.students.push(updated);
      } else {
        state.selectedClassroom.students[idx] = updated;
      }
    },
    removeStudentFromSelected: (state, action) => {
      if (!state.selectedClassroom) return;
      state.selectedClassroom.students = state.selectedClassroom.students.filter(
        (s) => s.id !== action.payload
      );
    },
    // ---- Jetson state (v2.3.0) ----
    setJetsonLoading: (state, action) => {
      const { classroomId, loading } = action.payload;
      const entry = state.jetsonByClassroom[classroomId] || {
        info: null, loading: false, error: null, fetchedAt: 0, lastPairingCode: null,
      };
      state.jetsonByClassroom[classroomId] = { ...entry, loading };
    },
    setJetsonInfo: (state, action) => {
      // payload: { classroomId, info } — info is the JetsonInfo from
      // /classrooms/{id}/jetson or null when none is paired.
      const { classroomId, info } = action.payload;
      const entry = state.jetsonByClassroom[classroomId] || {
        info: null, loading: false, error: null, fetchedAt: 0, lastPairingCode: null,
      };
      state.jetsonByClassroom[classroomId] = {
        ...entry,
        info,
        loading: false,
        error: null,
        fetchedAt: Date.now(),
      };
    },
    setJetsonError: (state, action) => {
      const { classroomId, error } = action.payload;
      const entry = state.jetsonByClassroom[classroomId] || {
        info: null, loading: false, error: null, fetchedAt: 0, lastPairingCode: null,
      };
      state.jetsonByClassroom[classroomId] = {
        ...entry,
        loading: false,
        error,
      };
    },
    setJetsonLastPairingCode: (state, action) => {
      // payload: { classroomId, code, expiresAt } | { classroomId, code: null }
      const { classroomId, code, expiresAt } = action.payload;
      const entry = state.jetsonByClassroom[classroomId] || {
        info: null, loading: false, error: null, fetchedAt: 0, lastPairingCode: null,
      };
      state.jetsonByClassroom[classroomId] = {
        ...entry,
        lastPairingCode: code ? { code, expiresAt: expiresAt || null } : null,
      };
    },
    clearJetsonForClassroom: (state, action) => {
      // Used after unpair so the card flips back to "no Jetson" without
      // a refetch round-trip.
      const classroomId = action.payload;
      state.jetsonByClassroom[classroomId] = {
        info: null,
        loading: false,
        error: null,
        fetchedAt: Date.now(),
        lastPairingCode: null,
      };
    },
    resetTeacher: () => initialState,
  },
});

export const {
  setClassrooms,
  setClassroomsLoading,
  selectClassroom,
  setSelectedClassroom,
  setClassroomLoading,
  setStudentTrainings,
  upsertStudentInSelected,
  removeStudentFromSelected,
  setJetsonLoading,
  setJetsonInfo,
  setJetsonError,
  setJetsonLastPairingCode,
  clearJetsonForClassroom,
  resetTeacher,
} = teacherSlice.actions;

export default teacherSlice.reducer;
