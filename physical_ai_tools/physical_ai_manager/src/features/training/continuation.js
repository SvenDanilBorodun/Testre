// „Fortsetzen" for a capped / wall-capped training (owner E32–E37, E46).
// Pure: decides what the student may do with one training, given the job list
// and the remaining credits. No React, no side effects.

const de = (n) => Number(n).toLocaleString('de-DE');

export const CONTINUE_DE = {
  button: 'Fortsetzen',
  continuedFrom: (step) => `Fortsetzung ab Schritt ${de(step)}`,
  continuationChip: 'Fortsetzung',
  stopped: (step, total) =>
    `Bei Schritt ${de(step)} von ${de(total)} gestoppt – du kannst das Training fortsetzen.`,
  notMine: (step, total) =>
    `Bei Schritt ${de(step)} von ${de(total)} gestoppt (Zeitlimit) – fortsetzen kann es nur, wer es gestartet hat.`,
  expired:
    'Der gespeicherte Zwischenstand ist abgelaufen oder nicht mehr nutzbar – ein neues Training ist nötig.',
  continuing: 'Wird gerade fortgesetzt …',
  continued: 'Wurde bereits fortgesetzt.',
  noCredit: 'Kein Trainingsguthaben mehr für die Fortsetzung.',
  started: 'Fortsetzung gestartet – ein weiteres Guthaben wurde verbraucht.',
  failedPrefix: 'Fortsetzen nicht möglich: ',
};

const ACTIVE = new Set(['queued', 'running', 'cancel_requested']);

/**
 * { kind, child } for one training. kind ∈
 *   null | 'not_mine' | 'continuable' | 'no_credit' | 'expired' | 'continuing' | 'continued'
 * decided in this order (owner E46):
 *   - null unless the job is a continuable capped row
 *   - not_mine if the caller did not start it
 *   - continuing / continued if a child exists (active / succeeded)
 *   - expired past continuable_until
 *   - no_credit with no credits left
 *   - else continuable
 */
export function continuationState(job, jobs, remaining, nowMs) {
  if (
    !job ||
    job.status !== 'succeeded' ||
    !Number.isInteger(job.capped_step) ||
    job.capped_step >= job.total_steps ||
    !job.continuable_until
  ) {
    return { kind: null, child: null };
  }
  if (job.is_mine !== true) {
    return { kind: 'not_mine', child: null };
  }
  const children = (jobs || []).filter((j) => j && j.continued_from === job.id);
  const active = children.find((j) => ACTIVE.has(j.status));
  if (active) {
    return { kind: 'continuing', child: active };
  }
  const done = children.find((j) => j.status === 'succeeded');
  if (done) {
    return { kind: 'continued', child: done };
  }
  if (Date.parse(job.continuable_until) <= nowMs) {
    return { kind: 'expired', child: null };
  }
  if (Number(remaining) <= 0) {
    return { kind: 'no_credit', child: null };
  }
  return { kind: 'continuable', child: null };
}
