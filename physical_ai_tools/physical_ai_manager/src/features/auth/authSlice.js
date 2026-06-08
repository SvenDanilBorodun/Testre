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
  // Workgroup membership for the current user (students only). Set after
  // /me. NULL when user is not in a group; in that case credit/visibility
  // logic falls back to the per-user path. workgroupName is shown in chips.
  workgroupId: null,
  workgroupName: null,
  // The student's linked HuggingFace "Benutzer-ID" (from /me.hf_username).
  // Null until the cloud profile has been linked to the HF identity. The
  // Training tab links it automatically once both the cloud login and the
  // ROS whoami Benutzer-ID are known (StudentApp auto-link effect).
  hfUsername: null,
  profileLoaded: false,
  // Set when GET /me fails in a NON-auth way (404 = JWT valid but no
  // public.users row; network/5xx after retries). profileLoaded stays
  // false; the Training/Inferenz gates render a German error card with a
  // retry button instead of the workspace. Cleared on a successful load
  // and on session change.
  profileError: null,
  // Bumped by requestProfileRefetch() (e.g. the Training tab "Erneut
  // versuchen" button). The single useMeProfile owner (StudentApp/WebApp)
  // watches this and re-runs GET /me. A nonce instead of a thunk keeps the
  // fetch in ONE place — no dual hooks, no stale closures, no double toast.
  profileRefetchNonce: 0,
  // Only populated for teachers.
  poolTotal: null,
  allocatedTotal: null,
  poolAvailable: null,
  studentCount: null,
  groupCount: null,
  groupCreditsTotal: null,
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
        state.workgroupId = null;
        state.workgroupName = null;
        state.hfUsername = null;
        state.profileLoaded = false;
        state.profileError = null;
        state.poolTotal = null;
        state.allocatedTotal = null;
        state.poolAvailable = null;
        state.studentCount = null;
        state.groupCount = null;
        state.groupCreditsTotal = null;
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
      state.workgroupId = null;
      state.workgroupName = null;
      state.hfUsername = null;
      state.profileLoaded = false;
      state.profileError = null;
      state.poolTotal = null;
      state.allocatedTotal = null;
      state.poolAvailable = null;
      state.studentCount = null;
      state.groupCount = null;
      state.groupCreditsTotal = null;
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
      state.workgroupId = p.workgroup_id ?? null;
      state.workgroupName = p.workgroup_name ?? null;
      state.hfUsername = p.hf_username ?? null;
      state.profileLoaded = true;
      state.profileError = null;
      if (p.role === 'teacher') {
        state.poolTotal = p.pool_total;
        state.allocatedTotal = p.allocated_total;
        state.poolAvailable = p.pool_available;
        state.studentCount = p.student_count;
        state.groupCount = p.group_count ?? 0;
        state.groupCreditsTotal = p.group_credits_total ?? 0;
      }
    },
    // Update just the linked HF Benutzer-ID after a successful PATCH /me
    // (the StudentApp auto-link effect). Kept separate from setProfile so
    // the link can be reflected without re-applying the whole profile.
    setHfUsername: (state, action) => {
      state.hfUsername = action.payload ?? null;
    },
    // Surface a profile-load failure to the UI gates without signing out
    // (a valid JWT can still hit a missing public.users row, or a 5xx).
    // Passing null clears it (e.g. just before a manual refetch).
    setProfileError: (state, action) => {
      state.profileError = action.payload ?? null;
    },
    // Ask the single useMeProfile owner to re-run GET /me. The "Erneut
    // versuchen" buttons on the Training/Inferenz error cards dispatch this.
    requestProfileRefetch: (state) => {
      state.profileRefetchNonce += 1;
    },
    updateTeacherPool: (state, action) => {
      // Called when a teacher adjusts credits and we want a fresh pool total.
      const p = action.payload;
      if (p.pool_total !== undefined) state.poolTotal = p.pool_total;
      if (p.allocated_total !== undefined) state.allocatedTotal = p.allocated_total;
      if (p.pool_available !== undefined) state.poolAvailable = p.pool_available;
      if (p.student_count !== undefined) state.studentCount = p.student_count;
      if (p.group_count !== undefined) state.groupCount = p.group_count;
      if (p.group_credits_total !== undefined) state.groupCreditsTotal = p.group_credits_total;
    },
  },
});

export const {
  setSession,
  clearSession,
  setIsLoading,
  setQuota,
  setProfile,
  setHfUsername,
  setProfileError,
  requestProfileRefetch,
  updateTeacherPool,
} = authSlice.actions;

export default authSlice.reducer;
