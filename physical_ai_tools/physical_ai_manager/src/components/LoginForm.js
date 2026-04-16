import React, { useState } from 'react';
import clsx from 'clsx';
import toast from 'react-hot-toast';
import { useDispatch } from 'react-redux';
import { supabase } from '../lib/supabaseClient';
import { setSession, setIsLoading } from '../features/auth/authSlice';
import { usernameToEmail } from '../constants/appMode';

export default function LoginForm({ subtitle = 'Anmelden fuer Cloud-GPU-Training' }) {
  const dispatch = useDispatch();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e) => {
    e.preventDefault();
    setLoading(true);

    try {
      const email = usernameToEmail(username);
      const { data, error } = await supabase.auth.signInWithPassword({
        email,
        password,
      });
      if (error) throw error;
      dispatch(setSession(data.session));
      toast.success('Erfolgreich angemeldet!');
    } catch (error) {
      // Supabase returns "Invalid login credentials" — translate for students.
      const msg = error?.message || '';
      if (msg.toLowerCase().includes('invalid login')) {
        toast.error('Benutzername oder Passwort falsch');
      } else {
        toast.error(msg || 'Anmeldung fehlgeschlagen');
      }
    } finally {
      setLoading(false);
      dispatch(setIsLoading(false));
    }
  };

  const classCard = clsx(
    'bg-white',
    'border',
    'border-gray-200',
    'rounded-2xl',
    'shadow-lg',
    'p-8',
    'w-full',
    'max-w-sm'
  );

  const classInput = clsx(
    'w-full',
    'px-4',
    'py-3',
    'border',
    'border-gray-300',
    'rounded-lg',
    'focus:outline-none',
    'focus:ring-2',
    'focus:ring-teal-500',
    'focus:border-transparent',
    'text-base'
  );

  const classButton = clsx(
    'w-full',
    'px-4',
    'py-3',
    'bg-teal-600',
    'text-white',
    'rounded-lg',
    'font-semibold',
    'text-base',
    'transition-colors',
    'hover:bg-teal-700',
    'disabled:bg-gray-400',
    'disabled:cursor-not-allowed'
  );

  return (
    <div className="flex flex-col items-center justify-center h-full">
      <div className={classCard}>
        <h2 className="text-2xl font-bold text-gray-800 text-center mb-2">
          EduBotics
        </h2>
        <p className="text-sm text-gray-500 text-center mb-6">{subtitle}</p>

        <form onSubmit={handleSubmit} className="flex flex-col gap-4">
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">
              Benutzername
            </label>
            <input
              type="text"
              className={classInput}
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              placeholder="max.mustermann"
              autoComplete="username"
              required
              minLength={3}
              maxLength={32}
            />
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">
              Passwort
            </label>
            <input
              type="password"
              className={classInput}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="Passwort eingeben"
              autoComplete="current-password"
              required
              minLength={6}
            />
          </div>

          <button type="submit" className={classButton} disabled={loading}>
            {loading ? 'Bitte warten...' : 'Anmelden'}
          </button>
        </form>

        <p className="text-xs text-gray-400 text-center mt-6">
          Konto vergessen? Frage deinen Lehrer.
        </p>
      </div>
    </div>
  );
}
