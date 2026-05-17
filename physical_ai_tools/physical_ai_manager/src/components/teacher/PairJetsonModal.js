// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

import React, { useState } from 'react';
import toast from 'react-hot-toast';
import Modal from './Modal';
import { Btn } from '../EbUI';
import { pairJetson } from '../../services/jetsonClient';

const inputClass =
  'w-full h-10 px-3 bg-white border border-[var(--line)] rounded-[var(--radius-sm)] text-sm text-[var(--ink)] placeholder:text-[var(--ink-4)] focus:outline-none focus:border-[var(--accent)] focus:ring-2 focus:ring-[color:var(--accent-wash)] transition';

const codeInputClass =
  'w-full h-14 px-3 bg-white border border-[var(--line)] rounded-[var(--radius-sm)] text-2xl font-mono text-center tracking-[0.4em] text-[var(--ink)] placeholder:text-[var(--ink-4)] focus:outline-none focus:border-[var(--accent)] focus:ring-2 focus:ring-[color:var(--accent-wash)] transition';

/**
 * Teacher enters the 6-digit pairing code from the Jetson's setup.sh
 * output. Calls POST /teacher/classrooms/{id}/jetson/pair on success.
 *
 * Maps the common error codes to specific German messages:
 *   404 → "Pairing-Code ungültig oder abgelaufen" (the most likely cause)
 *   403 → "Klasse gehört nicht zu diesem Lehrer" (defense-in-depth; the
 *         UI shouldn't surface this unless the teacher's session is stale)
 */
export default function PairJetsonModal({ onClose, classroomId, onPaired }) {
  const [code, setCode] = useState('');
  const [loading, setLoading] = useState(false);

  // Read JWT from Redux at submit time so a stale captured token doesn't
  // hit the API.
  const codeValid = /^\d{6}$/.test(code);

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!codeValid) {
      toast.error('Bitte 6-stelligen Pairing-Code eingeben');
      return;
    }
    setLoading(true);
    try {
      // The token comes via prop or via parent dispatch. The simplest
      // wiring: the parent passes a callback that handles auth.
      const result = await onPaired(code);
      // onPaired must throw on failure so we keep the modal open with
      // the error toast. Success → close.
      toast.success(`Jetson gepaart: ${result?.mdns_name || 'Erfolg'}`);
      onClose();
    } catch (err) {
      const status = err?.status;
      if (status === 404) {
        toast.error('Pairing-Code ungültig oder abgelaufen');
      } else if (status === 403) {
        toast.error('Keine Berechtigung — Klasse gehört nicht zu diesem Lehrer');
      } else if (status === 409) {
        toast.error('Jetson ist bereits gepaart');
      } else {
        toast.error(err?.message || 'Pairing fehlgeschlagen');
      }
    } finally {
      setLoading(false);
    }
  };

  return (
    <Modal
      title="Klassen-Jetson hinzufügen"
      onClose={onClose}
      footer={
        <>
          <Btn variant="ghost" onClick={onClose} disabled={loading}>
            Abbrechen
          </Btn>
          <Btn
            variant="primary"
            type="submit"
            form="pair-jetson-form"
            disabled={loading || !codeValid}
          >
            {loading ? 'Pairen…' : 'Pairen'}
          </Btn>
        </>
      }
    >
      <form id="pair-jetson-form" onSubmit={handleSubmit} className="flex flex-col gap-4">
        <p className="text-sm text-[var(--ink-2)] leading-snug">
          Auf dem Jetson hat <span className="font-mono">setup.sh</span>{' '}
          einen 6-stelligen Code gedruckt. Gib ihn hier ein, um den Jetson
          dauerhaft mit dieser Klasse zu verbinden.
        </p>
        <label className="block">
          <span className="text-xs font-medium text-[var(--ink-2)] mb-1.5 block">
            Pairing-Code
          </span>
          <input
            type="text"
            inputMode="numeric"
            pattern="\d{6}"
            className={codeInputClass}
            value={code}
            onChange={(e) => {
              // Strip non-digits and cap at 6 chars — friendlier than
              // a hard validation error on every keystroke.
              const cleaned = e.target.value.replace(/\D/g, '').slice(0, 6);
              setCode(cleaned);
            }}
            placeholder="000000"
            maxLength={6}
            autoFocus
            required
          />
          <p className="text-[11px] text-[var(--ink-3)] mt-1.5 leading-snug">
            Codes laufen 30 Minuten nach dem Drucken ab. Bei Bedarf{' '}
            <span className="font-mono">sudo systemctl restart edubotics-jetson</span>{' '}
            auf dem Jetson, um einen frischen Code zu erzeugen.
          </p>
        </label>
      </form>
    </Modal>
  );
}
