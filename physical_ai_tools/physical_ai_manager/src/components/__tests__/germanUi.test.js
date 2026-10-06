// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// Review round 1 (owner decision R1-O2): the data tools and panels this PR
// touched are German all through — not only the lines the icon work changed.
// A student reads every JSX text, placeholder, title, aria-label and toast of
// these files, so none of them may carry an English word from the list below
// (German UI / English code, Rule §1; german_detail_lint.py cannot see JSX).
// Developer logs (console.*, Error messages that are never rendered) are not
// judged. The vocabulary holds only words that are NOT also German UI words
// („Upload", „Download", „Status", „Token", „Policy", „Tags" and the key name
// „Enter" stay allowed).

import fs from 'fs';
import path from 'path';
import * as espree from 'espree';
import { describe, expect, it } from 'vitest';

const SRC = path.resolve(__dirname, '../..');

export const GERMAN_FILES = Object.freeze([
  'components/CompactSystemStatus.js',
  'components/DatasetSelector.js',
  // The per-student Hugging-Face-Token card on the Startseite and its table of
  // sentences (the JSX itself carries none: every string comes from the table).
  'components/Home/HfTokenCard.jsx',
  'components/FileBrowser.js',
  'components/FileBrowserModal.js',
  'components/ModelWeightSelector.js',
  'components/PolicyDownloadModal.js',
  // Aufnahme 2.0 (spec §3.15): every component of the new page. Their words
  // come from the page, so most of the scan falls on components/Record/model/
  // recordCopy.js; what these files still write themselves is judged here.
  'components/Record/ActionBar.jsx',
  'components/Record/CameraTiles.jsx',
  'components/Record/EpisodeDots.jsx',
  'components/Record/FinishCard.jsx',
  'components/Record/PhaseOverlay.jsx',
  'components/Record/PhaseTrack.jsx',
  'components/Record/ProblemBanner.jsx',
  'components/Record/RecordHeader.jsx',
  'components/Record/RecordStage.jsx',
  'components/Record/RecordingFrame.jsx',
  'components/Record/SavingChips.jsx',
  'components/Record/SessionCard.jsx',
  'components/Record/StageCard.jsx',
  'components/Record/StageViewSwitch.jsx',
  'components/Record/TaskCard.jsx',
  'components/Record/model/recordCopy.js',
  'components/SystemStatus.js',
  'components/TagInput.js',
  'constants/HFStatus.js',
  'features/hfToken/hfTokenCopy.js',
  // Daten 2.0 (spec §G9): every file of the new tab. Their words come from
  // features/editDataset/datenCopy.js; what they still write is judged here.
  'components/Daten/CardMenu.jsx',
  'components/Daten/DatasetCard.jsx',
  'components/Daten/DatenPage.jsx',
  'components/Daten/DetailsPanel.jsx',
  'components/Daten/EpisodeList.jsx',
  'components/Daten/Fill.jsx',
  'components/Daten/HintsPanel.jsx',
  'components/Daten/JointCharts.jsx',
  'components/Daten/KeysLegend.jsx',
  'components/Daten/LibraryView.jsx',
  'components/Daten/MergePanel.jsx',
  'components/Daten/PlayerView.jsx',
  'components/Daten/Scrubber.jsx',
  'components/Daten/Stage.jsx',
  'components/Daten/SyncBadge.jsx',
  'components/Daten/SyncBanners.jsx',
  'components/Daten/ToolsBar.jsx',
  'components/Daten/Transport.jsx',
  'components/Daten/datenToasts.jsx',
  'components/Daten/theme.js',
  'components/Daten/dialogs/ConfirmDeleteDataset.jsx',
  'components/Daten/dialogs/ConfirmDeleteMarked.jsx',
  'components/Daten/dialogs/ConflictChoices.jsx',
  'components/Daten/dialogs/Dialog.jsx',
  'components/Daten/dialogs/EpisodeToolDialog.jsx',
  'components/Daten/dialogs/FetchDialog.jsx',
  'components/Daten/dialogs/NewerWarnDialog.jsx',
  'components/Daten/dialogs/ProgressDialog.jsx',
  'components/Daten/dialogs/PullNewerDialog.jsx',
  'components/Daten/dialogs/UploadCompareDialog.jsx',
  'components/Daten/dialogs/useHubState.js',
  'features/editDataset/api/datenHttp.js',
  'features/editDataset/datenContract.js',
  'features/editDataset/datenCopy.js',
  'features/editDataset/editDatasetSlice.js',
  'features/editDataset/hooks/useDatasetLocalChanges.js',
  'features/editDataset/hooks/useDatenCommand.js',
  'features/editDataset/hooks/useDatenJobs.js',
  'features/editDataset/hooks/useDatenSession.js',
  'features/editDataset/hooks/useDatenStartWait.js',
  'features/editDataset/hooks/useDatenState.js',
  'features/editDataset/hooks/useEpisodePlayer.js',
  'features/editDataset/hooks/useGroupNamespaces.js',
  'features/editDataset/model/cardModel.js',
  'features/editDataset/model/chartData.js',
  'features/editDataset/model/episodeNumbers.js',
  'features/editDataset/model/format.js',
  'features/editDataset/model/hintText.js',
  'features/editDataset/model/hubLinks.js',
  'features/editDataset/model/labels.js',
  'features/editDataset/model/libraryState.js',
  'features/editDataset/model/mergeChecks.js',
  'features/editDataset/model/playerClock.js',
  'features/editDataset/model/syncBadge.js',
  'features/editDataset/renderProbe.js',
  'pages/EditDatasetPage.js',
  'pages/TrainingPage.js',
]);

