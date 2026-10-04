// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// „Hugging-Face-Token": the student pastes their token ONCE, here, and it
// follows their account to any robot (cloud API migration 042,
// hooks/useHfTokenSync). The second control on the Startseite, after „Roboter
// aktivieren" — and like that one it navigates nowhere and is gated on no
// capability: every rig needs a token to upload, whatever else it can do.
//
// WHAT THIS CARD NEVER DOES. It never keeps the token. The input is local
// component state, emptied the instant the student presses „Token speichern"
// (before the request is even sent), not inside a <form> and never persisted,
// and it is not a password field to a password manager (TokenInput, review j).
// The word „Token" reaches Redux only as a fingerprint and a 4-character hint
// (features/hfToken). Every sentence comes from features/hfToken/hfTokenCopy,
// so the German is reviewable in one place and the JSX carries none.
//
// THREE-STATE, like the rest of the page. `unknown`/`loading` means the cloud
// has not answered: the card says so and offers nothing, and nothing else
// reads that as „no token". A robot whose state has not arrived yet is shown as
// „gespeichert", not as „Roboter nicht verbunden" — that pill is for a link
// that is actually down.

import React, { useCallback, useState } from 'react';
import { useDispatch, useSelector } from 'react-redux';

import { Btn, Card, Pill } from '../EbUI';
import Icon from '../icons/Icon';
import { hfCopy } from '../../features/hfToken/hfTokenCopy';
import { kicked } from '../../features/hfToken/hfTokenSlice';
import {
  selectHfAccount,
  selectHfDecision,
  selectHfInSync,
  selectHfOfflineEscape,
  selectHfRobot,
  selectHfSync,
  selectHfSyncFailed,
} from '../../features/hfToken/hfTokenSelectors';
import {
  forceRetransfer,
  removeHfToken,
  saveHfToken,
  verifyHfToken,
} from '../../features/hfToken/hfTokenThunks';
import { isCloudOnlyMode } from '../../utils/cloudMode';

const NOTE = 'text-[12.5px] leading-snug text-[var(--ink-3)]';
const NOTE_BAD = 'text-[12.5px] leading-snug text-[color:var(--danger)]';

/** „03.10.26, 14:05" for an ISO time, or '' when it is missing or unparseable. */
export function formatChecked(iso) {
  if (typeof iso !== 'string' || !iso) return '';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleString('de-DE', { dateStyle: 'short', timeStyle: 'short' });
}

/**
 * The robot half of a STORED token, as one pure decision so the pill, the note
 * and the retry button cannot disagree.
 * @returns {{tone: string, pill: string, note: ?string, retry: boolean, badNote: boolean}}
 */
export function robotView({ robot, heartbeatConnected, jetsonConnected, inSync, working, decision, failed }) {
  if (jetsonConnected) {
    return { tone: 'neutral', pill: null, note: 'card.jetsonNote', retry: false, badNote: false };
  }
  if (!robot.known) {
    // A robot that never sent the state topic is an image from before this
    // feature: it takes no personal token. One that has not spoken YET is just
    // slow — „gespeichert", not an accusation.
    if (robot.legacy) {
      return { tone: 'neutral', pill: 'card.pill.notAccepted', note: null, retry: false, badNote: false };
    }
    return heartbeatConnected
      ? { tone: 'neutral', pill: 'card.pill.stored', note: null, retry: false, badNote: false }
      : { tone: 'neutral', pill: 'card.pill.noLink', note: null, retry: false, badNote: false };
  }
  if (robot.accepts !== true) {
    return { tone: 'neutral', pill: 'card.pill.notAccepted', note: null, retry: false, badNote: false };
  }
  if (inSync) {
    return { tone: 'success', pill: 'card.pill.active', note: null, retry: false, badNote: false };
  }
  if (decision === 'taken_over') {
    return { tone: 'amber', pill: 'card.pill.takenOver', note: 'card.takenOverNote', retry: true, badNote: false };
  }
  if (failed) {
    return { tone: 'danger', pill: 'card.pill.failed', note: 'card.failedNote', retry: true, badNote: true };
  }
  if (working) {
    return { tone: 'amber', pill: 'card.pill.working', note: null, retry: false, badNote: false };
  }
  if (decision === 'wait') {
    return { tone: 'amber', pill: 'card.pill.waiting', note: 'card.waitingNote', retry: false, badNote: false };
  }
  return { tone: 'amber', pill: 'card.pill.working', note: null, retry: false, badNote: false };
}

