// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.

// The teacher web's Content Security Policy (F-46, 2026-11-11): the policy
// builder (scripts/csp-policy.mjs), its place in nginx.web.conf.template (every
// location with an add_header of its own carries it, like the five security
// headers), the report sink, and how Dockerfile.web and the envsh wire it.
// The real nginx:1.27.5-alpine entrypoint was driven by hand (both modes, the
// fail-closed starts, a Playwright click-through in ENFORCE mode with zero
// violations; docs/CLAUDE-CHANGELOG.md 2026-11-11); CI renders the template
// through the envsh and runs `nginx -t` (ci.yml::nginx-validate) and checks
// the built image's headers (ci.yml::teacher-web-build-validate).

import fs from 'fs';
import path from 'path';
import { buildWebCsp, originOf, REPORT_PATH } from '../../scripts/csp-policy.mjs';

const APP = path.resolve(__dirname, '../..');
const read = (name) => fs.readFileSync(path.join(APP, name), 'utf8');
const TEMPLATE = read('nginx.web.conf.template');
const ENVSH = read('nginx.web.csp.envsh');
const DOCKERFILE = read('Dockerfile.web');

const URLS = { supabaseUrl: 'https://abcdefgh.supabase.co', apiUrl: 'https://api.example.org' };
const directives = (policy) =>
  Object.fromEntries(policy.split('; ').map((d) => [d.split(' ')[0], d.split(' ').slice(1)]));

