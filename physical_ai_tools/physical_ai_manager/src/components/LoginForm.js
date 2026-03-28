import React, { useState } from 'react';
import clsx from 'clsx';
import toast from 'react-hot-toast';
import { useDispatch } from 'react-redux';
import { supabase } from '../lib/supabaseClient';
import { setSession, setIsLoading } from '../features/auth/authSlice';

export default function LoginForm() {
  const dispatch = useDispatch();
  const [isSignUp, setIsSignUp] = useState(false);
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e) => {
    e.preventDefault();
    setLoading(true);

    try {
      if (isSignUp) {
        const { data, error } = await supabase.auth.signUp({
          email,
          password,
        });
        if (error) throw error;
        if (data.session) {
          dispatch(setSession(data.session));
          toast.success('Konto erfolgreich erstellt!');
        } else {
          toast.success('Konto erstellt! E-Mail zur Bestätigung prüfen.');
        }
      } else {
        const { data, error } = await supabase.auth.signInWithPassword({
          email,
          password,
        });
        if (error) throw error;
        dispatch(setSession(data.session));
        toast.success('Erfolgreich angemeldet!');
      }
    } catch (error) {
      toast.error(error.message);
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
          Cloud-Training
        </h2>
        <p className="text-sm text-gray-500 text-center mb-6">
          Anmelden für Cloud-GPU-Training
        </p>

        {/* Tab switcher */}
        <div className="flex mb-6 bg-gray-100 rounded-lg p-1">
          <button
            className={clsx(
              'flex-1 py-2 rounded-md text-sm font-medium transition-colors',
              !isSignUp ? 'bg-white text-gray-800 shadow-sm' : 'text-gray-500'
            )}
            onClick={() => setIsSignUp(false)}
          >
            Anmelden
          </button>
          <button
            className={clsx(
              'flex-1 py-2 rounded-md text-sm font-medium transition-colors',
              isSignUp ? 'bg-white text-gray-800 shadow-sm' : 'text-gray-500'
            )}
            onClick={() => setIsSignUp(true)}
          >
            Registrieren
          </button>
        </div>

        <form onSubmit={handleSubmit} className="flex flex-col gap-4">
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">E-Mail</label>
            <input
              type="email"
              className={classInput}
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="student@example.com"
              required
            />
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Passwort</label>
            <input
              type="password"
              className={classInput}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="Passwort eingeben"
              required
              minLength={6}
            />
          </div>

          <button type="submit" className={classButton} disabled={loading}>
            {loading ? 'Bitte warten...' : isSignUp ? 'Konto erstellen' : 'Anmelden'}
          </button>
        </form>
      </div>
    </div>
  );
}
