import React, { useState } from 'react';
import toast from 'react-hot-toast';
import Modal from './Modal';

export default function PasswordResetModal({ onClose, onSubmit, username }) {
  const [password, setPassword] = useState('');
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (password.length < 6) return;
    setLoading(true);
    try {
      await onSubmit(password);
      toast.success('Passwort zurueckgesetzt');
      onClose();
    } catch (err) {
      toast.error(err.message || 'Fehler');
    } finally {
      setLoading(false);
    }
  };

  return (
    <Modal
      title="Passwort zuruecksetzen"
      onClose={onClose}
      footer={
        <>
          <button
            type="button"
            onClick={onClose}
            className="px-4 py-2 rounded-lg text-gray-700 hover:bg-gray-100"
            disabled={loading}
          >
            Abbrechen
          </button>
          <button
            type="submit"
            form="reset-password-form"
            className="px-4 py-2 rounded-lg bg-teal-600 text-white hover:bg-teal-700 disabled:bg-gray-300"
            disabled={loading || password.length < 6}
          >
            {loading ? 'Setzen...' : 'Setzen'}
          </button>
        </>
      }
    >
      <form id="reset-password-form" onSubmit={handleSubmit} className="flex flex-col gap-3">
        <p className="text-sm text-gray-600">
          Neues Passwort fuer <span className="font-mono font-semibold">{username}</span>:
        </p>
        <input
          type="text"
          className="w-full px-4 py-3 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-teal-500 font-mono"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="mindestens 6 Zeichen"
          minLength={6}
          autoFocus
          required
        />
        <p className="text-xs text-gray-500">
          Gib das neue Passwort dem Schueler weiter.
        </p>
      </form>
    </Modal>
  );
}
