import React, { useState } from 'react';
import toast from 'react-hot-toast';
import Modal from '../teacher/Modal';

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
      toast.error('Benutzername: 3-32 Zeichen, Kleinbuchstaben, Ziffern, . _ -');
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
            form="create-teacher-form"
            className="px-4 py-2 rounded-lg bg-teal-600 text-white hover:bg-teal-700 disabled:bg-gray-300"
            disabled={loading || !usernameValid || !fullName || password.length < 6}
          >
            {loading ? 'Erstellen...' : 'Erstellen'}
          </button>
        </>
      }
    >
      <form id="create-teacher-form" onSubmit={handleSubmit} className="flex flex-col gap-4">
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">
            Vollstaendiger Name
          </label>
          <input
            type="text"
            className="w-full px-4 py-3 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-teal-500"
            value={fullName}
            onChange={(e) => setFullName(e.target.value)}
            maxLength={100}
            autoFocus
            required
          />
        </div>
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">
            Benutzername (fuer Login)
          </label>
          <input
            type="text"
            className="w-full px-4 py-3 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-teal-500"
            value={username}
            onChange={(e) => setUsername(e.target.value.toLowerCase())}
            placeholder="frau.mustermann"
            maxLength={32}
            required
          />
        </div>
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">
            Start-Passwort
          </label>
          <input
            type="text"
            className="w-full px-4 py-3 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-teal-500 font-mono"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            minLength={6}
            required
          />
        </div>
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">
            Start-Credits (Pool)
          </label>
          <input
            type="number"
            className="w-full px-4 py-3 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-teal-500"
            value={credits}
            onChange={(e) => setCredits(e.target.value)}
            min={0}
            max={10000}
          />
          <p className="text-xs text-gray-500 mt-1">
            Credits die der Lehrer an Schueler verteilen kann.
          </p>
        </div>
      </form>
    </Modal>
  );
}
