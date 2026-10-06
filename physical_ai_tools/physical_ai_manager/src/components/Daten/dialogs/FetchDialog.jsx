// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// „Von Hugging Face holen" (owner decision D12, spec §E7): any dataset by its
// repo id — a teacher's or a public one — saved as the student's OWN copy.
// The id is checked here first (REPO_ID_RE); „Suchen" asks the robot's
// sidecar (`hub/probe`), which answers what it found or why it cannot be
// opened (not found or no access, another robot, an old or unknown format,
// Hugging Face unreachable, no token). „Speichern als" shows where the copy
// lands and refuses a name that exists here; a name that exists online only
// warns (the upload asks later).

import React, { useEffect, useRef, useState } from 'react';
import Dialog from './Dialog';
import Fill from '../Fill';
import Icon from '../../icons/Icon';
import { releasePointerFocus } from '../../Record/ActionBar';
import COPY from '../../../features/editDataset/datenCopy';
import { REPO_ID_RE } from '../../../features/editDataset/datenContract';
import {
  fill, fmtBytes, fmtDay, fmtFps, fmtGB, fmtTime,
} from '../../../features/editDataset/model/format';
import { camerasPhrase, nameFromRepo, robotName } from '../../../features/editDataset/model/labels';
import { safeTaskName } from '../../../utils/datasetName';

const REPO = new RegExp(REPO_ID_RE);

function Refusal({ result, repo, robotType }) {
  const r = result.refusal;
  const mono = (t) => <span className="dat-mono">{t}</span>;
  let title = null;
  let text = null;
  if (r === 'invalid') {
    text = <Fill template={COPY.fetch.invalid} values={{ pattern: mono(COPY.fetch.invalidPattern), example: mono(COPY.fetch.invalidExample) }} />;
  } else if (r === 'not_found') {
    title = COPY.fetch.notFoundTitle; text = COPY.fetch.notFound;
  } else if (r === 'other_robot') {
    title = COPY.fetch.otherRobotTitle;
    text = fill(COPY.fetch.otherRobot, { repo, robot: result.robot_type || '–', ours: robotName(robotType) });
  } else if (r === 'old_format') {
    title = COPY.fetch.oldFormatTitle; text = fill(COPY.fetch.oldFormat, { repo });
  } else if (r === 'unsupported') {
    title = COPY.fetch.unsupportedTitle; text = fill(COPY.fetch.unsupported, { repo });
  } else if (r === 'unreachable') {
    text = COPY.fetch.unreachable;
  } else if (r === 'no_token') {
    text = COPY.fetch.noToken;
  } else {
    text = COPY.http.generic;
  }
  return (
    <div className="dat-found dat-bad" data-refusal={r || 'error'}>
      {title ? <h3><Icon name="cancel" size={16} />{title}</h3> : null}
      {text}
    </div>
  );
}

