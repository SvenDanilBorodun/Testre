import { createSlice } from '@reduxjs/toolkit';

const initialState = {
  classrooms: [],
  classroomsLoading: false,
  selectedClassroomId: null,
  selectedClassroom: null, // full detail with students
  classroomLoading: false,
  studentTrainings: {}, // { studentId: [trainings] }
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
  resetTeacher,
} = teacherSlice.actions;

export default teacherSlice.reducer;
