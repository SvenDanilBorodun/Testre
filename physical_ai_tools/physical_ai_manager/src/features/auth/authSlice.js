import { createSlice } from '@reduxjs/toolkit';

const initialState = {
  session: null,
  isAuthenticated: false,
  isLoading: true,
  trainingCredits: 0,
  trainingsUsed: 0,
  // Profile info fetched from /me after session is established.
  role: null, // 'admin' | 'teacher' | 'student' | null
  username: null,
  fullName: null,
  classroomId: null,
  profileLoaded: false,
  // Only populated for teachers.
  poolTotal: null,
  allocatedTotal: null,
  poolAvailable: null,
  studentCount: null,
};

const authSlice = createSlice({
  name: 'auth',
  initialState,
  reducers: {
    setSession: (state, action) => {
      state.session = action.payload;
      state.isAuthenticated = !!action.payload;
      if (!action.payload) {
        state.role = null;
        state.username = null;
        state.fullName = null;
        state.classroomId = null;
        state.profileLoaded = false;
        state.poolTotal = null;
        state.allocatedTotal = null;
        state.poolAvailable = null;
        state.studentCount = null;
      }
    },
    clearSession: (state) => {
      state.session = null;
      state.isAuthenticated = false;
      state.trainingCredits = 0;
      state.trainingsUsed = 0;
      state.role = null;
      state.username = null;
      state.fullName = null;
      state.classroomId = null;
      state.profileLoaded = false;
      state.poolTotal = null;
      state.allocatedTotal = null;
      state.poolAvailable = null;
      state.studentCount = null;
    },
    setIsLoading: (state, action) => {
      state.isLoading = action.payload;
    },
    setQuota: (state, action) => {
      state.trainingCredits = action.payload.training_credits;
      state.trainingsUsed = action.payload.trainings_used;
    },
    setProfile: (state, action) => {
      const p = action.payload;
      state.role = p.role;
      state.username = p.username;
      state.fullName = p.full_name;
      state.classroomId = p.classroom_id;
      state.profileLoaded = true;
      if (p.role === 'teacher') {
        state.poolTotal = p.pool_total;
        state.allocatedTotal = p.allocated_total;
        state.poolAvailable = p.pool_available;
        state.studentCount = p.student_count;
      }
    },
    updateTeacherPool: (state, action) => {
      // Called when a teacher adjusts credits and we want a fresh pool total.
      const p = action.payload;
      if (p.pool_total !== undefined) state.poolTotal = p.pool_total;
      if (p.allocated_total !== undefined) state.allocatedTotal = p.allocated_total;
      if (p.pool_available !== undefined) state.poolAvailable = p.pool_available;
      if (p.student_count !== undefined) state.studentCount = p.student_count;
    },
  },
});

export const {
  setSession,
  clearSession,
  setIsLoading,
  setQuota,
  setProfile,
  updateTeacherPool,
} = authSlice.actions;

export default authSlice.reducer;
