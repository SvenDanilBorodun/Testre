// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.
//
// The teacher web's Content Security Policy (F-46, 2026-11-11), built from the
// SAME two URLs the bundle inlines (REACT_APP_CLOUD_API_URL,
// REACT_APP_SUPABASE_URL), so the policy can never name another API or project
// than the one the code talks to. Dockerfile.web runs it right after
// `npm run build` and the nginx image reads the result through
// nginx.web.csp.envsh. Node standard library only (CI runs it on the runner's
// own Node too).
//
//   node scripts/csp-policy.mjs            → the policy on stdout
//
// Every directive is as tight as the click-through allowed (CLAUDE.md, the
// React dual build bullet; docs/CLAUDE-CHANGELOG.md 2026-11-11):
//   * script-src 'self'          — Vite emits no inline script, nothing evals;
//   * style-src 'unsafe-inline'  — react-hot-toast (goober, in the entry chunk),
//     Blockly and its plugins inject <style> elements at run time;
//   * img-src data:              — the admin's TOTP QR code (Supabase answers a
//     data: URI) and Blockly's own SVG data icons;
//   * connect-src                — the API, Supabase over https and wss
//     (Realtime); nothing else: the teacher web only LINKS to Hugging Face;
//   * report-uri /csp-report     — logged by nginx itself (nginx.web.conf.template).
import { pathToFileURL } from 'node:url';

export const REPORT_PATH = '/csp-report';

/** The origin of one of the two build URLs, or an Error naming the variable. */
export function originOf(name, raw) {
  const value = String(raw ?? '').trim();
  let url;
  try {
    url = new URL(value);
  } catch {
    throw new Error(`${name} is not an absolute URL; the teacher web's CSP cannot be built.`);
  }
  if (url.protocol !== 'https:' && url.protocol !== 'http:') {
    throw new Error(`${name} must be an http(s) URL; the teacher web's CSP cannot be built.`);
  }
  if (url.username || url.password) {
    throw new Error(`${name} must not carry credentials; the teacher web's CSP cannot be built.`);
  }
  return url.origin;
}

/** The policy as one header value: directives joined by "; ". */
export function buildWebCsp({ supabaseUrl, apiUrl }) {
  const api = originOf('REACT_APP_CLOUD_API_URL', apiUrl);
  const supabase = originOf('REACT_APP_SUPABASE_URL', supabaseUrl);
  const realtime = supabase.replace(/^http/, 'ws'); // https → wss, http → ws
  const connect = [...new Set(["'self'", api, supabase, realtime])];
  return [
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data:",
    "font-src 'self'",
    `connect-src ${connect.join(' ')}`,
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
    `report-uri ${REPORT_PATH}`,
  ].join('; ');
}

const invokedDirectly = process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href;
if (invokedDirectly) {
  try {
    process.stdout.write(
      `${buildWebCsp({
        supabaseUrl: process.env.REACT_APP_SUPABASE_URL,
        apiUrl: process.env.REACT_APP_CLOUD_API_URL,
      })}\n`,
    );
  } catch (err) {
    console.error(`csp-policy: ${err.message}`);
    process.exit(1);
  }
}
