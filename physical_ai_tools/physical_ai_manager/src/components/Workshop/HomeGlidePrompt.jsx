/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

import React, {
  createContext, useCallback, useContext, useEffect, useRef, useState,
} from 'react';
import ReactDOM from 'react-dom';
import toast from 'react-hot-toast';
import { useRosServiceCaller } from '../../hooks/useRosServiceCaller';

// The warned glide to the Grundstellung after a RE-LOCK.
//
// Three places hand the student a limp arm and then lock it again: „Tisch
// vermessen" (touch-off solve), „Arm festsetzen" after „Arm freischalten", and
// „✋ Vormachen" (offered ONCE at „Fertig", only after a confirmed re-lock of an
// arm the session released). The server now re-locks every one of them IN
// PLACE (on the OMX the re-torque used to snap the arm back to the controller's
// old setpoint in ~50 ms — the "jump to home"). Getting back to the
// Grundstellung is then a SEPARATE, visible step: this dialog warns first, counts
// down, and only then asks the server for a slow glide (/workshop/jog mode
// 'home', the same floor-aware route planner as the `home` block).
//
// The student can start at once („Jetzt fahren"), keep the arm where it is
// („Hier stehen lassen"), or stop a running glide („Stopp" → hand_guide(false),
// the universal manual exit, which also makes the server hold the arm where it
// stopped). Nothing here moves the arm without the countdown having been shown.

export const HOME_GLIDE_COUNTDOWN_S = 5;

function secondsLabel(n) {
  return n === 1 ? '1 Sekunde' : `${n} Sekunden`;
}

