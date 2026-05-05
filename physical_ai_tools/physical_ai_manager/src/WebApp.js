// WebApp — public web deployment for teachers and admin.
// No ROS, no robot pages — just login + role-based dashboard.

import React, { useEffect } from 'react';
import toast from 'react-hot-toast';
import { useDispatch, useSelector } from 'react-redux';
import { supabase } from './lib/supabaseClient';
import {
  setSession,
  setIsLoading,
  setProfile,
  clearSession,
} from './features/auth/authSlice';
import { getMe } from './services/meApi';
import LoginForm from './components/LoginForm';
import TeacherDashboard from './pages/teacher/TeacherDashboard';
import AdminDashboard from './pages/admin/AdminDashboard';

function WebApp() {
  const dispatch = useDispatch();
  const isLoading = useSelector((s) => s.auth.isLoading);
  const isAuthenticated = useSelector((s) => s.auth.isAuthenticated);
  const session = useSelector((s) => s.auth.session);
  const role = useSelector((s) => s.auth.role);
  const profileLoaded = useSelector((s) => s.auth.profileLoaded);

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

  useEffect(() => {
    if (!session?.access_token) return;
    let alive = true;
    getMe(session.access_token)
      .then((me) => {
        if (!alive) return;
        dispatch(setProfile(me));
        if (me.role === 'student') {
          toast.error(
            'Schueler-Konten koennen die Web-App nicht nutzen. Bitte oeffne die Desktop-App.',
            { duration: 6000 }
          );
          supabase.auth.signOut();
          dispatch(clearSession());
        }
      })
      .catch((err) => {
        if (!alive) return;
        console.error('getMe failed', err);
        // 401/403 => token dead; sign out so the login form returns.
        // Network/5xx => keep the session, just surface the problem.
        const status = err?.status ?? err?.response?.status;
        if (status === 401 || status === 403) {
          toast.error('Sitzung abgelaufen — bitte erneut anmelden.');
          supabase.auth.signOut();
          dispatch(clearSession());
        } else {
          toast.error('Profil konnte nicht geladen werden — Server erreichbar?');
        }
      });
    return () => {
      alive = false;
    };
  }, [session?.access_token, dispatch]);

  const handleLogout = async () => {
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
