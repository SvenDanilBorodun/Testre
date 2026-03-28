import { createSlice } from '@reduxjs/toolkit';

const initialState = {
  session: null,
  isAuthenticated: false,
  isLoading: true,
  trainingCredits: 0,
  trainingsUsed: 0,
};

const authSlice = createSlice({
  name: 'auth',
  initialState,
  reducers: {
    setSession: (state, action) => {
      state.session = action.payload;
      state.isAuthenticated = !!action.payload;
    },
    clearSession: (state) => {
      state.session = null;
      state.isAuthenticated = false;
      state.trainingCredits = 0;
      state.trainingsUsed = 0;
    },
    setIsLoading: (state, action) => {
      state.isLoading = action.payload;
    },
    setQuota: (state, action) => {
      state.trainingCredits = action.payload.training_credits;
      state.trainingsUsed = action.payload.trainings_used;
    },
  },
});

export const { setSession, clearSession, setIsLoading, setQuota } = authSlice.actions;

export default authSlice.reducer;