export function HomeGlideDialog({
  phase, secondsLeft, onGoNow, onStay, onStop, stopping,
}) {
  if (phase !== 'countdown' && phase !== 'moving') return null;
  const moving = phase === 'moving';
  // Portalled to <body>: the callers live inside the dock / the calibration
  // wizard, and a `fixed` overlay is only viewport-fixed while no ancestor
  // establishes a containing block (transform, filter, contain). A warning that
  // can be clipped by layout CSS is not a warning.
  const overlay = (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/60 backdrop-blur-sm"
      role="alertdialog"
      aria-modal="true"
      aria-labelledby="home-glide-title"
      aria-describedby="home-glide-text"
    >
      <div className="mx-4 max-w-md rounded-2xl border-2 border-amber-500 bg-white p-6 shadow-2xl">
        <div className="flex flex-col items-center gap-3 text-center">
          <div className="text-4xl" aria-hidden="true">⚠️</div>
          <h2 id="home-glide-title" className="text-xl font-bold text-amber-700">
            {moving
              ? 'Der Arm fährt in die Grundstellung …'
              : 'Achtung: Der Arm fährt gleich in die Grundstellung'}
          </h2>
          <p id="home-glide-text" className="text-sm text-gray-700">
            {moving
              ? 'Bitte Abstand halten. Mit „Stopp" hält der Arm sofort an und '
                + 'bleibt an seiner Position.'
              : 'Der Arm ist wieder fest und hält seine Position. Gleich fährt er '
                + 'langsam in die Grundstellung zurück. Nimm jetzt die Hände vom '
                + 'Arm und räume den Bereich um den Arm frei.'}
          </p>
          {!moving && (
            <p className="text-base font-semibold text-gray-900" role="timer" aria-live="polite">
              Start in {secondsLabel(secondsLeft)}
            </p>
          )}
          {moving ? (
            <button
              type="button"
              onClick={onStop}
              disabled={stopping}
              className="mt-1 rounded-xl bg-red-600 px-6 py-2.5 text-base font-semibold text-white shadow hover:bg-red-700 disabled:opacity-50"
            >
              {stopping ? 'Wird gestoppt …' : 'Stopp'}
            </button>
          ) : (
            <div className="mt-1 flex flex-wrap items-center justify-center gap-2">
              <button
                type="button"
                onClick={onGoNow}
                className="rounded-xl bg-amber-600 px-5 py-2.5 text-sm font-semibold text-white shadow hover:bg-amber-700"
              >
                Jetzt fahren
              </button>
              <button
                type="button"
                onClick={onStay}
                // Default focus on the choice that does NOT move the arm, so a
                // stray Enter/Space from the button that opened this cannot
                // start the glide early.
                // eslint-disable-next-line jsx-a11y/no-autofocus
                autoFocus
                className="rounded-xl border border-gray-300 bg-white px-5 py-2.5 text-sm font-medium text-gray-700 hover:bg-gray-50"
              >
                Hier stehen lassen
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
  if (typeof document === 'undefined' || !document.body) return overlay;
  return ReactDOM.createPortal(overlay, document.body);
}

/**
 * Owns the countdown → glide → result flow. Returns
 *   offerHomeGlide()  — open the warning (no-op while one is already open),
 *   homeGlideDialog   — the element to render (null while idle),
 *   homeGlideActive   — true while the warning or the glide is on screen.
 *
 * Unmounting during the COUNTDOWN cancels it (nothing moves). Unmounting during
 * the glide leaves the server-side glide running — it is slow and floor-checked,
 * and aborting it from a teardown would be a surprise of its own.
 */
export function useHomeGlidePrompt() {
  const { homeArm, handGuide } = useRosServiceCaller();
  const [phase, setPhase] = useState('idle'); // 'idle' | 'countdown' | 'moving'
  const [secondsLeft, setSecondsLeft] = useState(HOME_GLIDE_COUNTDOWN_S);
  const [stopping, setStopping] = useState(false);
  const phaseRef = useRef('idle');
  const stopRequestedRef = useRef(false);
  const mountedRef = useRef(true);
  const homeArmRef = useRef(homeArm);
  const handGuideRef = useRef(handGuide);
  useEffect(() => { homeArmRef.current = homeArm; }, [homeArm]);
  useEffect(() => { handGuideRef.current = handGuide; }, [handGuide]);
  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  const setPhaseBoth = useCallback((next) => {
    phaseRef.current = next;
    if (mountedRef.current) setPhase(next);
  }, []);

  const startGlide = useCallback(async () => {
    // Exactly ONE glide per offer: the countdown reaching zero and a click on
    // „Jetzt fahren" can land in the same tick.
    if (phaseRef.current !== 'countdown') return;
    setPhaseBoth('moving');
    stopRequestedRef.current = false;
    const fn = homeArmRef.current;
    try {
      if (typeof fn !== 'function') {
        toast.error('Die Fahrt in die Grundstellung ist zurzeit nicht verfügbar.');
        return;
      }
      const res = await fn();
      // The RESULT decides, not the click: a „Stopp" can lose the race against
      // the glide's own start (a double-click), and then the arm DID go home —
      // saying „gestoppt" there would be false.
      if (res && res.success) {
        toast.success(res.message || 'Der Arm steht in der Grundstellung.');
      } else if (stopRequestedRef.current) {
        toast((res && res.message) || 'Fahrt gestoppt.', { icon: '✋' });
      } else {
        toast.error((res && res.message) || 'Fahrt in die Grundstellung nicht möglich.');
      }
    } catch (e) {
      toast.error(`Fahrt in die Grundstellung fehlgeschlagen: ${e.message || e}`);
    } finally {
      if (mountedRef.current) setStopping(false);
      setPhaseBoth('idle');
    }
  }, [setPhaseBoth]);

  const offerHomeGlide = useCallback(() => {
    if (phaseRef.current !== 'idle') return;
    setSecondsLeft(HOME_GLIDE_COUNTDOWN_S);
    setPhaseBoth('countdown');
  }, [setPhaseBoth]);

  useEffect(() => {
    if (phase !== 'countdown') return undefined;
    if (secondsLeft <= 0) {
      startGlide();
      return undefined;
    }
    const id = setTimeout(() => setSecondsLeft((s) => s - 1), 1000);
    return () => clearTimeout(id);
  }, [phase, secondsLeft, startGlide]);

  const stay = useCallback(() => {
    if (phaseRef.current !== 'countdown') return;
    setPhaseBoth('idle');
    toast('Der Arm bleibt an seiner Position.', { icon: '🦾' });
  }, [setPhaseBoth]);

  const stop = useCallback(async () => {
    if (phaseRef.current !== 'moving') return;
    stopRequestedRef.current = true;
    setStopping(true);
    const fn = handGuideRef.current;
    try {
      if (typeof fn === 'function') await fn(false);
    } catch (e) {
      toast.error(`Stopp fehlgeschlagen: ${e.message || e}`);
    }
  }, []);

  const homeGlideDialog = phase === 'idle' ? null : (
    <HomeGlideDialog
      phase={phase}
      secondsLeft={secondsLeft}
      onGoNow={startGlide}
      onStay={stay}
      onStop={stop}
      stopping={stopping}
    />
  );

  return { offerHomeGlide, homeGlideDialog, homeGlideActive: phase !== 'idle' };
}

// ONE prompt per Roboter-Studio page. The three callers do not live long
// enough to own a countdown themselves: a successful touch-off advances the
// wizard (unmounting TableTouchStep), „Überspringen" on the verify step swaps
// the wizard for the editor, the dock panel hosting JogPanel can be closed, and
// the Vormachen overlay unmounts the moment it offers the glide. WorkshopPage
// outlives all three, so it hosts the provider.
const HomeGlideContext = createContext(null);

export function HomeGlideProvider({ children }) {
  const glide = useHomeGlidePrompt();
  return (
    <HomeGlideContext.Provider value={glide}>
      {children}
      {glide.homeGlideDialog}
    </HomeGlideContext.Provider>
  );
}

/**
 * What a caller uses: the page-level prompt when a HomeGlideProvider is above
 * it, otherwise a component-local one (standalone mounts and tests), whose
 * `homeGlideDialog` the caller must render. Under a provider the returned
 * dialog is always null — the provider renders the only one.
 */
export function useHomeGlide() {
  const shared = useContext(HomeGlideContext);
  const local = useHomeGlidePrompt();
  if (shared) {
    return {
      offerHomeGlide: shared.offerHomeGlide,
      homeGlideDialog: null,
      homeGlideActive: shared.homeGlideActive,
    };
  }
  return local;
}

export default HomeGlideDialog;
