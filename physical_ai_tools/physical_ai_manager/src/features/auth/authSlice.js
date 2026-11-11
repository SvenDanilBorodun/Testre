import { createSlice } from '@reduxjs/toolkit';
import { signedOut } from '../session/sessionActions';

const initialState = {
  session: null,
  isAuthenticated: false,
  isLoading: true,
  // Legacy quota fields (an older API's derived credits). The pages read the
  // training-hour fields below.
  trainingCredits: 0,
  trainingsUsed: 0,
  // Training hours from GET /trainings/quota (credit system v2, spec §8.2).
  // `null` = not asked yet, never 0: the Start key stays off until it is known.
  hoursFree: null,
  hoursReserved: null,
  hoursSpent: null,
  nextReservationHours: null,
  maxHoursPerTraining: null,
  account: null, // 'student' | 'group' | null
  memberLimitHours: null,
  memberLeftHours: null,
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
  // The student's HuggingFace account name (from /me.hf_username). It is PROVEN:
  // the cloud sets it from the stored token's own whoami when the student saves
  // the token on the Startseite (PUT /me/hf-token). Null until a token has been
  // stored, and again after sign-out.
  hfUsername: null,
  // GDPR Art. 17 (launch plan step 5): the caller's open deletion request and its
  // Art. 12(3) deadline, from /me (ISO timestamps), null when none is open. The
  // student SPA shows them (components/DeletionRequestNotice.jsx).
  deletionRequestedAt: null,
  deletionDueBy: null,
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
  // Σ of the admin's compensations into this teacher's student/group accounts.
  compensated: null,
};

/** The student's training-hour quota fields, reset by `session/signedOut`. */
function clearQuotaFields(state) {
  state.trainingCredits = 0;
  state.trainingsUsed = 0;
  state.hoursFree = null;
  state.hoursReserved = null;
  state.hoursSpent = null;
  state.nextReservationHours = null;
  state.maxHoursPerTraining = null;
  state.account = null;
  state.memberLimitHours = null;
  state.memberLeftHours = null;
}

const intOrNull = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null);

/** Every profile field derived from /me. Shared by the two paths that drop a
 *  session — `setSession(null)` (supabase's own auth-state change) and the
 *  `session/signedOut` broadcast — so the two can never drift apart. */
function clearProfileFields(state) {
  state.role = null;
  state.username = null;
  state.fullName = null;
  state.classroomId = null;
  state.workgroupId = null;
  state.workgroupName = null;
  state.hfUsername = null;
  state.deletionRequestedAt = null;
  state.deletionDueBy = null;
  state.profileLoaded = false;
  state.profileError = null;
  state.poolTotal = null;
  state.allocatedTotal = null;
  state.poolAvailable = null;
  state.studentCount = null;
  state.groupCount = null;
  state.groupCreditsTotal = null;
  state.compensated = null;
}

const authSlice = createSlice({
  name: 'auth',
  initialState,
  reducers: {
    setSession: (state, action) => {
      state.session = action.payload;
      state.isAuthenticated = !!action.payload;
      if (!action.payload) {
        clearProfileFields(state);
      }
    },
    setIsLoading: (state, action) => {
      state.isLoading = action.payload;
    },
    setQuota: (state, action) => {
      const q = action.payload || {};
      state.trainingCredits = q.training_credits;
      state.trainingsUsed = q.trainings_used;
      state.hoursFree = intOrNull(q.hours_free);
      state.hoursReserved = intOrNull(q.hours_reserved);
      state.hoursSpent = intOrNull(q.hours_spent);
      state.nextReservationHours = intOrNull(q.next_reservation_hours);
      state.maxHoursPerTraining = intOrNull(q.max_hours_per_training);
      state.account = q.account === 'student' || q.account === 'group' ? q.account : null;
      state.memberLimitHours = intOrNull(q.member_limit_hours);
      state.memberLeftHours = intOrNull(q.member_left_hours);
      // The quota names the group whose hours fund the student; an answer
      // without the key (an older API) keeps what /me said.
      if (q.workgroup_name !== undefined) state.workgroupName = q.workgroup_name ?? null;
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
      state.deletionRequestedAt = typeof p.deletion_requested_at === 'string' ? p.deletion_requested_at : null;
      state.deletionDueBy = typeof p.deletion_due_by === 'string' ? p.deletion_due_by : null;
      state.profileLoaded = true;
      state.profileError = null;
      if (p.role === 'teacher') {
        state.poolTotal = p.pool_total;
        state.allocatedTotal = p.allocated_total;
        state.poolAvailable = p.pool_available;
        state.studentCount = p.student_count;
        state.groupCount = p.group_count ?? 0;
        state.groupCreditsTotal = p.group_credits_total ?? 0;
        state.compensated = intOrNull(p.compensated);
      }
    },
    // Update just the proven HF account name after a successful PUT
    // /me/hf-token (features/hfToken/hfTokenThunks::saveHfToken). Kept separate
    // from setProfile so the change can be reflected without re-applying the
    // whole profile.
    setHfUsername: (state, action) => {
      state.hfUsername = action.payload ?? null;
    },
    // After POST /me/delete/withdraw: the request is no longer open.
    clearDeletionRequest: (state) => {
      state.deletionRequestedAt = null;
      state.deletionDueBy = null;
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
      if (p.compensated !== undefined) state.compensated = intOrNull(p.compensated);
    },
  },
  extraReducers: (builder) => {
    // The session itself. This used to be a separate `clearSession` action the
    // sign-out helper dispatched next to the four slice resets; folding it into
    // the broadcast leaves exactly one way to end a session.
    builder.addCase(signedOut, (state) => {
      state.session = null;
      state.isAuthenticated = false;
      // The hour balance is the STUDENT's, not the machine's — and unlike the
      // fields above it is not reset by `setSession(null)`, which is why the
      // two paths are not the same function.
      clearQuotaFields(state);
      clearProfileFields(state);
    });
  },
});

export const {
  setSession,
  setIsLoading,
  setQuota,
  setProfile,
  setHfUsername,
  clearDeletionRequest,
  setProfileError,
  requestProfileRefetch,
  updateTeacherPool,
} = authSlice.actions;

export default authSlice.reducer;
