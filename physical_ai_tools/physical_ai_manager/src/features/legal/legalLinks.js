// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.
//
// The three legal links (launch plan step 4, 2026-11-11). Their addresses are
// Railway variables on the cloud API (LEGAL_IMPRESSUM_URL, LEGAL_DATENSCHUTZ_URL,
// LEGAL_NUTZUNGSBEDINGUNGEN_URL), served as additive keys of the public
// GET /version (cloud_training_api/app/services/legal_links.py), which answers
// `Access-Control-Allow-Origin: *` so a Pi's SPA at any LAN address can read
// it. A link is shown exactly while its address is set; changing one needs no
// rebuild.
//
// A link must never break a page: no API URL, an offline robot, `?cloud=1`
// without internet, a timeout, an error status, a malformed answer or an older
// API without the keys all mean „no links" — no toast, no console line. One
// request per page load (a failure is asked again after LEGAL_RETRY_AFTER_MS),
// without credentials and without any browser storage.

import { useEffect, useState } from 'react';
import { CLOUD_API_URL } from '../../services/cloudConfig';

// The /version keys, in display order, with their German link text. The same
// table as the API's legal_links.LEGAL_LINKS and the GUI's
// update_checker.LEGAL_LINKS (robotis_ai_setup/tests/test_legal_links_lockstep.py).
export const LEGAL_LINK_FIELDS = Object.freeze([
  Object.freeze({ key: 'legal_impressum_url', label: 'Impressum' }),
  Object.freeze({ key: 'legal_datenschutz_url', label: 'Datenschutz' }),
  Object.freeze({ key: 'legal_nutzungsbedingungen_url', label: 'Nutzungsbedingungen' }),
]);

export const LEGAL_FETCH_TIMEOUT_MS = 8000;
export const LEGAL_RETRY_AFTER_MS = 60000;
export const LEGAL_URL_MAX_CHARS = 2048;

/** An address the API served, re-checked here: an https URL with a host and no
 *  credentials, else null (an href is never anything else). */
export function safeLegalHref(value) {
  if (typeof value !== 'string') return null;
  const trimmed = value.trim();
  if (!trimmed || trimmed.length > LEGAL_URL_MAX_CHARS) return null;
  let url;
  try {
    url = new URL(trimmed);
  } catch {
    return null;
  }
  if (url.protocol !== 'https:' || !url.hostname || url.username || url.password) return null;
  return url.href;
}

/** The links a /version body names, in display order: [{ key, label, href }]. */
export function parseLegalLinks(body) {
  if (!body || typeof body !== 'object' || Array.isArray(body)) return [];
  return LEGAL_LINK_FIELDS.flatMap(({ key, label }) => {
    const href = safeLegalHref(body[key]);
    return href ? [{ key, label, href }] : [];
  });
}

const EMPTY = Object.freeze([]);
let state = { links: null, promise: null, failedAt: null };

/** The links once known this page load, else null (a component's first render). */
export function knownLegalLinks() {
  return state.links;
}

async function fetchLegalLinks(apiUrl, fetchImpl) {
  const controller = typeof AbortController === 'function' ? new AbortController() : null;
  const timer = controller ? setTimeout(() => controller.abort(), LEGAL_FETCH_TIMEOUT_MS) : null;
  try {
    const response = await fetchImpl(`${apiUrl.replace(/\/+$/, '')}/version`, {
      method: 'GET',
      credentials: 'omit',
      ...(controller ? { signal: controller.signal } : {}),
    });
    if (!response || !response.ok) return null;
    return parseLegalLinks(await response.json());
  } catch {
    return null;
  } finally {
    if (timer) clearTimeout(timer);
  }
}

/**
 * The links, fetched once per page load and shared by every surface. Never
 * rejects: anything that goes wrong resolves to [].
 */
export function loadLegalLinks({ apiUrl = CLOUD_API_URL, fetchImpl = globalThis.fetch, now = Date.now } = {}) {
  if (state.links) return Promise.resolve(state.links);
  if (state.promise) return state.promise;
  if (!apiUrl || typeof fetchImpl !== 'function') return Promise.resolve(EMPTY);
  if (state.failedAt !== null && now() - state.failedAt < LEGAL_RETRY_AFTER_MS) return Promise.resolve(EMPTY);
  state.promise = fetchLegalLinks(apiUrl, fetchImpl).then((links) => {
    state.promise = null;
    if (links === null) {
      state.failedAt = now();
      return EMPTY;
    }
    state.links = Object.freeze(links);
    state.failedAt = null;
    return state.links;
  });
  return state.promise;
}

/** Test seam: forget what this page load learned. */
export function resetLegalLinksForTests() {
  state = { links: null, promise: null, failedAt: null };
}

/** The links for a component: [] until (and unless) the API names some. */
export function useLegalLinks() {
  const [links, setLinks] = useState(() => knownLegalLinks() || EMPTY);
  useEffect(() => {
    let alive = true;
    loadLegalLinks().then((next) => {
      if (alive) setLinks(next);
    });
    return () => {
      alive = false;
    };
  }, []);
  return links;
}
