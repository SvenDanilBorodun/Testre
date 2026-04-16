import { createSlice } from '@reduxjs/toolkit';

const initialState = {
  teachers: [],
  loading: false,
};

const adminSlice = createSlice({
  name: 'admin',
  initialState,
  reducers: {
    setTeachers: (state, action) => {
      state.teachers = action.payload;
    },
    setLoading: (state, action) => {
      state.loading = action.payload;
    },
    upsertTeacher: (state, action) => {
      const updated = action.payload;
      const idx = state.teachers.findIndex((t) => t.id === updated.id);
      if (idx === -1) state.teachers.push(updated);
      else state.teachers[idx] = updated;
    },
    removeTeacher: (state, action) => {
      state.teachers = state.teachers.filter((t) => t.id !== action.payload);
    },
    resetAdmin: () => initialState,
  },
});

export const { setTeachers, setLoading, upsertTeacher, removeTeacher, resetAdmin } =
  adminSlice.actions;

export default adminSlice.reducer;
