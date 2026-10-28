import React, { useState } from 'react';
import toast from 'react-hot-toast';
import Modal from './Modal';
import { Btn } from '../EbUI';
import { changeWorkgroupHours, setMemberLimit } from '../../services/workgroupsApi';
import { newRequestId } from '../../services/cloudTrainingApi';
import { RECLAIM_FOOTNOTE_DE, hoursDe, hoursOf, numberDe, reclaimTitle } from '../../utils/hoursDe';
import Icon from '../icons/Icon';

const inputClass =
  'w-full h-10 px-3 bg-white border border-[var(--line)] rounded-[var(--radius-sm)] text-sm font-mono text-[var(--ink)] focus:outline-none focus:border-[var(--accent)] focus:ring-2 focus:ring-[color:var(--accent-wash)] transition';

/** The per-member limit as the teacher reads it (D5). */
export function limitStateText(limit) {
  return typeof limit === 'number' ? `Höchstens ${hoursDe(limit)} pro Mitglied` : 'Kein Limit gesetzt';
}

// A group's training hours (credit system v2, spec §10.4): free / reserved /
// spent, assign and reclaim by DELTA with a request_id each, and the one
// per-member hour limit of the group.
export default function WorkgroupCreditsModal({
  token,
  workgroup,
  poolAvailable,
  onClose,
  onChanged,
}) {
  const [amount, setAmount] = useState('');
  const [busy, setBusy] = useState(false);
  const currentLimit = typeof workgroup.member_limit_hours === 'number' ? workgroup.member_limit_hours : null;
  const [limitValue, setLimitValue] = useState(currentLimit === null ? '' : String(currentLimit));
  const [noLimit, setNoLimit] = useState(currentLimit === null);

  const free = hoursOf(workgroup.hours_free);
  const reserved = hoursOf(workgroup.hours_reserved);
  const spent = hoursOf(workgroup.hours_spent);

  const submit = async (delta) => {
    if (!Number.isFinite(delta) || delta === 0) return false;
    setBusy(true);
    try {
      const res = await changeWorkgroupHours(token, workgroup.id, { delta, request_id: newRequestId() });
      const f = hoursOf(res.hours_free ?? res.new_amount);
      const r = hoursOf(res.hours_reserved ?? reserved);
      const sp = hoursOf(res.hours_spent ?? spent);
      onChanged?.({
        ...workgroup,
        hours_free: f,
        hours_reserved: r,
        hours_spent: sp,
        shared_credits: f + r + sp,
        trainings_used: r + sp,
        remaining: f,
        pool_available: res.pool_available,
      });
      toast.success(`Freie Stunden der Gruppe: ${res.new_amount}`);
      setAmount('');
      return true;
    } catch (err) {
      toast.error(err.message || 'Fehler');
      return false;
    } finally {
      setBusy(false);
    }
  };

  const parsed = Number(amount);
  const canAdd = Number.isFinite(parsed) && parsed > 0 && parsed <= 1000;
  const canSubtract = Number.isFinite(parsed) && parsed > 0 && parsed <= free;

  const parsedLimit = Number(limitValue);
  const limitValid = noLimit || (limitValue !== '' && Number.isInteger(parsedLimit) && parsedLimit >= 1 && parsedLimit <= 1000);
  const nextLimit = noLimit ? null : parsedLimit;
  const limitChanged = nextLimit !== currentLimit;

  const saveLimit = async () => {
    if (busy || !limitValid) return;
    setBusy(true);
    try {
      const res = await setMemberLimit(token, workgroup.id, nextLimit);
      const saved = res && Object.prototype.hasOwnProperty.call(res, 'member_limit_hours')
        ? res.member_limit_hours : nextLimit;
      onChanged?.({ ...workgroup, member_limit_hours: saved ?? null });
      toast.success('Limit gespeichert');
    } catch (err) {
      toast.error(err.message || 'Fehler');
    } finally {
      setBusy(false);
    }
  };

  // "Fertig" should commit any pending value (positive add) and then
  // close — that's the natural mental model when the modal contains a
  // typed-but-not-yet-applied amount. Plain close lost the user's input.
  // On API failure the toast surfaces the error and the modal stays
  // open so the user can fix the value instead of being thrown out.
  const handleDone = async () => {
    if (busy) return;
    if (canAdd) {
      const ok = await submit(Math.abs(parsed));
      if (!ok) return;
    }
    onClose();
  };

  const members = Array.isArray(workgroup.members) ? workgroup.members : [];

  return (
    <Modal
      title={`Stunden · ${workgroup.name}`}
      onClose={onClose}
      footer={
        <>
          <Btn variant="ghost" onClick={onClose} disabled={busy}>
            Schließen
          </Btn>
          <Btn variant="primary" onClick={handleDone} disabled={busy}>
            {canAdd ? `+${parsed} Std. zuweisen & schließen` : 'Fertig'}
          </Btn>
        </>
      }
    >
      <div className="flex flex-col gap-5">
        <div className="grid grid-cols-3 gap-3 text-center">
          <div className="bg-[var(--accent-wash)] rounded-[var(--radius-sm)] py-3" data-testid="tile-free">
            <div className="text-[10px] uppercase font-mono tracking-wider text-[var(--accent-ink)] mb-1">
              Frei
            </div>
            <div className="text-2xl font-semibold text-[var(--accent-ink)]">{numberDe(free)}</div>
          </div>
          <div className="bg-[var(--bg-sunk)] rounded-[var(--radius-sm)] py-3" data-testid="tile-reserved">
            <div className="text-[10px] uppercase font-mono tracking-wider text-[var(--ink-3)] mb-1">
              Reserviert
            </div>
            <div className="text-2xl font-semibold text-[var(--ink)]">{numberDe(reserved)}</div>
          </div>
          <div className="bg-[var(--bg-sunk)] rounded-[var(--radius-sm)] py-3" data-testid="tile-spent">
            <div className="text-[10px] uppercase font-mono tracking-wider text-[var(--ink-3)] mb-1">
              Verbraucht
            </div>
            <div className="text-2xl font-semibold text-[var(--ink)]">{numberDe(spent)}</div>
          </div>
        </div>

        {poolAvailable !== null && poolAvailable !== undefined && (
          <p className="text-[11px] text-[var(--ink-3)] font-mono leading-snug -mt-2">
            {`Lehrer-Pool frei: ${numberDe(poolAvailable)} Std.`}
          </p>
        )}

        <label className="block">
          <span className="text-xs font-medium text-[var(--ink-2)] mb-1.5 block">
            Stunden der Gruppe anpassen
          </span>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => submit(-Math.abs(parsed))}
              disabled={busy || !canSubtract}
              className="w-9 h-10 rounded-[var(--radius-sm)] bg-[var(--bg-sunk)] hover:bg-[var(--danger-wash)] hover:text-[color:var(--danger)] text-[var(--ink-2)] disabled:opacity-40 disabled:cursor-not-allowed flex items-center justify-center transition"
              title={reclaimTitle(canSubtract, free)}
              aria-label={reclaimTitle(canSubtract, free)}
            >
              <Icon name="minus" size={18} />
            </button>
            <input
              type="number"
              min={1}
              max={1000}
              inputMode="numeric"
              placeholder="Betrag"
              aria-label="Stunden"
              value={amount}
              onChange={(e) => setAmount(e.target.value.replace(/[^0-9]/g, ''))}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && canAdd && !busy) {
                  e.preventDefault();
                  submit(Math.abs(parsed));
                }
              }}
              className={inputClass}
            />
            <button
              type="button"
              onClick={() => submit(Math.abs(parsed))}
              disabled={busy || !canAdd}
              className="w-9 h-10 rounded-[var(--radius-sm)] bg-[var(--accent-wash)] hover:brightness-95 text-[var(--accent-ink)] disabled:opacity-40 disabled:cursor-not-allowed flex items-center justify-center transition"
              title="Zuweisen"
              aria-label="Zuweisen"
            >
              <Icon name="plus" size={18} />
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
                title={`+${v} Std. sofort zuweisen`}
              >
                +{v}
              </button>
            ))}
          </div>
          <p className="text-[11px] text-[var(--ink-3)] mt-2 leading-snug">
            <span className="inline-flex items-center gap-0.5 font-mono"><Icon name="enterKey" /> Enter</span>
            {` weist zu · Schnellbuttons weisen sofort zu · ${RECLAIM_FOOTNOTE_DE}`}
          </p>
        </label>

        <section aria-label="Stundenlimit pro Mitglied" className="flex flex-col gap-2 pt-3 border-t border-[var(--line)]">
          <h4 className="text-xs font-medium text-[var(--ink-2)]">Stundenlimit pro Mitglied</h4>
          <p className="text-[12px] text-[var(--ink-3)]" data-testid="limit-state">{limitStateText(currentLimit)}</p>
          <div className="flex items-center gap-2 flex-wrap">
            <input
              type="number"
              min={1}
              max={1000}
              inputMode="numeric"
              aria-label="Stundenlimit pro Mitglied"
              value={limitValue}
              disabled={noLimit || busy}
              onChange={(e) => setLimitValue(e.target.value.replace(/[^0-9]/g, ''))}
              className={`${inputClass} max-w-[120px]`}
            />
            <label className="inline-flex items-center gap-1.5 text-sm text-[var(--ink-2)]">
              <input
                type="checkbox"
                checked={noLimit}
                disabled={busy}
                onChange={(e) => setNoLimit(e.target.checked)}
              />
              Kein Limit
            </label>
            <Btn variant="secondary" size="sm" onClick={saveLimit} disabled={busy || !limitValid || !limitChanged}>
              Limit speichern
            </Btn>
          </div>
          {members.length > 0 && (
            <ul className="flex flex-col gap-1 text-[12px] text-[var(--ink-2)]" aria-label="Genutzte Stunden je Mitglied">
              {members.map((m) => (
                <li key={m.id}>{`${m.full_name || m.username}: ${hoursDe(hoursOf(m.hours_used))} genutzt`}</li>
              ))}
            </ul>
          )}
        </section>
      </div>
    </Modal>
  );
}
