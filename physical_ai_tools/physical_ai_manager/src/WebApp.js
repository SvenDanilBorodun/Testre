// WebApp — public web deployment for teachers and admin.
// No ROS, no robot pages — just login + role-based dashboard.

import React, { useCallback, useEffect } from 'react';
import toast from 'react-hot-toast';
import { useDispatch, useSelector } from 'react-redux';
import { supabase } from './lib/supabaseClient';
import {
  setSession,
  setIsLoading,
  clearSession,
  requestProfileRefetch,
} from './features/auth/authSlice';
import { useMeProfile } from './hooks/useMeProfile';
import LoginForm from './components/LoginForm';
import TeacherDashboard from './pages/teacher/TeacherDashboard';
import AdminDashboard from './pages/admin/AdminDashboard';
import { resetJetsonOnLogout } from './hooks/useJetsonConnection';

function WebApp() {
  const dispatch = useDispatch();
  const isLoading = useSelector((s) => s.auth.isLoading);
  const isAuthenticated = useSelector((s) => s.auth.isAuthenticated);
  const role = useSelector((s) => s.auth.role);
  const profileLoaded = useSelector((s) => s.auth.profileLoaded);
  const profileError = useSelector((s) => s.auth.profileError);

  useEffect(() => {
    supabase.auth
      .getSession()
      .then(({ data: { session } }) => {
        dispatch(setSession(session));
      })
      .catch((err) => {
        console.error('supabase.getSession failed', err);
        toast.error('Anmeldedienst nicht erreichbar — bitte Seite neu laden.');
      })
      .finally(() => {
        dispatch(setIsLoading(false));
      });
    const {
      data: { subscription },
    } = supabase.auth.onAuthStateChange((_event, session) => {
      dispatch(setSession(session));
    });
    return () => subscription.unsubscribe();
  }, [dispatch]);

  // Shared robust /me loader: retry/backoff, 401/403 → sign-out, 404/5xx →
  // profileError (no sign-out). Teacher web doesn't link an HF identity, so
  // enableHfLink stays off. The only web-specific piece is bouncing a
  // wrong-role (student) account back to the desktop app.
  const handleProfile = useCallback(
    (me) => {
      if (me.role === 'student') {
        toast.error(
          'Schüler-Konten können die Web-App nicht nutzen. Bitte öffne die Desktop-App.',
          { duration: 6000 }
        );
        // v2.3.0: teacher web doesn't claim Jetsons (only the student app
        // does), so the beacon-release-on-logout step is a no-op here — but
        // we still clear the slice for symmetry and reset the rosbridge auth.
        resetJetsonOnLogout(dispatch);
        supabase.auth.signOut();
        dispatch(clearSession());
      }
    },
    [dispatch]
  );
  useMeProfile({ onProfile: handleProfile });

  const handleLogout = async () => {
    // v2.3.0: clear Jetson-side state on the way out so a re-login in
    // the same tab starts cleanly. Teacher app doesn't hold Jetson
    // locks (students do), so no beacon release is needed.
    resetJetsonOnLogout(dispatch);
    await supabase.auth.signOut();
    dispatch(clearSession());
    toast.success('Abgemeldet');
  };

  if (isLoading) {
    return (
      <div
        className="flex items-center justify-center min-h-screen"
        style={{ background: 'var(--bg)' }}
      >
        <div className="text-[var(--ink-3)] text-lg">Laden…</div>
      </div>
    );
  }

  if (!isAuthenticated) {
    return (
      <div
        className="min-h-screen flex items-center justify-center"
        style={{ background: 'var(--bg)' }}
      >
        <LoginForm subtitle="Anmelden für Lehrer / Admin" />
      </div>
    );
  }

  if (profileError) {
    return (
      <div
        className="min-h-screen flex items-center justify-center px-4"
        style={{ background: 'var(--bg)' }}
      >
        <div className="bg-white rounded-[var(--radius-lg)] shadow-pop border border-[var(--line)] p-8 max-w-md text-center">
          <h2 className="text-xl font-semibold tracking-tight text-[var(--ink)] mb-2">
            Profil konnte nicht geladen werden
          </h2>
          <p className="text-[var(--ink-3)] mb-5">{profileError}</p>
          <div className="flex items-center justify-center gap-3">
            <button
              onClick={() => dispatch(requestProfileRefetch())}
              className="h-10 px-4 bg-[var(--accent)] text-white rounded-[var(--radius-sm)] hover:brightness-110 transition"
            >
              Erneut versuchen
            </button>
            <button
              onClick={handleLogout}
              className="h-10 px-4 bg-white border border-[var(--line)] text-[var(--ink)] rounded-[var(--radius-sm)] hover:bg-[var(--bg-sunk)] transition"
            >
              Abmelden
            </button>
          </div>
        </div>
      </div>
    );
  }

  if (!profileLoaded) {
    return (
      <div
        className="flex items-center justify-center min-h-screen"
        style={{ background: 'var(--bg)' }}
      >
        <div className="text-[var(--ink-3)] text-lg">Profil wird geladen…</div>
      </div>
    );
  }

  if (role === 'admin') {
    return <AdminDashboard onLogout={handleLogout} />;
  }
  if (role === 'teacher') {
    return <TeacherDashboard onLogout={handleLogout} />;
  }

  return (
    <div
      className="min-h-screen flex items-center justify-center"
      style={{ background: 'var(--bg)' }}
    >
      <div className="bg-white rounded-[var(--radius-lg)] shadow-pop border border-[var(--line)] p-8 max-w-md text-center">
        <h2 className="text-xl font-semibold tracking-tight text-[var(--ink)] mb-2">
          Keine Berechtigung
        </h2>
        <p className="text-[var(--ink-3)] mb-4">
          Dieses Konto hat keine Rolle zugewiesen. Bitte wende dich an den Admin.
        </p>
        <button
          onClick={handleLogout}
          className="h-10 px-4 bg-[var(--accent)] text-white rounded-[var(--radius-sm)] hover:brightness-110 transition"
        >
          Abmelden
        </button>
      </div>
    </div>
  );
}

export default WebApp;
