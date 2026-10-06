// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// The Daten sidecar over the manager's nginx (spec §B3, §B9): read-only
// GET/HEAD under the same origin's /daten-api/v1, every route but /health
// behind a link token minted over rosbridge (§B5). Errors are JSON
// `{"error": <HTTP_ERRORS code>}` and never echo a path; this module turns
// every failure into ONE code the page switches on:
//
//   an HTTP_ERRORS code   the sidecar's own answer (403 token_* / scope, 409
//                         in_session / unsupported / unplayable, 503
//                         overloaded, …)
//   'sidecar_down'        502 / 504 / no answer at all: nginx could not reach
//                         the sidecar, or the network failed (§B10)
//   'http'                any other status without a code
//
// The 503 rule (§B8): a JSON request waits out `Retry-After` and tries again;
// the thumbnail and clip retries live with their elements (THUMB_RETRY_DELAYS_MS,
// CLIP_RETRY_DELAY_MS), which learn the status through `classifyMedia`.

import { API_PREFIX, HTTP_ERRORS } from '../datenContract';

export const TOKEN_ERRORS = Object.freeze(['token_invalid', 'token_expired', 'scope']);
export const THUMB_RETRY_DELAYS_MS = Object.freeze([2000, 4000, 8000]);
export const CLIP_RETRY_DELAY_MS = 2000;
export const JSON_503_RETRIES = 2;
const DEFAULT_RETRY_AFTER_S = 2;
const MAX_RETRY_AFTER_S = 10;

export class DatenHttpError extends Error {
  constructor(code, status = 0, retryAfterS = null) {
    super(`daten-api ${status || 'network'} ${code}`);
    this.name = 'DatenHttpError';
    this.code = code;
    this.status = status;
    this.retryAfterS = retryAfterS;
  }
}

/** True for the three 403 answers that a fresh link token cures. */
export function isTokenError(code) {
  return TOKEN_ERRORS.includes(code);
}

const enc = (s) => encodeURIComponent(String(s));

function query(params) {
  const parts = [];
  Object.entries(params || {}).forEach(([k, v]) => {
    if (v === undefined || v === null || v === '') return;
    parts.push(`${enc(k)}=${Array.isArray(v) ? v.map(enc).join(',') : enc(v)}`);
  });
  return parts.length ? `?${parts.join('&')}` : '';
}

/** `/daten-api/v1/lib/<T>/<path>?…` */
export function libUrl(token, path, params) {
  return `${API_PREFIX}/lib/${enc(token)}/${path}${query(params)}`;
}

/** `/daten-api/v1/ds/<T>/<path>` */
export function dsUrl(token, path) {
  return `${API_PREFIX}/ds/${enc(token)}/${path}`;
}

/** The clip of episode `i`, camera index `c` (§B4). */
export function clipUrl(token, i, c) {
  return dsUrl(token, `episode/${Number(i)}/video/${Number(c)}.mp4`);
}

export function thumbUrl(token) {
  return dsUrl(token, 'thumb.jpg');
}

function retryAfterOf(response) {
  const raw = response && response.headers && typeof response.headers.get === 'function'
    ? response.headers.get('Retry-After') : null;
  const v = Number(raw);
  return Number.isFinite(v) && v >= 0 ? Math.min(v, MAX_RETRY_AFTER_S) : DEFAULT_RETRY_AFTER_S;
}

async function errorCodeOf(response) {
  const status = response.status;
  if (status === 502 || status === 504) return 'sidecar_down';
  let code = '';
  try {
    const body = await response.json();
    code = body && typeof body.error === 'string' ? body.error : '';
  } catch {
    code = '';
  }
  if (HTTP_ERRORS.includes(code)) return code;
  return status === 503 ? 'overloaded' : 'http';
}

const defaultSleep = (ms) => new Promise((resolve) => { setTimeout(resolve, ms); });

/**
 * GET a JSON route. Resolves with the parsed body; rejects with a
 * DatenHttpError. A 503 is retried after its Retry-After, at most
 * `JSON_503_RETRIES` times. An abort rejects with the AbortError itself.
 */
export async function getJson(url, { signal, fetchImpl, sleep = defaultSleep } = {}) {
  const doFetch = fetchImpl || ((...a) => fetch(...a));
  for (let attempt = 0; ; attempt += 1) {
    let response;
    try {
      response = await doFetch(url, { method: 'GET', signal, credentials: 'same-origin', cache: 'no-store' });
    } catch (err) {
      if (err && err.name === 'AbortError') throw err;
      throw new DatenHttpError('sidecar_down', 0);
    }
    if (response.ok) {
      try {
        return await response.json();
      } catch {
        throw new DatenHttpError('http', response.status);
      }
    }
    const code = await errorCodeOf(response);
    if (response.status === 503 && attempt < JSON_503_RETRIES) {
      await sleep(retryAfterOf(response) * 1000);
      if (signal && signal.aborted) {
        const abort = new Error('aborted');
        abort.name = 'AbortError';
        throw abort;
      }
      continue;
    }
    throw new DatenHttpError(code, response.status, response.status === 503 ? retryAfterOf(response) : null);
  }
}

/**
 * Why a <video> or <img> failed to load `url`: one ranged GET (a byte of the
 * file, or the JSON error) → `{status, code}` with `code` '' when it loads
 * fine now. Never throws.
 */
export async function classifyMedia(url, { signal, fetchImpl } = {}) {
  const doFetch = fetchImpl || ((...a) => fetch(...a));
  try {
    const response = await doFetch(url, {
      method: 'GET', signal, credentials: 'same-origin', cache: 'no-store', headers: { Range: 'bytes=0-0' },
    });
    if (response.ok) return { status: response.status, code: '' };
    return { status: response.status, code: await errorCodeOf(response) };
  } catch (err) {
    if (err && err.name === 'AbortError') return { status: 0, code: 'aborted' };
    return { status: 0, code: 'sidecar_down' };
  }
}

/** GET /daten-api/v1/health → true when the sidecar answers. */
export async function sidecarHealthy({ fetchImpl } = {}) {
  const doFetch = fetchImpl || ((...a) => fetch(...a));
  try {
    const response = await doFetch(`${API_PREFIX}/health`, { method: 'GET', cache: 'no-store' });
    return response.ok;
  } catch {
    return false;
  }
}
