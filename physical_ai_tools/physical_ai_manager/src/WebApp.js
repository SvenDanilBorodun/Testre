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
    supabase.auth.getSession().then(({ data: { session } }) => {
      dispatch(setSession(session));
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
        console.error('getMe failed', err);
        toast.error('Profil konnte nicht geladen werden');
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
      <div className="flex items-center justify-center min-h-screen bg-gray-50">
        <div className="text-gray-500 text-lg">Laden...</div>
      </div>
    );
  }

  if (!isAuthenticated) {
    return (
      <div className="min-h-screen bg-gray-50 flex items-center justify-center">
        <LoginForm subtitle="Anmelden fuer Lehrer / Admin" />
      </div>
    );
  }

  if (!profileLoaded) {
    return (
      <div className="flex items-center justify-center min-h-screen bg-gray-50">
        <div className="text-gray-500 text-lg">Profil wird geladen...</div>
      </div>
    );
  }

  if (role === 'admin') {
    return <AdminDashboard onLogout={handleLogout} />;
  }
  if (role === 'teacher') {
    return <TeacherDashboard onLogout={handleLogout} />;
  }

  // Fallback — profile loaded but role wasn't teacher/admin (and wasn't student, which we caught above)
  return (
    <div className="min-h-screen bg-gray-50 flex items-center justify-center">
      <div className="bg-white rounded-2xl shadow-lg p-8 max-w-md text-center">
        <h2 className="text-xl font-bold text-gray-800 mb-2">Keine Berechtigung</h2>
        <p className="text-gray-600 mb-4">
          Dieses Konto hat keine Rolle zugewiesen. Bitte wende dich an den Admin.
        </p>
        <button
          onClick={handleLogout}
          className="px-4 py-2 bg-teal-600 text-white rounded-lg hover:bg-teal-700"
        >
          Abmelden
        </button>
      </div>
    </div>
  );
}

export default WebApp;