const ENGLISH = /\b(the|and|please|select|selected|selectable|failed|failed to|loading|load|cancel|browse|directory|folder|file|merge|output|add|remove|refresh|empty|found|detected|usage|used|free|total|user|dataset|datasets|model|instruction|parent|home|current|path|already|exists|choose|different|existing|more|only|navigation|disabled|repository name|cannot|must|characters|letters|numbers|configuration|type|local|full|will|be|saved|switch|while|in progress|memory|storage|almost|high|very|consider|closing|applications|cleaning)\b/i;
const ATTRS = new Set(['placeholder', 'title', 'aria-label', 'alt', 'selectButtonText', 'targetFileLabel', 'ariaLabel']);
const TOASTS = /^toast(\.(success|error))?$/;

const calleeName = (c) => {
  if (!c) return '';
  if (c.type === 'Identifier') return c.name;
  if (c.type === 'MemberExpression') return `${calleeName(c.object)}.${c.property.name || ''}`;
  return '';
};

function strings(node, out) {
  if (!node || typeof node !== 'object') return out;
  if (Array.isArray(node)) {
    node.forEach((n) => strings(n, out));
    return out;
  }
  // A string handed to a function is data (a class name, a key, a log line),
  // unless the function is a toast; a JSX element inside is judged on its own.
  if (node.type === 'CallExpression' && !TOASTS.test(calleeName(node.callee))) return out;
  if (node.type === 'JSXElement') return out;
  // A comparison operand is code (`hfDataType === 'model'`), never shown.
  if (node.type === 'BinaryExpression' && ['===', '!==', '==', '!='].includes(node.operator)) return out;
  if (node.type === 'Literal' && typeof node.value === 'string') out.push({ text: node.value, line: node.loc.start.line });
  if (node.type === 'TemplateLiteral') node.quasis.forEach((q) => out.push({ text: q.value.cooked, line: q.loc.start.line }));
  for (const [k, v] of Object.entries(node)) {
    if (k !== 'loc' && k !== 'range' && v && typeof v === 'object') strings(v, out);
  }
  return out;
}

/** Every English-looking string a student reads in `source`: `[{line, text}]`. */
export function englishUiStrings(source) {
  const ast = espree.parse(source, { ecmaVersion: 'latest', sourceType: 'module', ecmaFeatures: { jsx: true }, loc: true });
  const seen = [];
  const visit = (node, parent) => {
    if (!node || typeof node !== 'object') return;
    if (Array.isArray(node)) {
      node.forEach((n) => visit(n, parent));
      return;
    }
    if (node.type === 'JSXText') seen.push({ text: node.value, line: node.loc.start.line });
    if (node.type === 'JSXAttribute' && ATTRS.has(node.name.name) && node.value) strings(node.value, seen);
    if (node.type === 'JSXExpressionContainer' && parent && (parent.type === 'JSXElement' || parent.type === 'JSXFragment')) {
      strings(node.expression, seen);
    }
    if (node.type === 'CallExpression' && TOASTS.test(calleeName(node.callee))) strings(node.arguments[0], seen);
    if (node.type === 'Property' && node.key && ['message', 'label'].includes(node.key.name) && node.value) {
      strings(node.value, seen);
    }
    for (const [k, v] of Object.entries(node)) {
      if (k !== 'loc' && k !== 'range' && v && typeof v === 'object') visit(v, node);
    }
  };
  visit(ast, null);
  return seen
    .map(({ text, line }) => ({ text: String(text).replace(/\s+/g, ' ').trim(), line }))
    .filter(({ text }) => text && ENGLISH.test(text));
}

describe('the data tools and panels speak German (review round 1, R1-O2)', () => {
  it('the detector sees each place a student reads, and leaves code alone', () => {
    const src = [
      'const a = <p>Selected File:</p>;',
      'const b = <input placeholder="Enter output directory" />;',
      "const c = <b>{busy ? 'Loading...' : 'Laden'}</b>;",
      "toast.error('Failed to browse directory');",
      "const d = { message: 'Repository name cannot contain \"--\"' };",
      "console.error('Failed to fetch');",
      "const e = <div className={clsx('text-sm', { 'bg-white': x })}>Ausgewählte Datei:</div>;",
      "if (e.key === 'Enter') submit();",
      "const f = <b>{kind === 'model' ? 'Modell' : 'Datensatz'}</b>;",
    ].join('\n');
    expect(englishUiStrings(src).map((h) => h.line)).toEqual([1, 2, 3, 4, 5]);
  });

  it.each(GERMAN_FILES)('%s', (rel) => {
    const hits = englishUiStrings(fs.readFileSync(path.join(SRC, rel), 'utf8'))
      .map(({ line, text }) => `${rel}:${line} ${JSON.stringify(text)}`);
    expect(hits).toEqual([]);
  });
});
