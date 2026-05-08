import React, { useState } from 'react';
import toast from 'react-hot-toast';
import Modal from './Modal';
import { Btn } from '../EbUI';

const inputClass =
  'w-full h-10 px-3 bg-white border border-[var(--line)] rounded-[var(--radius-sm)] text-sm text-[var(--ink)] placeholder:text-[var(--ink-4)] focus:outline-none focus:border-[var(--accent)] focus:ring-2 focus:ring-[color:var(--accent-wash)] transition';

export default function CreateWorkgroupModal({ onClose, onSubmit }) {
  const [name, setName] = useState('');
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!name.trim()) {
      toast.error('Bitte einen Namen eingeben');
      return;
    }
    setLoading(true);
    try {
      await onSubmit(name.trim());
      toast.success('Arbeitsgruppe erstellt');
      onClose();
    } catch (err) {
      toast.error(err.message || 'Fehler beim Erstellen');
    } finally {
      setLoading(false);
    }
  };

  return (
    <Modal
      title="Neue Arbeitsgruppe"
      onClose={onClose}
      footer={
        <>
          <Btn variant="ghost" onClick={onClose} disabled={loading}>
            Abbrechen
          </Btn>
          <Btn
            variant="primary"
            type="submit"
            form="create-workgroup-form"
            disabled={loading || !name.trim()}
          >
            {loading ? 'Erstellen…' : 'Erstellen'}
          </Btn>
        </>
      }
    >
      <form
        id="create-workgroup-form"
        onSubmit={handleSubmit}
        className="flex flex-col gap-4"
      >
        <label className="block">
          <span className="text-xs font-medium text-[var(--ink-2)] mb-1.5 block">
            Gruppenname
          </span>
          <input
            type="text"
            className={inputClass}
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="z. B. Team Alpha"
            maxLength={100}
            autoFocus
            required
          />
          <p className="text-[11px] text-[var(--ink-3)] mt-1.5 leading-snug">
            Bis zu 10 Schueler teilen sich Credits, Trainings, Datensaetze und Workflows.
          </p>
        </label>
      </form>
    </Modal>
  );
}
