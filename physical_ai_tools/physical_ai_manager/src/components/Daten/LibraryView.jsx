// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// „Deine Datensätze" (the approved mockup's library, spec §C1, §G13): one list
// of the student's and the group's datasets on this robot and on Hugging Face,
// the filters, the search, the free disk, the online list's age and refresh,
// the banners for a token that is not this student's or a failed online list,
// and the merge mode.

import React, { useEffect, useMemo, useState } from 'react';
import Icon from '../icons/Icon';
import DatasetCard from './DatasetCard';
import MergePanel from './MergePanel';
import { releasePointerFocus } from '../Record/ActionBar';
import COPY from '../../features/editDataset/datenCopy';
import { fill, fmtGB, minutesSince, plural } from '../../features/editDataset/model/format';

function RefreshLink({ loading, hubAt, onRefresh }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 30000);
    return () => clearInterval(id);
  }, []);
  let text = COPY.page.refresh;
  if (loading) text = COPY.page.refreshLoading;
  else if (hubAt) {
    const m = minutesSince(hubAt, now);
    text = m < 1 ? COPY.page.refreshJust : fill(COPY.page.refreshAgo, { n: m });
  }
  return (
    <button
      type="button"
      className="dat-linkbtn"
      onClick={(e) => { releasePointerFocus(e); onRefresh(); }}
      disabled={loading}
      data-action="refresh"
    >
      <Icon name={loading ? 'loading' : 'refresh'} size={14} className={loading ? 'animate-spin' : undefined} />
      {text}
    </button>
  );
}

export default function LibraryView({
  cards, models, counts, filter, onFilter, query, onQuery, diskFree, hub, hubAt, hubLoading,
  inSync, onRefresh, onFetch, merge, onToggleMerge, onMergePick, mergeEntries, mergeExisting,
  onMergeGo, onAction, onMenu, onMenuOpenChange, peekToken, getToken, own, ownerNames, robotType,
  sidecarDown, loaded,
}) {
  const visible = useMemo(() => {
    const q = String(query || '').trim().toLowerCase();
    return cards.filter((c) => {
      if (filter === 'mine' && c.ns !== own) return false;
      if (filter === 'group' && c.ns === own) return false;
      if (!q) return true;
      const title = String((c.local && c.local.display_name) || c.name || '').toLowerCase();
      return title.includes(q) || c.id.toLowerCase().includes(q);
    });
  }, [cards, filter, query, own]);

  const hubFailed = hub && (hub.state === 'unreachable' || hub.state === 'auth');
  return (
    <div className="dat-main" data-view="library">
      <header className="dat-head">
        <div>
          <div className="dat-eyebrow">{COPY.page.eyebrow}</div>
          <h1 className="dat-title">{COPY.page.title}</h1>
          <p className="dat-sub">
            {fill(COPY.page.sub, {
              datasets: plural(counts.all, COPY.count.datasetOne, COPY.count.datasetMany),
              changed: counts.changed,
              local: counts.local,
            })}
          </p>
        </div>
        <div className="dat-head-actions">
          <button
            type="button"
            className="dat-btn"
            onClick={(e) => { releasePointerFocus(e); onFetch(); }}
            disabled={!inSync}
            title={inSync ? undefined : COPY.lib.tokenNotActive}
            data-action="fetch"
          >
            <Icon name="cloudDownload" size={16} />
            {COPY.page.fetch}
          </button>
          <button
            type="button"
            className={`dat-btn${merge.active ? ' dat-btn-primary' : ''}`}
            aria-pressed={merge.active ? 'true' : 'false'}
            onClick={(e) => { releasePointerFocus(e); onToggleMerge(); }}
            data-action="merge-mode"
          >
            <Icon name="mergeData" size={16} />
            {COPY.page.merge}
          </button>
        </div>
      </header>

      {sidecarDown ? (
        <div className="dat-banner dat-warn" role="status">
          <Icon name="loading" size={16} className="animate-spin" />
          <span className="dat-grow">{COPY.old.sidecar}</span>
        </div>
      ) : null}
      {!inSync ? (
        <div className="dat-banner dat-warn" role="status" data-banner="token">
          <Icon name="key" size={16} />
          <span className="dat-grow">{COPY.lib.tokenNotActive}</span>
        </div>
      ) : null}
      {inSync && hubFailed ? (
        <div className="dat-banner dat-warn" role="status" data-banner="hub">
          <Icon name="warning" size={16} />
          <span className="dat-grow">{COPY.lib.hubFailed}</span>
        </div>
      ) : null}

      <div className="dat-toolbar">
        <div className="dat-seg" role="group" aria-label={COPY.page.filterLabel}>
          {[['all', COPY.page.filterAll], ['mine', COPY.page.filterMine], ['group', COPY.page.filterGroup]].map(([k, l]) => (
            <button
              key={k}
              type="button"
              aria-pressed={filter === k ? 'true' : 'false'}
              onClick={(e) => { releasePointerFocus(e); onFilter(k); }}
            >
              {l}
            </button>
          ))}
        </div>
        <label className="dat-search">
          <Icon name="find" size={16} />
          <input
            type="search"
            placeholder={COPY.page.search}
            aria-label={COPY.page.search}
            value={query}
            onChange={(e) => onQuery(e.target.value)}
          />
        </label>
        <div className="dat-tb-meta">
          {diskFree !== null && diskFree !== undefined ? (
            <span className="dat-chip" title={COPY.lib.diskFreeTitle} data-chip="disk">
              <Icon name="hardDrive" size={14} />
              {fill(COPY.lib.diskFree, { gb: fmtGB(diskFree) })}
            </span>
          ) : null}
          <RefreshLink loading={hubLoading} hubAt={hubAt} onRefresh={onRefresh} />
        </div>
      </div>

      {hub && hub.hiddenCount > 0 ? (
        <p className="dat-small" style={{ margin: '0 0 12px' }}>{fill(COPY.lib.hiddenCount, { n: hub.hiddenCount })}</p>
      ) : null}

      <div className="dat-grid">
        {!loaded ? (
          <div className="dat-empty"><Icon name="loading" size={16} className="animate-spin" /> {COPY.lib.loading}</div>
        ) : cards.length === 0 ? (
          <div className="dat-empty">{COPY.lib.empty}</div>
        ) : visible.length === 0 ? (
          <div className="dat-empty">
            {String(query || '').trim() ? fill(COPY.page.noResultsFor, { q: String(query).trim() }) : COPY.page.noResults}
          </div>
        ) : visible.map((c) => (
          <DatasetCard
            key={c.id}
            model={models[c.id]}
            onAction={(action) => onAction(action, c)}
            onMenu={(item) => onMenu(item, c)}
            onMergePick={() => onMergePick(c.id)}
            onRefreshSync={inSync ? onRefresh : null}
            onMenuOpenChange={onMenuOpenChange}
            peekToken={peekToken}
            getToken={getToken}
          />
        ))}
      </div>

      {merge.active ? (
        <MergePanel
          key={merge.ids.join('|')}
          entries={mergeEntries}
          own={own}
          ownerNames={ownerNames}
          robotType={robotType}
          existingIds={mergeExisting}
          onGo={onMergeGo}
          onCancel={onToggleMerge}
        />
      ) : null}
    </div>
  );
}