/**
 * Can this browser mask a TEXT field (`-webkit-text-security`)? Chrome, Edge
 * and Safari always could, Firefox since 114 (MDN browser-compat-data,
 * css.properties.-webkit-text-security). Evaluated per render, so a test can
 * stub `window.CSS`.
 */
export function canMaskTextField() {
  try {
    return typeof CSS !== 'undefined' && typeof CSS.supports === 'function'
      && CSS.supports('-webkit-text-security', 'disc');
  } catch {
    return false;
  }
}

// WHY NOT `type="password"` (review j, 2026-10-04). Browsers and password-
// manager extensions offer to SAVE whatever is typed into a password field once
// it disappears after a request — with or without a <form> — and a Pi's or a
// classroom PC's browser profile is shared, so the next student would find the
// token in the saved passwords. A plain text field masked with
// `-webkit-text-security: disc` looks the same on screen but is not a password
// field to the browser's manager; `autoComplete="off"` (honoured on a text
// field) and the ignore attributes of the common extensions (1Password,
// LastPass, Bitwarden, Dashlane) keep those away too. A browser that cannot
// mask a text field falls back to `type="password"`: a visible token on a
// classroom screen is worse than a save offer the student can decline.
const PASSWORD_MANAGER_IGNORE = Object.freeze({
  'data-1p-ignore': 'true',
  'data-lpignore': 'true',
  'data-bwignore': 'true',
  'data-form-type': 'other',
});

function TokenInput({ value, onChange, onSubmit, disabled }) {
  const masked = canMaskTextField();
  return (
    <input
      type={masked ? 'text' : 'password'}
      style={masked ? { WebkitTextSecurity: 'disc' } : undefined}
      value={value}
      onChange={(event) => onChange(event.target.value)}
      onKeyDown={(event) => {
        if (event.key === 'Enter') {
          event.preventDefault();
          onSubmit();
        }
      }}
      placeholder={hfCopy('card.input.placeholder')}
      aria-label={hfCopy('card.input.aria')}
      autoComplete={masked ? 'off' : 'new-password'}
      autoCapitalize="off"
      autoCorrect="off"
      spellCheck={false}
      name="edubotics-hf-access"
      {...PASSWORD_MANAGER_IGNORE}
      disabled={disabled}
      className="h-10 flex-1 min-w-[200px] rounded-[var(--radius-sm)] border border-[var(--line)] bg-white px-3 font-mono text-sm"
    />
  );
}

