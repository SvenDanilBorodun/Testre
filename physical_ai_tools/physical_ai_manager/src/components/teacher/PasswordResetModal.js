import React, { useState } from 'react';
import toast from 'react-hot-toast';
import Modal from './Modal';
import { Btn } from '../EbUI';
import {
  PASSWORD_MAX,
  passwordErrorDe,
  passwordHintDe,
  passwordMinLength,
} from '../../utils/passwordRules';

const inputClass =
  'w-full h-10 px-3 bg-white border border-[var(--line)] rounded-[var(--radius-sm)] text-sm text-[var(--ink)] placeholder:text-[var(--ink-4)] focus:outline-none focus:border-[var(--accent)] focus:ring-2 focus:ring-[color:var(--accent-wash)] transition';

/**
 * @param {object} props
 * @param {'student'|'staff'} [props.audience] whose password is reset: a
 *   teacher resets a student's (8 characters), an admin a teacher's (12; owner
 *   decision 2026-11-10, utils/passwordRules.js).
 */
export default function PasswordResetModal({ onClose, onSubmit, username, audience = 'student' }) {
  const [password, setPassword] = useState('');
  const [loading, setLoading] = useState(false);
  const passwordError = passwordErrorDe(password, audience);

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (passwordError) {
      toast.error(passwordError);
      return;
    }
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
            disabled={loading || passwordError !== null}
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
          placeholder={`mindestens ${passwordMinLength(audience)} Zeichen`}
          minLength={passwordMinLength(audience)}
          maxLength={PASSWORD_MAX}
          aria-describedby="reset-password-hint"
          autoFocus
          required
        />
        <p id="reset-password-hint" className="text-[11px] text-[var(--ink-3)] leading-snug">
          {passwordHintDe(audience)}{' '}
          {audience === 'staff'
            ? 'Gib das neue Passwort der Lehrkraft weiter.'
            : 'Gib das neue Passwort dem Schüler weiter.'}
        </p>
      </form>
    </Modal>
  );
}
