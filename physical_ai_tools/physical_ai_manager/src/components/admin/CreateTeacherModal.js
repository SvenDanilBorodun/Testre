import React, { useState } from 'react';
import toast from 'react-hot-toast';
import Modal from '../teacher/Modal';
import { Btn } from '../EbUI';

const inputClass =
  'w-full h-10 px-3 bg-white border border-[var(--line)] rounded-[var(--radius-sm)] text-sm text-[var(--ink)] placeholder:text-[var(--ink-4)] focus:outline-none focus:border-[var(--accent)] focus:ring-2 focus:ring-[color:var(--accent-wash)] transition';

export default function CreateTeacherModal({ onClose, onSubmit }) {
  const [username, setUsername] = useState('');
  const [fullName, setFullName] = useState('');
  const [password, setPassword] = useState('');
  const [credits, setCredits] = useState(0);
  const [loading, setLoading] = useState(false);

  const usernameValid = /^[a-z0-9][a-z0-9._-]{2,31}$/.test(username);

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!usernameValid) {
      toast.error('Benutzername: 3–32 Zeichen, Kleinbuchstaben, Ziffern, . _ -');
      return;
    }
    if (password.length < 6) {
      toast.error('Passwort mindestens 6 Zeichen');
      return;
    }
    setLoading(true);
    try {
      await onSubmit({
        username,
        full_name: fullName,
        password,
        credits: Number(credits) || 0,
      });
      toast.success('Lehrer erstellt');
      onClose();
    } catch (err) {
      toast.error(err.message || 'Fehler');
    } finally {
      setLoading(false);
    }
  };

  return (
    <Modal
      title="Neuen Lehrer erstellen"
      onClose={onClose}
      footer={
        <>
          <Btn variant="ghost" onClick={onClose} disabled={loading}>
            Abbrechen
          </Btn>
          <Btn
            variant="primary"
            type="submit"
            form="create-teacher-form"
            disabled={loading || !usernameValid || !fullName || password.length < 6}
          >
            {loading ? 'Erstellen…' : 'Erstellen'}
          </Btn>
        </>
      }
    >
      <form id="create-teacher-form" onSubmit={handleSubmit} className="flex flex-col gap-4">
        <label className="block">
          <span className="text-xs font-medium text-[var(--ink-2)] mb-1.5 block">
            Vollständiger Name
          </span>
          <input
            type="text"
            className={inputClass}
            value={fullName}
            onChange={(e) => setFullName(e.target.value)}
            maxLength={100}
            autoFocus
            required
          />
        </label>
        <label className="block">
          <span className="text-xs font-medium text-[var(--ink-2)] mb-1.5 block">
            Benutzername (für Login)
          </span>
          <input
            type="text"
            className={inputClass}
            value={username}
            onChange={(e) => setUsername(e.target.value.toLowerCase())}
            placeholder="frau.mustermann"
            maxLength={32}
            required
          />
          <p className="text-[11px] text-[var(--ink-3)] mt-1.5 leading-snug">
            3–32 Zeichen: Kleinbuchstaben, Ziffern, . _ -
          </p>
        </label>
        <label className="block">
          <span className="text-xs font-medium text-[var(--ink-2)] mb-1.5 block">
            Start-Passwort
          </span>
          <input
            type="text"
            className={`${inputClass} font-mono`}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            minLength={6}
            required
          />
        </label>
        <label className="block">
          <span className="text-xs font-medium text-[var(--ink-2)] mb-1.5 block">
            Start-Credits (Pool)
          </span>
          <input
            type="number"
            className={inputClass}
            value={credits}
            onChange={(e) => setCredits(e.target.value)}
            min={0}
            max={10000}
          />
          <p className="text-[11px] text-[var(--ink-3)] mt-1.5 leading-snug">
            Credits die der Lehrer an Schüler verteilen kann.
          </p>
        </label>
      </form>
    </Modal>
  );
}
