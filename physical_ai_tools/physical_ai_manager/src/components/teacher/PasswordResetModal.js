import React, { useState } from 'react';
import toast from 'react-hot-toast';
import Modal from './Modal';
import { Btn } from '../EbUI';

const inputClass =
  'w-full h-10 px-3 bg-white border border-[var(--line)] rounded-[var(--radius-sm)] text-sm text-[var(--ink)] placeholder:text-[var(--ink-4)] focus:outline-none focus:border-[var(--accent)] focus:ring-2 focus:ring-[color:var(--accent-wash)] transition';

export default function PasswordResetModal({ onClose, onSubmit, username }) {
  const [password, setPassword] = useState('');
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (password.length < 6) return;
    setLoading(true);
    try {
      await onSubmit(password);
      toast.success('Passwort zurückgesetzt');
      onClose();
    } catch (err) {
      toast.error(err.message || 'Fehler');
    } finally {
      setLoading(false);
    }
  };

  return (
    <Modal
      title="Passwort zurücksetzen"
      onClose={onClose}
      footer={
        <>
          <Btn variant="ghost" onClick={onClose} disabled={loading}>
            Abbrechen
          </Btn>
          <Btn
            variant="primary"
            type="submit"
            form="reset-password-form"
            disabled={loading || password.length < 6}
          >
            {loading ? 'Setzen…' : 'Setzen'}
          </Btn>
        </>
      }
    >
      <form id="reset-password-form" onSubmit={handleSubmit} className="flex flex-col gap-3">
        <p className="text-sm text-[var(--ink-2)]">
          Neues Passwort für{' '}
          <span className="font-mono font-semibold text-[var(--ink)]">{username}</span>:
        </p>
        <input
          type="text"
          className={`${inputClass} font-mono`}
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="mindestens 6 Zeichen"
          minLength={6}
          autoFocus
          required
        />
        <p className="text-[11px] text-[var(--ink-3)] leading-snug">
          Gib das neue Passwort dem Schüler weiter.
        </p>
      </form>
    </Modal>
  );
}
