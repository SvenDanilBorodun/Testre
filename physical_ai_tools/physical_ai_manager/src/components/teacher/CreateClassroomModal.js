import React, { useState } from 'react';
import toast from 'react-hot-toast';
import Modal from './Modal';

export default function CreateClassroomModal({ onClose, onSubmit }) {
  const [name, setName] = useState('');
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!name.trim()) return;
    setLoading(true);
    try {
      await onSubmit(name.trim());
      toast.success('Klasse erstellt');
      onClose();
    } catch (err) {
      toast.error(err.message || 'Fehler beim Erstellen');
    } finally {
      setLoading(false);
    }
  };

  return (
    <Modal
      title="Neue Klasse erstellen"
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
            form="create-classroom-form"
            className="px-4 py-2 rounded-lg bg-teal-600 text-white hover:bg-teal-700 disabled:bg-gray-300"
            disabled={loading || !name.trim()}
          >
            {loading ? 'Erstellen...' : 'Erstellen'}
          </button>
        </>
      }
    >
      <form id="create-classroom-form" onSubmit={handleSubmit} className="flex flex-col gap-4">
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">
            Name der Klasse
          </label>
          <input
            type="text"
            className="w-full px-4 py-3 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-teal-500"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="z.B. Klasse 8A"
            maxLength={100}
            autoFocus
            required
          />
          <p className="text-xs text-gray-500 mt-1">Max. 30 Schueler pro Klasse.</p>
        </div>
      </form>
    </Modal>
  );
}
