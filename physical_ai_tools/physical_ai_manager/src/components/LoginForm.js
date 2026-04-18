import React, { useState } from 'react';
import toast from 'react-hot-toast';
import { useDispatch } from 'react-redux';
import { supabase } from '../lib/supabaseClient';
import { setSession, setIsLoading } from '../features/auth/authSlice';
import { usernameToEmail } from '../constants/appMode';
import { Btn, LogoMark } from './EbUI';
import packageJson from '../../package.json';

export default function LoginForm({ subtitle = 'Anmelden für Cloud-GPU-Training' }) {
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

  const inputClass =
    'w-full h-11 px-3 bg-white border border-[var(--line)] rounded-[var(--radius-sm)] text-sm text-[var(--ink)] placeholder:text-[var(--ink-4)] focus:outline-none focus:border-[var(--accent)] focus:ring-2 focus:ring-[color:var(--accent-wash)] transition';

  return (
    <div className="h-full w-full flex items-center justify-center grid-dot">
      <div className="flex flex-col items-center gap-6">
        <div className="flex items-center gap-2">
          <LogoMark />
          <span className="font-semibold text-[15px] tracking-tight text-[var(--ink)]">
            EduBotics
          </span>
        </div>
        <div className="w-[380px] bg-white border border-[var(--line)] rounded-[var(--radius-lg)] shadow-pop p-7">
          <h1 className="text-[22px] font-semibold tracking-tight text-[var(--ink)]">
            Willkommen zurück
          </h1>
          <p className="text-sm text-[var(--ink-3)] mt-1">{subtitle}</p>

          <form onSubmit={handleSubmit} className="mt-6 flex flex-col gap-3">
            <label className="block">
              <span className="text-xs font-medium text-[var(--ink-2)] mb-1.5 block">
                Benutzername
              </span>
              <input
                type="text"
                className={inputClass}
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                placeholder="max.mustermann"
                autoComplete="username"
                required
                minLength={3}
                maxLength={32}
              />
            </label>

            <label className="block">
              <span className="text-xs font-medium text-[var(--ink-2)] mb-1.5 block">
                Passwort
              </span>
              <input
                type="password"
                className={inputClass}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="Passwort eingeben"
                autoComplete="current-password"
                required
                minLength={6}
              />
            </label>

            <Btn
              variant="primary"
              size="lg"
              type="submit"
              className="w-full justify-center mt-2"
              disabled={loading}
            >
              {loading ? 'Bitte warten…' : 'Anmelden'}
            </Btn>
          </form>

          <div className="mt-5 pt-4 border-t border-[var(--line)] flex items-center justify-between text-xs text-[var(--ink-3)]">
            <span>Konto vergessen? Frage deinen Lehrer.</span>
            <span className="font-mono">v{packageJson.version}</span>
          </div>
        </div>
        <div className="text-[11px] text-[var(--ink-3)] font-mono">
          edubotics.local · gesicherte Verbindung
        </div>
      </div>
    </div>
  );
}
