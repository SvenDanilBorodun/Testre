import React, { useState } from 'react';
import toast from 'react-hot-toast';
import { MdAdd, MdRemove } from 'react-icons/md';
import Modal from './Modal';
import { Btn } from '../EbUI';
import { adjustWorkgroupCredits } from '../../services/workgroupsApi';

const inputClass =
  'w-full h-10 px-3 bg-white border border-[var(--line)] rounded-[var(--radius-sm)] text-sm font-mono text-[var(--ink)] focus:outline-none focus:border-[var(--accent)] focus:ring-2 focus:ring-[color:var(--accent-wash)] transition';

export default function WorkgroupCreditsModal({
  token,
  workgroup,
  poolAvailable,
  onClose,
  onChanged,
}) {
  const [amount, setAmount] = useState('');
  const [busy, setBusy] = useState(false);

  const used = workgroup.trainings_used || 0;
  const total = workgroup.shared_credits || 0;
  const remaining = Math.max(total - used, 0);

  const submit = async (delta) => {
    if (!Number.isFinite(delta) || delta === 0) return;
    setBusy(true);
    try {
      const res = await adjustWorkgroupCredits(token, workgroup.id, delta);
      onChanged?.({
        ...workgroup,
        shared_credits: res.new_amount,
        remaining: Math.max(res.new_amount - used, 0),
      });
      toast.success(`Geteilte Credits: ${res.new_amount}`);
      setAmount('');
    } catch (err) {
      toast.error(err.message || 'Fehler');
    } finally {
      setBusy(false);
    }
  };

  const parsed = Number(amount);
  const canAdd = Number.isFinite(parsed) && parsed > 0 && parsed <= 1000;
  const canSubtract = Number.isFinite(parsed) && parsed > 0 && parsed <= remaining;
  const canApply = canAdd; // legacy alias for the input keydown

  return (
    <Modal
      title={`Credits · ${workgroup.name}`}
      onClose={onClose}
      footer={
        <Btn variant="primary" onClick={onClose}>
          Fertig
        </Btn>
      }
    >
      <div className="flex flex-col gap-5">
        <div className="grid grid-cols-3 gap-3 text-center">
          <div className="bg-[var(--bg-sunk)] rounded-[var(--radius-sm)] py-3">
            <div className="text-[10px] uppercase font-mono tracking-wider text-[var(--ink-3)] mb-1">
              Geteilt
            </div>
            <div className="text-2xl font-semibold text-[var(--ink)]">{total}</div>
          </div>
          <div className="bg-[var(--bg-sunk)] rounded-[var(--radius-sm)] py-3">
            <div className="text-[10px] uppercase font-mono tracking-wider text-[var(--ink-3)] mb-1">
              Verbraucht
            </div>
            <div className="text-2xl font-semibold text-[var(--ink)]">{used}</div>
          </div>
          <div className="bg-[var(--accent-wash)] rounded-[var(--radius-sm)] py-3">
            <div className="text-[10px] uppercase font-mono tracking-wider text-[var(--accent-ink)] mb-1">
              Frei
            </div>
            <div className="text-2xl font-semibold text-[var(--accent-ink)]">{remaining}</div>
          </div>
        </div>

        {poolAvailable !== null && poolAvailable !== undefined && (
          <p className="text-[11px] text-[var(--ink-3)] font-mono leading-snug -mt-2">
            Lehrer-Pool verfügbar: <span className="text-[var(--ink)]">{poolAvailable}</span>
          </p>
        )}

        <label className="block">
          <span className="text-xs font-medium text-[var(--ink-2)] mb-1.5 block">
            Geteilte Credits anpassen
          </span>
          <div className="flex items-center gap-2">
            <button
              onClick={() => submit(-Math.abs(parsed))}
              disabled={busy || !canSubtract}
              className="w-9 h-10 rounded-[var(--radius-sm)] bg-[var(--bg-sunk)] hover:bg-[var(--danger-wash)] hover:text-[color:var(--danger)] text-[var(--ink-2)] disabled:opacity-40 disabled:cursor-not-allowed flex items-center justify-center transition"
              title={
                canSubtract
                  ? 'Abziehen'
                  : remaining === 0
                  ? 'Keine freien Credits zum Abziehen'
                  : `Max. ${remaining} abziehbar`
              }
            >
              <MdRemove size={18} />
            </button>
            <input
              type="number"
              min={1}
              max={1000}
              inputMode="numeric"
              placeholder="Betrag"
              value={amount}
              onChange={(e) => setAmount(e.target.value.replace(/[^0-9]/g, ''))}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && canApply && !busy) {
                  e.preventDefault();
                  submit(Math.abs(parsed));
                }
              }}
              className={inputClass}
            />
            <button
              onClick={() => submit(Math.abs(parsed))}
              disabled={busy || !canAdd}
              className="w-9 h-10 rounded-[var(--radius-sm)] bg-[var(--accent-wash)] hover:brightness-95 text-[var(--accent-ink)] disabled:opacity-40 disabled:cursor-not-allowed flex items-center justify-center transition"
              title="Hinzufügen"
            >
              <MdAdd size={18} />
            </button>
          </div>
          <div className="flex items-center gap-1 mt-2">
            {[1, 5, 10, 25].map((v) => (
              <button
                key={v}
                type="button"
                disabled={busy}
                onClick={() => submit(v)}
                className="h-7 flex-1 rounded-[var(--radius-sm)] bg-[var(--bg-sunk)] hover:bg-[var(--accent-wash)] hover:text-[var(--accent-ink)] text-[11px] font-mono text-[var(--ink-2)] disabled:opacity-40 transition"
                title={`+${v} Credits sofort hinzufügen`}
              >
                +{v}
              </button>
            ))}
          </div>
          <p className="text-[11px] text-[var(--ink-3)] mt-2 leading-snug">
            <span className="font-mono">↩ Enter</span> fügt Credits hinzu · Schnellbuttons fügen sofort hinzu · Beim Reduzieren darf der neue Wert nicht unter die bereits verbrauchten Credits fallen.
          </p>
        </label>
      </div>
    </Modal>
  );
}
