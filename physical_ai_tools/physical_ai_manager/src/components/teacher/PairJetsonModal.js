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

const codeInputClass =
  'w-full h-14 px-3 bg-white border border-[var(--line)] rounded-[var(--radius-sm)] text-2xl font-mono text-center tracking-[0.4em] text-[var(--ink)] placeholder:text-[var(--ink-4)] focus:outline-none focus:border-[var(--accent)] focus:ring-2 focus:ring-[color:var(--accent-wash)] transition';

/**
 * Teacher enters the 6-digit pairing code from the Jetson's setup.sh
 * output. The parent's `onPaired` callback performs the new
 * two-step pair-intent → pair flow (2026-05 IDOR fix in
 * jetsonClient.pairJetsonIntent + jetsonClient.pairJetson). The
 * loading spinner here stays active across both calls because
 * `onPaired` only resolves after both succeed.
 *
 * Error mapping by step + status (errors thrown by the client are
 * tagged with `err.step ∈ {'intent','pair'}`):
 *   intent 404 → "Pairing-Code ungültig oder abgelaufen"
 *   intent 409 → German message already set by pairJetsonIntent
 *                ("…bereits von einem anderen Lehrer beansprucht…")
 *   pair   403 → German message already set by pairJetson
 *                ("Pairing-Intent abgelaufen oder ungültig…")
 *                Edge case: the intent_token is consumed once the
 *                pair-intent succeeds. If the subsequent pair fails
 *                we have no client-side release endpoint — the
 *                teacher must ask the classroom agent to rotate its
 *                pairing_code (either by restarting the agent or
 *                via the regenerate_pairing_code RPC). The toast
 *                below surfaces that hint.
 *   pair   404 → "Pairing-Code ungültig oder abgelaufen"
 *   pair   409 → "Jetson ist bereits gepaart"
 */
export default function PairJetsonModal({ onClose, classroomId, onPaired }) {
  // classroomId is currently consumed by the parent through the
  // onPaired closure, but we keep the prop on the signature so future
  // refactors that move the fetch into this component don't need to
  // re-thread it through JetsonSection. Avoid an unused-var lint by
  // referencing it through Boolean coercion.
  void classroomId;

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
      // onPaired runs the two-step pair-intent → pair flow inside
      // JetsonSection (it has the access_token from Redux). It only
      // resolves after BOTH calls succeed, so this single setLoading
      // gate covers the whole flow.
      const result = await onPaired(code);
      toast.success(`Jetson gepaart: ${result?.mdns_name || 'Erfolg'}`);
      onClose();
    } catch (err) {
      const status = err?.status;
      const step = err?.step;
      if (step === 'pair' && status !== 404 && status !== 409) {
        // The intent_token was consumed but the pair step failed
        // (403 mismatch, 5xx, network). The teacher cannot retry
        // until the agent rotates its pairing_code — surface the
        // German hint regardless of the underlying status code.
        toast.error(
          'Pairing fehlgeschlagen. Bitte beim Klassen-Agenten den Code neu generieren.'
        );
      } else if (status === 404) {
        toast.error('Pairing-Code ungültig oder abgelaufen');
      } else if (status === 409) {
        // Step 'intent' 409 → wrapped German message about another
        // teacher already claiming the code. Step 'pair' 409 →
        // Jetson already bound. Show the wrapped message when one
        // is present, otherwise the generic German.
        toast.error(err?.message || 'Jetson ist bereits gepaart');
      } else if (status === 403) {
        // Catch-all for any 403 not already handled above.
        toast.error(err?.message || 'Keine Berechtigung');
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
