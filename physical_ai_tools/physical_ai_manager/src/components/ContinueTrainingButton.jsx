import React, { useState } from 'react';
import { useDispatch, useSelector } from 'react-redux';
import toast from 'react-hot-toast';
import { continueCloudTraining } from '../services/cloudTrainingApi';
import { setSelectedTrainingId, triggerCloudJobsRefresh } from '../features/training/trainingSlice';
import { continuationState, CONTINUE_DE } from '../features/training/continuation';
import Icon from './icons/Icon';
import { releasePointerFocus } from './sucher/pointerFocus';

// „Fortsetzen" for a capped / wall-capped training (owner E32–E37, E46). Only the
// student who started it sees the key; a groupmate sees the stop fact without the
// false „du kannst … fortsetzen" promise (not_mine).
export default function ContinueTrainingButton({ job, jobs, remaining, onContinued }) {
  const dispatch = useDispatch();
  const session = useSelector((s) => s.auth?.session); // defensive: the chart's tests have no auth slice
  const [busy, setBusy] = useState(false);

  const { kind } = continuationState(job, jobs, remaining, Date.now());
  if (kind === null) return null;

  const step = job.capped_step;
  const total = job.total_steps;

  const Note = ({ children }) =>
    children ? (
      <p role="status" className="su-trn-note">
        {children}
      </p>
    ) : null;

  if (kind === 'not_mine') {
    return <Note>{CONTINUE_DE.notMine(step, total)}</Note>;
  }
  if (kind === 'expired' || kind === 'continuing' || kind === 'continued') {
    return <Note>{{ expired: CONTINUE_DE.expired, continuing: CONTINUE_DE.continuing, continued: CONTINUE_DE.continued }[kind]}</Note>;
  }

  const note = job.status_note || CONTINUE_DE.stopped(step, total);

  const onClick = async (e) => {
    releasePointerFocus(e);
    if (busy) return;
    setBusy(true);
    try {
      const result = await continueCloudTraining(session?.access_token, job.id);
      toast.success(CONTINUE_DE.started);
      dispatch(triggerCloudJobsRefresh());
      dispatch(setSelectedTrainingId(result.training_id));
      onContinued?.();
    } catch (err) {
      if (err && err.status === 409) {
        toast.error(err.detail);
        dispatch(triggerCloudJobsRefresh());
      } else {
        toast.error(CONTINUE_DE.failedPrefix + ((err && (err.detail || err.message)) || ''));
      }
    } finally {
      setBusy(false);
    }
  };

  if (kind === 'no_credit') {
    return (
      <>
        <Note>{note}</Note>
        <button type="button" className="su-key su-key--signal su-key--sm" disabled title={CONTINUE_DE.noCredit}>
          <Icon name="play" />
          <span className="su-lab">{CONTINUE_DE.noCredit}</span>
        </button>
      </>
    );
  }

  // continuable
  return (
    <>
      <Note>{note}</Note>
      <button
        type="button"
        className="su-key su-key--signal su-key--sm"
        onClick={onClick}
        disabled={busy}
      >
        <Icon name="play" />
        <span className="su-lab">{CONTINUE_DE.button}</span>
      </button>
    </>
  );
}