describe('scripts/csp-policy.mjs', () => {
  it('builds the policy from the bundle\'s own two URLs, nothing wider', () => {
    const d = directives(buildWebCsp(URLS));
    expect(d).toEqual({
      'default-src': ["'self'"],
      'script-src': ["'self'"],
      'style-src': ["'self'", "'unsafe-inline'"],
      'img-src': ["'self'", 'data:'],
      'font-src': ["'self'"],
      'connect-src': ["'self'", 'https://api.example.org', 'https://abcdefgh.supabase.co', 'wss://abcdefgh.supabase.co'],
      'object-src': ["'none'"],
      'base-uri': ["'self'"],
      'form-action': ["'self'"],
      'frame-ancestors': ["'none'"],
      'report-uri': [REPORT_PATH],
    });
    expect(REPORT_PATH).toBe('/csp-report');
  });

  it('never allows eval, an inline script, a wildcard or a Google host', () => {
    const policy = buildWebCsp(URLS);
    expect(policy).not.toMatch(/unsafe-eval|wasm-unsafe-eval|strict-dynamic|\*|google|gstatic|appspot|huggingface/);
    expect(directives(policy)['script-src']).toEqual(["'self'"]);
  });

  it('takes the ORIGIN of each URL (a path, a trailing slash or upper case never widen or break it)', () => {
    const d = directives(
      buildWebCsp({ supabaseUrl: 'https://ABC.supabase.co/', apiUrl: 'https://Api.Example.org:8443/v1/' }),
    );
    expect(d['connect-src']).toEqual(["'self'", 'https://api.example.org:8443', 'https://abc.supabase.co', 'wss://abc.supabase.co']);
  });

  it('a local http stack gets ws:, and one URL for both is listed once', () => {
    const d = directives(buildWebCsp({ supabaseUrl: 'http://127.0.0.1:54321', apiUrl: 'http://127.0.0.1:54321' }));
    expect(d['connect-src']).toEqual(["'self'", 'http://127.0.0.1:54321', 'ws://127.0.0.1:54321']);
  });

  it.each([
    ['', 'is not an absolute URL'],
    ['supabase.co', 'is not an absolute URL'],
    ['ftp://x.example', 'must be an http(s) URL'],
    // eslint-disable-next-line no-script-url -- the value under test
    ['javascript:alert(1)', 'must be an http(s) URL'],
    ['https://user:pw@x.example', 'must not carry credentials'],
  ])('refuses %j (the build fails, naming the variable)', (raw, why) => {
    expect(() => originOf('REACT_APP_SUPABASE_URL', raw)).toThrow(`REACT_APP_SUPABASE_URL ${why}`);
    expect(() => buildWebCsp({ ...URLS, supabaseUrl: raw })).toThrow('REACT_APP_SUPABASE_URL');
  });

  it('the policy holds nothing the envsh refuses (no quote, dollar, backslash or line break)', () => {
    expect(buildWebCsp(URLS)).not.toMatch(/["$\\\n]/);
  });
});

describe('nginx.web.conf.template carries the CSP', () => {
  // eslint-disable-next-line no-template-curly-in-string -- nginx/envsubst placeholders, not JS
  const CSP_LINE = 'add_header ${EDUBOTICS_CSP_HEADER} "${EDUBOTICS_CSP_POLICY}" always;';

  // Every block that sets an add_header of its own loses the server-level ones
  // (nginx inheritance), so the CSP sits wherever Permissions-Policy sits.
  it('wherever the five security headers are declared, the CSP is declared too', () => {
    const permissions = TEMPLATE.match(/add_header Permissions-Policy /g).length;
    expect(permissions).toBe(4); // server + /index.html + /version.json + /static/
    expect(TEMPLATE.split(CSP_LINE).length - 1).toBe(permissions);
    const bodies = TEMPLATE.split(/\n\s*location /)
      .slice(1)
      .map((block) => block.slice(0, block.indexOf('\n    }')));
    const withOwnHeaders = bodies.filter((body) => /add_header/.test(body));
    expect(withOwnHeaders).toHaveLength(3);
    expect(withOwnHeaders.filter((body) => !body.includes(CSP_LINE))).toEqual([]);
  });

  it('the only placeholders are $PORT and the two CSP variables the envsh exports', () => {
    const placeholders = new Set([...TEMPLATE.matchAll(/\$\{([A-Z_]+)\}/g)].map((m) => m[1]));
    expect([...placeholders].sort()).toEqual(['EDUBOTICS_CSP_HEADER', 'EDUBOTICS_CSP_POLICY', 'PORT']);
  });

  it('the report sink: POST only, rate-limited, 16 KB, logged in its own format', () => {
    const loc = TEMPLATE.match(/location = \/csp-report \{([\s\S]*?)\n {4}\}/)[1];
    expect(loc).toMatch(/limit_except POST \{ deny all; \}/);
    expect(loc).toMatch(/limit_req zone=edubotics_csp_report burst=\d+ nodelay;/);
    expect(loc).toMatch(/client_max_body_size 16k;/);
    expect(loc).toMatch(/access_log \/var\/log\/nginx\/access\.log edubotics_csp_report;/);
    expect(loc).toMatch(/proxy_pass http:\/\/unix:\/tmp\/edubotics-csp-report\.sock:\/;/);
    expect(loc).not.toMatch(/add_header/); // the server-level headers apply
    expect(TEMPLATE).toMatch(/log_format edubotics_csp_report escape=json '[^']*\$request_body';/);
    expect(TEMPLATE).toMatch(/listen unix:\/tmp\/edubotics-csp-report\.sock;/);
    expect(ENVSH).toMatch(/rm -f \/tmp\/edubotics-csp-report\.sock/);
  });
});

describe('the image wiring (Dockerfile.web + nginx.web.csp.envsh)', () => {
  it('the build writes the policy from the bundle\'s URLs, the image installs it and the envsh', () => {
    expect(DOCKERFILE).toMatch(/RUN node scripts\/csp-policy\.mjs > csp-policy\.txt/);
    expect(DOCKERFILE).toMatch(/COPY --from=build \/app\/csp-policy\.txt \/etc\/nginx\/edubotics\/csp-policy\.txt/);
    expect(DOCKERFILE).toMatch(/COPY nginx\.web\.csp\.envsh \/docker-entrypoint\.d\/18-edubotics-csp\.envsh/);
    expect(DOCKERFILE).toMatch(/chmod 0755 \/docker-entrypoint\.d\/18-edubotics-csp\.envsh/);
    expect(DOCKERFILE).toContain('NGINX_ENVSUBST_FILTER="^(PORT|EDUBOTICS_CSP_HEADER|EDUBOTICS_CSP_POLICY)$"');
    // Sourced before the image's own 20-envsubst-on-templates.sh (sort -V order).
    expect('18-edubotics-csp.envsh' < '20-envsubst-on-templates.sh').toBe(true);
  });

  it('report-only unless EDUBOTICS_CSP_ENFORCE is exactly 1; refuses a missing or unquotable policy', () => {
    expect(ENVSH).toMatch(/EDUBOTICS_CSP_ENFORCE:-\}" \| tr -d '\[:space:\]'\)" = "1" \]/);
    expect(ENVSH).toMatch(/EDUBOTICS_CSP_HEADER=Content-Security-Policy\n/);
    expect(ENVSH).toMatch(/EDUBOTICS_CSP_HEADER=Content-Security-Policy-Report-Only\n/);
    expect(ENVSH).toMatch(/export EDUBOTICS_CSP_HEADER EDUBOTICS_CSP_POLICY/);
    expect(ENVSH).toMatch(/if \[ ! -s "\$edubotics_csp_file" \]; then[\s\S]*?exit 1/);
    expect(ENVSH).toMatch(/\*'"'\* \| \*'\$'\* \| \*'\\'\*/);
  });
});