export default function FetchDialog({
  probeRepo, own, robotType, localIds, hubIds, diskFree, onFetch, onClose,
}) {
  const [repo, setRepo] = useState('');
  const [state, setState] = useState({ kind: 'idle' }); // idle | searching | found | refused
  const [saveName, setSaveName] = useState('');
  const reqRef = useRef(0);
  useEffect(() => () => { reqRef.current += 1; }, []);

  const search = async () => {
    const id = repo.trim();
    const req = ++reqRef.current;
    if (!REPO.test(id)) {
      setState({ kind: 'refused', result: { refusal: 'invalid' }, repo: id });
      return;
    }
    setState({ kind: 'searching' });
    let result;
    try {
      result = await probeRepo(id);
    } catch {
      result = { found: false, refusal: 'error' };
    }
    if (req !== reqRef.current) return;
    if (result && result.found && !result.refusal) {
      setSaveName(nameFromRepo(id, robotType));
      setState({ kind: 'found', result, repo: id });
    } else {
      setState({ kind: 'refused', result: result || { refusal: 'error' }, repo: id });
    }
  };

  const found = state.kind === 'found' ? state.result : null;
  const name = saveName.trim();
  const safe = safeTaskName(name);
  const target = safe ? `${own}/${robotType}_${safe}` : '';
  const clash = !!target && localIds && localIds.has(target);
  const online = !!target && !clash && hubIds && hubIds.has(target);
  const canGo = !!found && !!name && !!target && !clash;

  let preview = null;
  if (found) {
    if (!name || !target) preview = <span style={{ color: 'var(--danger-ink)' }}>{COPY.fetch.noName}</span>;
    else if (clash) preview = <span style={{ color: 'var(--danger-ink)' }}>{COPY.fetch.nameExists}</span>;
    else {
      const size = fmtBytes(found.size_bytes || 0);
      preview = diskFree !== null && diskFree !== undefined
        ? fill(COPY.fetch.savedAs, { id: target, size, free: fmtGB(diskFree) })
        : fill(COPY.fetch.savedAsNoDisk, { id: target, size });
    }
  }

  return (
    <Dialog title={COPY.fetch.title} icon="cloudDownload" iconTone="sky" wide onClose={onClose}>
      <p className="dat-small" style={{ marginTop: 0 }}>{COPY.fetch.sub}</p>
      <form
        className="dat-row2"
        onSubmit={(e) => { e.preventDefault(); search(); }}
      >
        <label className="dat-field">
          {COPY.fetch.repoLabel}
          <input
            value={repo}
            autoComplete="off"
            spellCheck={false}
            placeholder={COPY.fetch.placeholder}
            data-autofocus
            onChange={(e) => {
              setRepo(e.target.value);
              if (state.kind !== 'idle') setState({ kind: 'idle' });
            }}
          />
        </label>
        <button type="submit" className="dat-btn">{COPY.fetch.search}</button>
      </form>

      {state.kind === 'searching' ? (
        <div className="dat-found"><h3><Icon name="loading" size={16} className="animate-spin" />{COPY.fetch.searching}</h3></div>
      ) : null}
      {state.kind === 'refused' ? <Refusal result={state.result} repo={state.repo} robotType={robotType} /> : null}
      {found ? (
        <>
          <div className="dat-found dat-ok" data-found="">
            <h3>
              <Icon name="checkCircle" size={16} style={{ color: 'var(--success)' }} />
              <Fill template={COPY.fetch.found} values={{ repo: <span className="dat-mono">{state.repo}</span> }} />
            </h3>
            <dl className="dat-stats">
              <div><dt>{COPY.card.episodes}</dt><dd>{found.total_episodes ?? '–'}</dd></div>
              <div><dt>{COPY.card.duration}</dt><dd>{fmtTime(found.duration_s || 0, false)}</dd></div>
              <div><dt>{COPY.card.size}</dt><dd>{fmtBytes(found.size_bytes || 0)}</dd></div>
              <div><dt>{COPY.card.fps}</dt><dd>{fmtFps(found.fps)}</dd></div>
            </dl>
            <div className="dat-small" style={{ marginTop: 6 }}>
              {fill(COPY.fetch.foundLine, {
                robot: robotName(found.robot_type || robotType),
                cameras: camerasPhrase(found.cameras),
                vis: found.private ? COPY.fetch.visPrivate : COPY.fetch.visPublic,
                updated: fill(COPY.fetch.updated, { date: fmtDay(found.last_modified) }),
              })}
            </div>
          </div>
          <label className="dat-field" style={{ marginTop: 12 }}>
            {COPY.fetch.saveAs}
            <input value={saveName} autoComplete="off" onChange={(e) => setSaveName(e.target.value)} />
          </label>
          <div className="dat-small dat-mono" style={{ marginTop: 5 }} data-preview="">{preview}</div>
          {online ? <p className="dat-warnline">{COPY.fetch.onlineName}</p> : null}
        </>
      ) : null}

      <div className="dat-acts">
        <button type="button" className="dat-btn" onClick={(e) => { releasePointerFocus(e); onClose(); }}>{COPY.fetch.cancel}</button>
        <button
          type="button"
          className="dat-btn dat-btn-primary"
          disabled={!canGo}
          onClick={(e) => {
            releasePointerFocus(e);
            onFetch({ repo: state.repo, revision: found.sha, target, displayName: name });
          }}
        >
          <Icon name="cloudDownload" size={16} />
          {COPY.fetch.go}
        </button>
      </div>
    </Dialog>
  );
}
