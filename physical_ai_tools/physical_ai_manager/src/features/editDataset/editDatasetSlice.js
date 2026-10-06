/*
 * Copyright 2025 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 *
 * Author: Kiwoong Park
 */

// The Daten tab's Redux state (Daten 2.0, spec §G3). Only what must outlive a
// tab switch or reach code outside the tab lives here: the HF worker's status
// (PolicyDownloadModal and the global HF hook read it), the upload progress the
// card shows, which view and dataset are open, the episode marks, the transfers
// the Daten page itself started, and the library filter. Library entries,
// summaries, hub results, link tokens and /edubotics/daten_state live in hook
// state inside the feature, so nothing outside the tab re-renders on them.
//
// Marks belong to ONE version of a dataset (R-9): each set carries the
// `meta_digest` of the summary it was made on; a summary with another digest
// drops them (the page says `copy.marks.cleared`).
//
// Nothing here is persisted, so `initialState` carries no storage snapshot and
// a whole-slice reset answers `session/signedOut`.

import { createSlice } from '@reduxjs/toolkit';
import { signedOut } from '../session/sessionActions';

export const LIBRARY_FILTERS = Object.freeze(['all', 'mine', 'group']);

const initialState = {
  hfStatus: 'Idle',
  uploadStatus: {
    current: 0,
    total: 0,
    percentage: 0.0,
  },
  view: 'library',
  openId: null,
  marks: {},
  transfers: {},
  libraryFilter: 'all',
};

const editDatasetSlice = createSlice({
  name: 'editDataset',
  initialState,
  reducers: {
    setHFStatus: (state, action) => {
      state.hfStatus = action.payload;
    },
    setUploadStatus: (state, action) => {
      state.uploadStatus = action.payload;
    },
    // The player for dataset `id`.
    openPlayer: (state, action) => {
      state.view = 'player';
      state.openId = action.payload;
    },
    showLibrary: (state) => {
      state.view = 'library';
      state.openId = null;
    },
    setLibraryFilter: (state, action) => {
      if (LIBRARY_FILTERS.includes(action.payload)) state.libraryFilter = action.payload;
    },
    // {id, digest, index}: mark or unmark one episode of the version `digest`.
    toggleMark: (state, action) => {
      const { id, digest, index } = action.payload || {};
      if (!id || !Number.isInteger(index) || index < 0) return;
      const cur = state.marks[id];
      const indices = cur && cur.digest === digest ? cur.indices.slice() : [];
      const at = indices.indexOf(index);
      if (at >= 0) indices.splice(at, 1);
      else indices.push(index);
      indices.sort((a, b) => a - b);
      state.marks[id] = { digest, indices };
    },
    // {id, digest, indices}: replace the marks of one dataset version.
    setMarks: (state, action) => {
      const { id, digest, indices } = action.payload || {};
      if (!id) return;
      const clean = [...new Set((indices || []).filter((i) => Number.isInteger(i) && i >= 0))].sort((a, b) => a - b);
      state.marks[id] = { digest, indices: clean };
    },
    clearMarks: (state, action) => {
      delete state.marks[action.payload];
    },
    // {repoId, kind: 'upload'|'download', id, via?}: a transfer the Daten page
    // started. The global HF hook does not toast it (the page does) and
    // registers a Daten upload with its own metadata (spec §E5).
    addTransfer: (state, action) => {
      const { repoId, kind, id, via } = action.payload || {};
      if (!repoId || (kind !== 'upload' && kind !== 'download')) return;
      state.transfers[repoId] = {
        kind, id: id || repoId, via: via || null, startedAt: Date.now(), result: null,
      };
    },
    // {repoId, status: 'Success'|'Failed', message}: its terminal HF status.
    setTransferResult: (state, action) => {
      const { repoId, status, message } = action.payload || {};
      const t = state.transfers[repoId];
      if (!t) return;
      t.result = { status: String(status || ''), message: String(message || ''), at: Date.now() };
    },
    removeTransfer: (state, action) => {
      delete state.transfers[action.payload];
    },
  },
  extraReducers: (builder) => {
    builder.addCase(signedOut, () => initialState);
  },
});

export const {
  setHFStatus,
  setUploadStatus,
  openPlayer,
  showLibrary,
  setLibraryFilter,
  toggleMark,
  setMarks,
  clearMarks,
  addTransfer,
  setTransferResult,
  removeTransfer,
} = editDatasetSlice.actions;

const EMPTY = Object.freeze([]);

/** The marks of dataset `id` for version `digest` (an empty list for another version). */
export const selectMarks = (id, digest) => (s) => {
  const m = s.editDataset && s.editDataset.marks ? s.editDataset.marks[id] : null;
  return m && m.digest === digest ? m.indices : EMPTY;
};

export default editDatasetSlice.reducer;