export default function HfTokenCard() {
  const dispatch = useDispatch();
  const account = useSelector(selectHfAccount);
  const robot = useSelector(selectHfRobot);
  const sync = useSelector(selectHfSync);
  const decision = useSelector(selectHfDecision);
  const inSync = useSelector(selectHfInSync);
  const failed = useSelector(selectHfSyncFailed);
  const jetsonConnected = useSelector((s) => s.jetson?.status === 'connected');
  // „Ohne Anmeldung fortfahren": the account half is off without a JWT, so the
  // state would read „wird geladen" for ever (review i).
  const offline = useSelector(selectHfOfflineEscape);
  const heartbeatConnected = useSelector((s) => s.tasks?.heartbeatStatus === 'connected');

  // The token lives in this state for as long as it takes to press the button.
  const [input, setInput] = useState('');
  const [saving, setSaving] = useState(false);
  const [replacing, setReplacing] = useState(false);
  const [confirmingRemove, setConfirmingRemove] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState(null); // {text, bad} — a German sentence, never a token

  const submit = useCallback(async () => {
    const value = input.trim();
    if (!value || saving) return;
    // Emptied BEFORE the request goes out: the secret must not sit in the DOM
    // or in component state while the network is slow.
    setInput('');
    setSaving(true);
    setMessage(null);
    try {
      const result = await dispatch(saveHfToken(value));
      if (result.ok) {
        setReplacing(false);
      } else if (result.error) {
        setMessage({ text: result.error, bad: true });
      }
    } finally {
      setSaving(false);
    }
  }, [dispatch, input, saving]);

  const verify = useCallback(async () => {
    setBusy(true);
    setMessage(null);
    try {
      const result = await dispatch(verifyHfToken());
      if (!result.ok && result.error) setMessage({ text: result.error, bad: true });
    } finally {
      setBusy(false);
    }
  }, [dispatch]);

  const remove = useCallback(async () => {
    setBusy(true);
    setMessage(null);
    try {
      const result = await dispatch(removeHfToken());
      if (!result.ok && result.error) setMessage({ text: result.error, bad: true });
      setConfirmingRemove(false);
    } finally {
      setBusy(false);
    }
  }, [dispatch]);

  if (isCloudOnlyMode()) return null;

  const { status } = account;
  const working = sync.phase === 'pushing' || sync.phase === 'clearing';
  const view = status === 'stored'
    ? robotView({
      robot, heartbeatConnected, jetsonConnected, inSync, working, decision, failed,
    })
    : null;

  const pillText = (() => {
    if (status === 'none') return hfCopy('card.pill.none');
    if (status === 'stored' && view?.pill) return hfCopy(view.pill);
    return null;
  })();
  const pillTone = status === 'none' ? 'neutral' : (view?.tone ?? 'neutral');

  const header = (
    <Pill tone={pillTone} dot={status === 'stored' && inSync}>
      {pillText}
    </Pill>
  );

  const inputRow = (
    <div className="flex flex-wrap items-center gap-2">
      <TokenInput value={input} onChange={setInput} onSubmit={submit} disabled={saving} />
      <Btn variant="primary" onClick={submit} disabled={saving || !input.trim()}>
        {saving ? (
          <span className="inline-flex items-center gap-1.5">
            <Icon name="loading" className="animate-spin" /> {hfCopy('card.saving')}
          </span>
        ) : (
          <span className="inline-flex items-center gap-1.5">
            <Icon name="key" /> {hfCopy('card.save')}
          </span>
        )}
      </Btn>
    </div>
  );

  const feedback = message ? (
    <p role="alert" className={`${message.bad ? NOTE_BAD : NOTE} mt-2`}>{message.text}</p>
  ) : null;

  let body;
  if ((status === 'unknown' || status === 'loading') && offline) {
    body = <p className={NOTE}>{hfCopy('card.offline')}</p>;
  } else if (status === 'unknown' || status === 'loading') {
    body = <p className={NOTE}>{hfCopy('card.unknown')}</p>;
  } else if (status === 'unsupported') {
    body = <p className={NOTE}>{hfCopy('card.unsupported')}</p>;
  } else if (status === 'unavailable') {
    body = <p className={NOTE}>{hfCopy('card.unavailable')}</p>;
  } else if (status === 'error') {
    body = (
      <div className="flex flex-col sm:flex-row sm:items-center gap-3">
        <p className={`${NOTE_BAD} flex-1`}>{hfCopy('card.loadError')}</p>
        <Btn variant="secondary" onClick={() => dispatch(kicked())}>
          <Icon name="refresh" /> {hfCopy('card.reload')}
        </Btn>
      </div>
    );
  } else if (status === 'none') {
    body = (
      <div>
        <p className="text-[13px] leading-snug text-[var(--ink-2)] mb-2">{hfCopy('card.none.body')}</p>
        <ol className="list-decimal pl-5 mb-3 space-y-0.5 text-[12.5px] leading-snug text-[var(--ink-3)]">
          <li>{hfCopy('card.none.step1')}</li>
          <li>{hfCopy('card.none.step2')}</li>
          <li>{hfCopy('card.none.step3')}</li>
          <li>{hfCopy('card.none.step4')}</li>
        </ol>
        {inputRow}
        {feedback}
      </div>
    );
  } else if (status === 'unusable') {
    body = (
      <div>
        <p className={`${NOTE_BAD} mb-3 inline-flex items-start gap-1.5`}>
          <Icon name="warning" className="mt-0.5" /> <span>{hfCopy('card.unusable')}</span>
        </p>
        {inputRow}
        <div className="mt-3">
          <Btn variant="ghost" size="sm" onClick={remove} disabled={busy}>
            <Icon name="trash" /> {hfCopy('card.remove')}
          </Btn>
        </div>
        {feedback}
      </div>
    );
  } else {
    // stored
    const checked = formatChecked(account.validatedAt);
    body = (
      <div>
        <div className="flex flex-col gap-0.5 mb-3">
          <p className="text-[13px] leading-snug text-[var(--ink-2)]">
            {hfCopy('card.stored.as')}{' '}
            <strong className="font-semibold text-[var(--ink)]">{account.hfUsername}</strong>
          </p>
          <p className={NOTE}>
            {hfCopy('card.stored.hint')}{' '}
            <span className="font-mono">{account.hint}</span>
            {checked ? ` · ${hfCopy('card.stored.checked')} ${checked}` : ''}
          </p>
        </div>

        {account.accountChanged && (
          <p className={`${NOTE_BAD} mb-3 inline-flex items-start gap-1.5`}>
            <Icon name="warning" className="mt-0.5" /> <span>{hfCopy('card.accountChanged')}</span>
          </p>
        )}

        <div aria-live="polite">
          {view?.note && (
            <p className={`${view.badNote ? NOTE_BAD : NOTE} mb-3 inline-flex items-start gap-1.5`}>
              <Icon name={view.badNote ? 'warning' : 'info'} className="mt-0.5" />
              <span>{hfCopy(view.note)}</span>
            </p>
          )}
          {view?.badNote && sync.lastMessage && (
            <p className={`${NOTE_BAD} mb-3`}>{sync.lastMessage}</p>
          )}
        </div>

        {replacing ? (
          <div className="mb-3">
            {inputRow}
            <div className="mt-2">
              <Btn variant="ghost" size="sm" onClick={() => { setReplacing(false); setInput(''); }} disabled={saving}>
                {hfCopy('card.removeAbort')}
              </Btn>
            </div>
          </div>
        ) : (
          <div className="flex flex-wrap items-center gap-2">
            {view?.retry && (
              <Btn variant="primary" onClick={() => dispatch(forceRetransfer())}>
                <Icon name="refresh" /> {hfCopy('card.retry')}
              </Btn>
            )}
            <Btn variant="secondary" onClick={() => { setReplacing(true); setMessage(null); }} disabled={busy}>
              <Icon name="key" /> {hfCopy('card.replace')}
            </Btn>
            <Btn variant="secondary" onClick={verify} disabled={busy}>
              <Icon name="refresh" /> {hfCopy('card.verify')}
            </Btn>
            {confirmingRemove ? (
              <>
                <Btn variant="danger" onClick={remove} disabled={busy}>
                  <Icon name="trash" /> {hfCopy('card.removeConfirm')}
                </Btn>
                <Btn variant="ghost" onClick={() => setConfirmingRemove(false)} disabled={busy}>
                  {hfCopy('card.removeAbort')}
                </Btn>
              </>
            ) : (
              <Btn variant="ghost" onClick={() => setConfirmingRemove(true)} disabled={busy}>
                <Icon name="trash" /> {hfCopy('card.remove')}
              </Btn>
            )}
          </div>
        )}
        {feedback}
      </div>
    );
  }

  return (
    <div data-testid="hf-token-card">
      <Card title={hfCopy('card.title')} right={pillText ? header : null}>
        {body}
      </Card>
    </div>
  );
}
