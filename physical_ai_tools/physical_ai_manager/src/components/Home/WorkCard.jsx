// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// „Deine Arbeit" — continuity between weekly lessons.
//
// Five counters and two „zuletzt" lines, all from the cloud (see
// hooks/useStudentWork). The page had none of this: a student returning after
// a week saw a robot picture and a button, and nothing that said what they had
// already built.
//
// TWO HONESTY RULES, both visible in the code below:
//
//  * `null` renders „—", never 0. A failed fetch and an empty portfolio are
//    different facts, and only one of them is about the student.
//
//  * When the student is in a workgroup, the trainings and the credits are the
//    GROUP'S, and the card says so. `/trainings/list` returns the group's rows
//    and TrainingJob carries no owner id, so this cannot be narrowed on the
//    client; `get_remaining_credits` deliberately returns the shared pool. Both
//    numbers are correct — presenting them unlabelled as personal would be the
//    lie. Datasets and programs ARE filtered to the student (both rows carry an
//    owner), so those stay strictly theirs either way.

import React from 'react';
import { useSelector } from 'react-redux';

import { Card, Pill, Stat } from '../EbUI';
import { relativeDe } from '../../hooks/useStudentWork';

/** „—" for unknown, the number otherwise. `0` is a real answer and prints. */
function fmt(n) {
  return typeof n === 'number' && Number.isFinite(n) ? String(n) : '—';
}

export default function WorkCard({ work, loading }) {
  const workgroupName = useSelector((s) => s.auth.workgroupName);
  const inGroup = !!useSelector((s) => s.auth.workgroupId);

  const {
    datasetCount, episodeCount, modelCount, workflowCount,
    creditsRemaining, creditsTotal, runningTrainings, lastDataset, lastWorkflow,
  } = work;

  const creditsTone = creditsRemaining === 0 ? 'danger'
    : (typeof creditsRemaining === 'number' && creditsRemaining > 0 ? 'success' : undefined);

  return (
    <Card
      title="Deine Arbeit"
      subtitle={inGroup
        ? `Training und Credits teilst du mit der Gruppe ${workgroupName || ''}`.trim()
        : undefined}
      right={runningTrainings > 0
        ? (
          <Pill tone="accent" dot>
            {runningTrainings === 1 ? '1 Training läuft' : `${runningTrainings} Trainings laufen`}
          </Pill>
        )
        : null}
    >
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-5">
        <Stat label="Aufnahmen" value={loading ? '…' : fmt(datasetCount)} trend="Datensätze" />
        <Stat label="Episoden" value={loading ? '…' : fmt(episodeCount)} trend="aufgezeichnet" />
        <Stat
          label="Modelle"
          value={loading ? '…' : fmt(modelCount)}
          trend={inGroup ? 'in der Gruppe' : 'fertig trainiert'}
        />
        <Stat label="Programme" value={loading ? '…' : fmt(workflowCount)} trend="Roboter Studio" />
        <Stat
          label={inGroup ? 'Credits der Gruppe' : 'Credits'}
          value={loading ? '…' : fmt(creditsRemaining)}
          tone={creditsTone}
          trend={typeof creditsTotal === 'number' ? `von ${creditsTotal} übrig` : 'übrig'}
        />
      </div>

      {(lastDataset || lastWorkflow) && (
        <div className="mt-5 pt-4 border-t border-dashed border-[var(--line)] flex flex-wrap gap-x-8 gap-y-1.5 text-xs text-[var(--ink-3)]">
          {lastDataset && (
            <span>
              Zuletzt aufgenommen:{' '}
              <span className="text-[var(--ink-2)]">{lastDataset.name}</span>
              {relativeDe(lastDataset.created_at) && ` · ${relativeDe(lastDataset.created_at)}`}
            </span>
          )}
          {lastWorkflow && (
            <span>
              Zuletzt programmiert:{' '}
              <span className="text-[var(--ink-2)]">{lastWorkflow.name}</span>
              {relativeDe(lastWorkflow.updated_at) && ` · ${relativeDe(lastWorkflow.updated_at)}`}
            </span>
          )}
        </div>
      )}

      {/* An empty portfolio is a real state and gets a real sentence — but only
          once we KNOW it is empty. All-null means the fetches failed, and that
          is not the moment to tell a student they have done nothing. */}
      {!loading && datasetCount === 0 && workflowCount === 0 && modelCount === 0 && (
        <p className="mt-5 pt-4 border-t border-dashed border-[var(--line)] text-xs text-[var(--ink-3)]">
          Noch nichts aufgenommen oder programmiert — hier siehst du später deine Datensätze,
          Modelle und Programme.
        </p>
      )}
    </Card>
  );
}
