// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// The /daten-api/ proxy in BOTH nginx configs (the Windows student rig and the
// Orange Pi template): a link token rides in the URL PATH (/daten-api/v1/ds/<T>/…,
// valid 30 min), and nginx writes request lines into two logs (V1-6, T1-1):
//   * the access log, for every request — so the location logs none, and the
//     SERVER logs in a token-free format, because a request nginx answers before
//     it picks a location (a header too large, a bad request line, TRACE) or a
//     /DATEN-API/ spelling that falls through to the SPA is logged by the server,
//     and a tab opened at a Daten URL sends it as the Referer of its next request;
//   * the error log, which appends the request line to every error of a request
//     (the sidecar down, a refused method, a too-large body) — so the location's
//     errors go to /dev/null.
// Measured with the real nginx:1.27.5-alpine (fix round 2): with both halves no
// token in either log over 31 request shapes; drop the error_log line and the
// error log shows 9 tokens, drop the server access_log line and the access log 13.

import fs from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';

const ROOT = path.resolve(__dirname, '../../../..');
const CONFIGS = ['nginx.conf', 'nginx.opi.conf.template'];

function directives(text) {
  return text.split('\n').map((l) => l.replace(/#.*/, '').trim()).filter(Boolean);
}

function datenBlock(text) {
  const m = /location \/daten-api\/ \{([\s\S]*?)\n {4}\}/.exec(text);
  return m ? m[1] : null;
}

function serverBlock(text) {
  const m = /\nserver \{\n([\s\S]*)\n\}\s*$/.exec(text);
  return m ? m[1] : null;
}

function mapBlock(text, variable) {
  const m = new RegExp(`\\nmap \\$\\S+ \\$${variable} \\{([\\s\\S]*?)\\n\\}`).exec(text);
  return m ? m[1] : null;
}

function logFormat(text, name) {
  const m = new RegExp(`\\nlog_format ${name} ([\\s\\S]*?);\\n`).exec(text);
  return m ? m[1] : null;
}

// What the two maps do, in JavaScript: their one redacting key is `~*daten-api`.
function mapKeyMatches(block, sample) {
  const keys = directives(block).map((l) => /^"~\*([^"]+)"/.exec(l)).filter(Boolean);
  return keys.some((k) => new RegExp(k[1], 'i').test(sample));
}

const TOKEN = 'v1.eyJzIjoiZHM6bGVuYS9vbXhfZl93dWVyZmVsIn0.abcdefabcdefabcdefabcdefabcdef12';

describe.each(CONFIGS)('%s: the /daten-api/ block', (name) => {
  const text = fs.readFileSync(path.join(ROOT, name), 'utf8');
  const block = datenBlock(text);

  it('exists', () => {
    expect(block).not.toBeNull();
  });

  it('writes no access log (its URLs carry the link tokens)', () => {
    const lines = directives(block);
    expect(lines).toContain('access_log off;');
    expect(lines.filter((l) => l.startsWith('access_log'))).toEqual(['access_log off;']);
  });

  it('writes no error log: nginx appends the request line to every error it logs', () => {
    const lines = directives(block);
    expect(lines.filter((l) => l.startsWith('error_log'))).toEqual(['error_log /dev/null;']);
  });

  it('the parser has teeth: without the lines the checks fail', () => {
    const stripped = directives(block.replace(/\n\s*access_log off;/, '').replace(/\n\s*error_log \/dev\/null;/, ''));
    expect(stripped).not.toContain('access_log off;');
    expect(stripped.filter((l) => l.startsWith('error_log'))).toEqual([]);
  });
});

describe.each(CONFIGS)('%s: the server logs in a token-free format', (name) => {
  const text = fs.readFileSync(path.join(ROOT, name), 'utf8');
  const server = serverBlock(text);
  const format = logFormat(text, 'edubotics_tokenless');
  const requestMap = mapBlock(text, 'edubotics_log_request');
  const refererMap = mapBlock(text, 'edubotics_log_referer');

  it('the server replaces the http-level log with the token-free format', () => {
    expect(server).not.toBeNull();
    const own = directives(server).filter((l) => l.startsWith('access_log'));
    expect(own).toContain('access_log /var/log/nginx/access.log edubotics_tokenless;');
    // Every other access_log in the server is `off`: a location with its own
    // format would log its request lines again.
    expect(own.filter((l) => l !== 'access_log off;')).toEqual([
      'access_log /var/log/nginx/access.log edubotics_tokenless;',
    ]);
  });

  it('the format prints the redacted request line and Referer, never the raw ones', () => {
    expect(format).not.toBeNull();
    expect(format).toContain('$edubotics_log_request');
    expect(format).toContain('$edubotics_log_referer');
    for (const raw of ['$request"', '$request ', '$request_uri', '$uri', '$http_referer', '$args', '$query_string']) {
      expect(format).not.toContain(raw);
    }
  });

  it('both maps replace any spelling of the Daten API and keep everything else', () => {
    expect(requestMap).not.toBeNull();
    expect(refererMap).not.toBeNull();
    expect(directives(requestMap).some((l) => /^default\s+\$request;$/.test(l))).toBe(true);
    expect(directives(refererMap).some((l) => /^default\s+\$http_referer;$/.test(l))).toBe(true);
    for (const sample of [
      `GET /daten-api/v1/ds/${TOKEN}/thumb.jpg HTTP/1.1`,
      `TRACE /daten-api/v1/ds/${TOKEN}/summary HTTP/1.1`,
      `GET /DATEN-API/V1/DS/${TOKEN.toUpperCase()}/thumb.jpg HTTP/1.1`,
      `get /Daten-Api/v1/lib/${TOKEN}/library?ns=a HTTP/1.1`,
    ]) {
      expect(mapKeyMatches(requestMap, sample)).toBe(true);
    }
    expect(mapKeyMatches(refererMap, `http://localhost/daten-api/v1/ds/${TOKEN}/thumb.jpg`)).toBe(true);
    expect(mapKeyMatches(requestMap, 'GET /static/js/index.js HTTP/1.1')).toBe(false);
    expect(mapKeyMatches(refererMap, 'http://localhost/')).toBe(false);
    // The replacement values name no variable that could carry the path.
    for (const line of [...directives(requestMap), ...directives(refererMap)]) {
      if (line.startsWith('"~*')) expect(line).not.toMatch(/\$(request|uri|request_uri|http_referer|args)\b/);
    }
  });

  it('the parser has teeth: the raw format would fail', () => {
    const raw = text.replace(/"\$edubotics_log_request"/, '"$request"');
    expect(logFormat(raw, 'edubotics_tokenless')).toContain('$request"');
  });
});
