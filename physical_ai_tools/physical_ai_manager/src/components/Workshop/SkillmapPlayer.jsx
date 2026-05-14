/*
 * Copyright 2025 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

import React, { useEffect, useState, useCallback } from 'react';
import { useDispatch, useSelector } from 'react-redux';
import toast from 'react-hot-toast';
import {
  setActiveTutorial,
  advanceTutorialStep,
  setRestrictedBlocks,
} from '../../features/workshop/workshopSlice';
import { updateTutorialProgress } from '../../services/tutorialApi';
import useSupabaseTutorialProgress from '../../hooks/useSupabaseTutorialProgress';
import { DE } from './blocks/messages_de';
import { TUTORIAL_INDEX, loadTutorial } from './tutorialIndex';

/**
 * Sidebar that lists the bundled tutorials and steps the student
 * through the active one. Each step's `allowed_blocks` array (from
 * the markdown frontmatter) becomes the toolbox restriction so
 * beginners aren't overwhelmed by the full block library.
 */
function SkillmapPlayer() {
  const dispatch = useDispatch();
  const accessToken = useSelector((s) => s.auth?.session?.access_token);
  const activeTutorialId = useSelector((s) => s.workshop.activeTutorialId);
  const activeStep = useSelector((s) => s.workshop.activeTutorialStep);

  // Audit U1: use the realtime hook so progress is live (teacher
  // dashboard, parallel browser tab, etc. all stay in sync) and the
  // hook handles the token-rotation race correctly. The fallback poll
  // keeps the screen accurate even when Supabase Realtime is offline.
  const { progress, refetch: refetchProgress } = useSupabaseTutorialProgress();
  const [tutorial, setTutorial] = useState(null);

  // Load active tutorial body when the id changes.
  useEffect(() => {
    if (!activeTutorialId) {
      setTutorial(null);
      dispatch(setRestrictedBlocks(null));
      return;
    }
    let cancelled = false;
    loadTutorial(activeTutorialId)
      .then((doc) => {
        if (cancelled) return;
        setTutorial(doc);
        const step = doc?.steps?.[activeStep] || null;
        dispatch(setRestrictedBlocks(step?.allowed_blocks || null));
      })
      .catch((e) => {
        if (cancelled) return;
        toast.error(`Lernpfad konnte nicht geladen werden: ${e.message || e}`);
      });
    return () => { cancelled = true; };
  }, [activeTutorialId, activeStep, dispatch]);

  const handleStart = useCallback(
    (id) => {
      dispatch(setActiveTutorial({ id, step: 0 }));
    },
    [dispatch]
  );

  const handleNext = useCallback(async () => {
    if (!tutorial) return;
    const nextStep = activeStep + 1;
    const isLast = nextStep >= (tutorial.steps?.length || 0);
    if (isLast) {
      dispatch(setActiveTutorial({ id: null, step: 0 }));
      try {
        if (accessToken) {
          await updateTutorialProgress(accessToken, tutorial.id, {
            current_step: tutorial.steps.length,
            completed: true,
          });
          // Audit U2: trigger an immediate refetch so the sidebar
          // tick appears without waiting for the realtime channel
          // (which may take up to a second on a slow link, or never
          // arrive if the publication is misconfigured). The realtime
          // path is best-effort; this is the authoritative reconcile.
          refetchProgress?.();
        }
        toast.success(DE.TUTORIAL_DONE);
      } catch (e) {
        // Cloud sync failed — surface a German hint so the student knows
        // their completion may not be visible to the teacher dashboard
        // until they retry. Local Redux state is untouched.
        toast.error('Fortschritt konnte nicht gespeichert werden — bitte erneut starten.');
      }
      return;
    }
    dispatch(advanceTutorialStep());
    if (accessToken) {
      try {
        await updateTutorialProgress(accessToken, tutorial.id, {
          current_step: nextStep,
        });
        refetchProgress?.();
      } catch (e) {
        toast.error('Fortschritt konnte nicht gespeichert werden.');
      }
    }
  }, [accessToken, activeStep, dispatch, refetchProgress, tutorial]);

  const handlePrev = useCallback(() => {
    if (activeStep > 0) {
      dispatch(setActiveTutorial({
        id: activeTutorialId,
        step: activeStep - 1,
      }));
    }
  }, [activeStep, activeTutorialId, dispatch]);

  const handleStop = useCallback(() => {
    dispatch(setActiveTutorial({ id: null, step: 0 }));
  }, [dispatch]);

  if (!activeTutorialId) {
    return (
      <aside
        className="bg-white rounded-lg border border-[var(--line)] p-3 sm:p-4 overflow-auto"
        aria-label={DE.SKILLMAP_TITLE}
      >
        <h2 className="text-base font-semibold mb-3">{DE.SKILLMAP_TITLE}</h2>
        <ol className="space-y-2">
          {TUTORIAL_INDEX.map((entry, idx) => {
            const done = progress[entry.id]?.completed_at;
            return (
              <li
                key={entry.id}
                className="flex items-center gap-2 px-2 py-2 rounded-md border border-[var(--line)]"
              >
                <span
                  className={
                    'w-6 h-6 rounded-full text-xs font-medium '
                    + 'flex items-center justify-center '
                    + (done
                      ? 'bg-green-100 text-green-700'
                      : 'bg-gray-100 text-gray-700')
                  }
                  aria-hidden="true"
                >
                  {done ? '✓' : idx + 1}
                </span>
                <div className="flex-1 min-w-0">
                  <p className="text-sm font-medium truncate">{entry.title_de}</p>
                  <p className="text-xs text-[var(--ink-4)]">Stufe {entry.level}</p>
                </div>
                <button
                  type="button"
                  onClick={() => handleStart(entry.id)}
                  className="text-xs px-2 py-1 rounded-md bg-[var(--accent)] text-white hover:opacity-90"
                >
                  ▶
                </button>
              </li>
            );
          })}
        </ol>
      </aside>
    );
  }

  if (!tutorial) {
    return (
      <aside className="bg-white rounded-lg border border-[var(--line)] p-3 sm:p-4">
        <p className="text-sm text-[var(--ink-3)]">Lernpfad lädt …</p>
      </aside>
    );
  }

  const stepInfo = tutorial.steps?.[activeStep] || null;
  const totalSteps = tutorial.steps?.length || 0;

  return (
    <aside
      className="bg-white rounded-lg border border-[var(--line)] p-3 sm:p-4 overflow-auto"
      aria-label={DE.SKILLMAP_TITLE}
    >
      <header className="mb-3">
        <h2 className="text-base font-semibold">{tutorial.title_de}</h2>
        <p className="text-xs text-[var(--ink-3)]">
          Schritt {activeStep + 1} von {totalSteps}
        </p>
      </header>
      {stepInfo ? (
        <article className="prose prose-sm max-w-none mb-4">
          <h3 className="text-sm font-semibold">{stepInfo.title}</h3>
          <p className="text-sm text-[var(--ink)] whitespace-pre-wrap">
            {stepInfo.body}
          </p>
          {Array.isArray(stepInfo.hints) && stepInfo.hints.length > 0 && (
            <details className="text-xs mt-2">
              <summary className="cursor-pointer text-[var(--ink-3)]">Tipp</summary>
              <ul className="ml-4 list-disc">
                {stepInfo.hints.map((hint, i) => (
                  <li key={i}>{hint}</li>
                ))}
              </ul>
            </details>
          )}
        </article>
      ) : (
        <p className="text-sm text-red-600 mb-4">Schritt nicht gefunden.</p>
      )}
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={handlePrev}
          disabled={activeStep === 0}
          className="px-3 py-1.5 text-sm rounded-md border border-[var(--line)] hover:bg-[var(--bg-sunk)] disabled:opacity-50"
        >
          ← {DE.TUTORIAL_PREV}
        </button>
        <button
          type="button"
          onClick={handleNext}
          className="px-3 py-1.5 text-sm rounded-md bg-[var(--accent)] text-white hover:opacity-90"
        >
          {activeStep + 1 >= totalSteps ? '✓' : DE.TUTORIAL_NEXT}
        </button>
        <button
          type="button"
          onClick={handleStop}
          className="ml-auto text-xs text-[var(--ink-4)] hover:underline"
        >
          beenden
        </button>
      </div>
    </aside>
  );
}

export default SkillmapPlayer;
