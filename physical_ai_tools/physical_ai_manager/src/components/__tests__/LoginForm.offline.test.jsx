// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// The OFFLINE ESCAPE on the login card — „Ohne Anmeldung fortfahren".
//
// The student rig works completely offline today except for Training, Inferenz
// and the HuggingFace upload: rosbridge, the arm, the cameras, Roboter Studio,
// the Blockly autosave and recording-to-disk are all local. So a login gate with
// NO escape turns "the school Wi-Fi is down" into "every robot in the room is a
// brick" — a worse defect than the one the gate fixes.
//
// The escape is therefore ATTEMPT-GATED, the pattern this product already ships
// twice (the GUI's forced-update modal and PiUpdateGate both reveal „Ohne …
// fortfahren" only after a FAILED attempt): it appears only once an attempt has
// PROVEN the auth service could not answer. Measured, this matters — with the
// host unreachable supabase-js takes ~10.5 s and then hands back the English
// technical string „fetch failed", which LoginForm used to toast verbatim at a
// German schoolchild who cannot tell it from a wrong password.
//
// Four properties are pinned:
//   1. proven-unreachable  → the escape appears and calls back exactly once;
//   2. wrong password (400) → NO escape. The cloud is up and identity is the
//      whole point; an escape here is a bypass button, not a fallback;
//   3. the student never sees supabase-js's own English message;
//   4. a caller that passes no `onOfflineContinue` — the teacher web and the
//      Training/Inferenz page gates — never grows a bypass.

import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import toast from 'react-hot-toast';
import LoginForm from '../LoginForm';

// react-redux: the form only dispatches; a bare stub is enough.
const mockDispatch = vi.fn();
vi.mock('react-redux', () => ({
  __esModule: true,
  useDispatch: () => mockDispatch,
}));

vi.mock('react-hot-toast', () => ({
  __esModule: true,
  default: { error: vi.fn(), success: vi.fn(), dismiss: vi.fn() },
}));

// supabaseClient: pure stub so importing it never touches the network/env.
const mockSignIn = vi.fn();
vi.mock('../../lib/supabaseClient', () => ({
  __esModule: true,
  supabase: { auth: { signInWithPassword: (...a) => mockSignIn(...a) } },
}));

// The MEASURED shape: @supabase/auth-js 2.103.1 against an unreachable host.
const UNREACHABLE = Object.freeze({
  name: 'AuthRetryableFetchError',
  message: 'fetch failed',
  status: 0,
});
// The service answering ABOUT this request.
const WRONG_PASSWORD = Object.freeze({
  name: 'AuthApiError',
  message: 'Invalid login credentials',
  status: 400,
});

const ESCAPE = 'Ohne Anmeldung fortfahren';

beforeEach(() => {
  mockDispatch.mockClear();
  mockSignIn.mockReset();
  toast.error.mockClear();
  toast.success.mockClear();
});

/** Fill the form and submit it once. */
async function attemptLogin() {
  userEvent.type(screen.getByPlaceholderText('max.mustermann'), 'max.mustermann');
  userEvent.type(screen.getByPlaceholderText('Passwort eingeben'), 'geheim123');
  userEvent.click(screen.getByRole('button', { name: 'Anmelden' }));
  await waitFor(() => expect(mockSignIn).toHaveBeenCalled());
}

describe('LoginForm offline escape', () => {
  it('reveals the escape after an attempt PROVES the cloud unreachable', async () => {
    mockSignIn.mockResolvedValue({ data: { session: null }, error: UNREACHABLE });
    const onOfflineContinue = vi.fn();
    render(<LoginForm onOfflineContinue={onOfflineContinue} />);

    // Hidden until an attempt has been made — a permanently visible link would
    // simply be clicked by every student every time.
    expect(screen.queryByRole('button', { name: ESCAPE })).toBeNull();

    await attemptLogin();

    const btn = await screen.findByRole('button', { name: ESCAPE });
    expect(btn).toBeInTheDocument();
    // The escape lives INSIDE the <form>, under the submit button, so
    // type="button" is load-bearing: a bare <button> there defaults to
    // type="submit" and clicking the escape would re-fire the ~10 s login the
    // student just escaped from — on the one path where the auth service is
    // KNOWN to be unreachable, i.e. another 10 s of „Bitte warten…".
    expect(btn).toHaveAttribute('type', 'button');

    userEvent.click(btn);
    expect(onOfflineContinue).toHaveBeenCalledTimes(1);
    // …and this is the assertion that proves it BEHAVIOURALLY, without reaching
    // into the DOM tree: drop the type and the click submits the form, so
    // signInWithPassword is called a second time. MEASURED — with type removed
    // this line reports "called 2 times".
    await waitFor(() => expect(onOfflineContinue).toHaveBeenCalled());
    expect(mockSignIn).toHaveBeenCalledTimes(1);
  });

  it('offers NO escape for a wrong password', async () => {
    mockSignIn.mockResolvedValue({ data: { session: null }, error: WRONG_PASSWORD });
    const onOfflineContinue = vi.fn();
    render(<LoginForm onOfflineContinue={onOfflineContinue} />);
    await attemptLogin();

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith('Benutzername oder Passwort falsch')
    );
    expect(screen.queryByRole('button', { name: ESCAPE })).toBeNull();
    expect(onOfflineContinue).not.toHaveBeenCalled();
  });

  it('never echoes supabase-js’s English message at the student', async () => {
    mockSignIn.mockResolvedValue({ data: { session: null }, error: UNREACHABLE });
    render(<LoginForm onOfflineContinue={vi.fn()} />);
    await attemptLogin();

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        'Anmeldedienst nicht erreichbar — bitte Internetverbindung prüfen.'
      )
    );
    for (const call of toast.error.mock.calls) {
      expect(String(call[0])).not.toMatch(/fetch failed|Failed to fetch/i);
    }
  });

  it('grows no bypass for a caller that did not ask for one', async () => {
    // WebApp (the teacher web) and the TrainingPage / InferencePage gates all
    // render <LoginForm/> with no onOfflineContinue. There is nothing offline
    // for them to fall back TO, and a bypass on the teacher surface would be a
    // straight defect.
    mockSignIn.mockResolvedValue({ data: { session: null }, error: UNREACHABLE });
    render(<LoginForm />);
    await attemptLogin();

    await waitFor(() => expect(toast.error).toHaveBeenCalled());
    expect(screen.queryByRole('button', { name: ESCAPE })).toBeNull();
  });

  it('renders the footer hint only when one is passed', () => {
    const HINT = 'Noch kein Konto? Bitte frag deine Lehrkraft.';
    const { unmount } = render(<LoginForm footerHint={HINT} />);
    expect(screen.getByText(HINT)).toBeInTheDocument();
    unmount();

    render(<LoginForm />);
    expect(screen.queryByText(HINT)).toBeNull();
  });
});
