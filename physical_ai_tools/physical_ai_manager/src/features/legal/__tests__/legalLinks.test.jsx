// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.

// The legal links (launch plan step 4, 2026-11-11): read from the public
// GET /version of the cloud API, shown exactly while set, and never able to
// break a page — an unreachable API, an error, a malformed answer or an older
// API mean „no links", without a toast or a console line.

import React from 'react';
import { act, render, screen, within } from '@testing-library/react';
import {
  LEGAL_FETCH_TIMEOUT_MS,
  LEGAL_LINK_FIELDS,
  LEGAL_RETRY_AFTER_MS,
  knownLegalLinks,
  loadLegalLinks,
  parseLegalLinks,
  resetLegalLinksForTests,
  safeLegalHref,
} from '../legalLinks';
import LegalLinks, { LEGAL_NAV_LABEL } from '../../../components/LegalLinks';
import LoginForm from '../../../components/LoginForm';

vi.mock('react-redux', () => ({ __esModule: true, useDispatch: () => vi.fn() }));
vi.mock('react-hot-toast', () => ({
  __esModule: true,
  default: { error: vi.fn(), success: vi.fn(), dismiss: vi.fn() },
}));
vi.mock('../../../lib/supabaseClient', () => ({
  __esModule: true,
  supabase: { auth: { signInWithPassword: vi.fn() } },
}));

const API = 'https://api.edubotics.test';
const ALL = {
  version: '2.40.0',
  legal_impressum_url: 'https://edubotics.example/impressum',
  legal_datenschutz_url: 'https://edubotics.example/datenschutz',
  legal_nutzungsbedingungen_url: 'https://edubotics.example/nutzungsbedingungen',
};

function okFetch(body) {
  return vi.fn(async () => ({ ok: true, json: async () => body }));
}

let consoleSpies;
beforeEach(() => {
  resetLegalLinksForTests();
  consoleSpies = ['error', 'warn', 'log'].map((m) => vi.spyOn(console, m));
});
afterEach(() => {
  // „no console line": nothing here may write to the console.
  for (const spy of consoleSpies) {
    expect(spy).not.toHaveBeenCalled();
    spy.mockRestore();
  }
  vi.useRealTimers();
});

describe('parsing what /version names', () => {
  it('the three keys in display order with their German text', () => {
    expect(LEGAL_LINK_FIELDS.map((f) => [f.key, f.label])).toEqual([
      ['legal_impressum_url', 'Impressum'],
      ['legal_datenschutz_url', 'Datenschutz'],
      ['legal_nutzungsbedingungen_url', 'Nutzungsbedingungen'],
    ]);
    expect(parseLegalLinks(ALL).map((l) => l.label)).toEqual(['Impressum', 'Datenschutz', 'Nutzungsbedingungen']);
  });

  it('only a set https address becomes a link; everything else is skipped', () => {
    expect(
      parseLegalLinks({
        legal_impressum_url: null,
        legal_datenschutz_url: 'https://edubotics.example/datenschutz',
        legal_nutzungsbedingungen_url: '',
      }),
    ).toEqual([{ key: 'legal_datenschutz_url', label: 'Datenschutz', href: 'https://edubotics.example/datenschutz' }]);
    for (const bad of [
      'http://edubotics.example/x',
      // eslint-disable-next-line no-script-url -- the value under test
      'javascript:alert(1)',
      'data:text/html,x',
      '/impressum',
      'https://user:pw@edubotics.example/',
      42,
      {},
      `https://edubotics.example/${'a'.repeat(2100)}`,
    ]) {
      expect(safeLegalHref(bad)).toBeNull();
    }
    for (const body of [null, undefined, 'x', [], 7, { version: '2.37.0' }]) {
      expect(parseLegalLinks(body)).toEqual([]);
    }
  });
});

describe('loading', () => {
  it('asks /version once, without credentials, and shares the answer', async () => {
    const fetchImpl = okFetch(ALL);
    const [a, b] = await Promise.all([
      loadLegalLinks({ apiUrl: `${API}/`, fetchImpl }),
      loadLegalLinks({ apiUrl: API, fetchImpl }),
    ]);
    expect(a).toHaveLength(3);
    expect(b).toBe(a);
    expect(await loadLegalLinks({ apiUrl: API, fetchImpl })).toBe(a);
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    const [url, init] = fetchImpl.mock.calls[0];
    expect(url).toBe(`${API}/version`);
    expect(init.credentials).toBe('omit');
    expect(init.headers).toBeUndefined(); // a simple request: no preflight
    expect(knownLegalLinks()).toBe(a);
  });

  it('no API URL (a build without one) asks nothing', async () => {
    const fetchImpl = okFetch(ALL);
    expect(await loadLegalLinks({ apiUrl: '', fetchImpl })).toEqual([]);
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it.each([
    ['an unreachable API', vi.fn(async () => { throw new TypeError('Failed to fetch'); })],
    ['an error status', vi.fn(async () => ({ ok: false, status: 503, json: async () => ({}) }))],
    ['a body that is not JSON', vi.fn(async () => ({ ok: true, json: async () => { throw new SyntaxError('x'); } }))],
    ['an older API without the keys', okFetch({ version: '2.37.0', download_url: null })],
  ])('%s → no links, and it is asked again only after a minute', async (_name, fetchImpl) => {
    let t = 1_000_000;
    const now = () => t;
    expect(await loadLegalLinks({ apiUrl: API, fetchImpl, now })).toEqual([]);
    expect(await loadLegalLinks({ apiUrl: API, fetchImpl, now })).toEqual([]);
    const calls = fetchImpl.mock.calls.length;
    t += LEGAL_RETRY_AFTER_MS;
    await loadLegalLinks({ apiUrl: API, fetchImpl, now });
    // An older API answered (no keys): that is an answer, cached like one.
    expect(fetchImpl.mock.calls.length).toBe(_name.startsWith('an older') ? calls : calls + 1);
  });

  it('a request that hangs is given up after the timeout', async () => {
    vi.useFakeTimers();
    const fetchImpl = vi.fn(
      (_url, { signal }) =>
        new Promise((_resolve, reject) => {
          signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
        }),
    );
    const pending = loadLegalLinks({ apiUrl: API, fetchImpl });
    await vi.advanceTimersByTimeAsync(LEGAL_FETCH_TIMEOUT_MS);
    expect(await pending).toEqual([]);
  });
});

describe('<LegalLinks />', () => {
  it('renders nothing at all while no address is set', async () => {
    await loadLegalLinks({ apiUrl: API, fetchImpl: okFetch({ version: '2.40.0' }) });
    const { container } = render(<LegalLinks />);
    const { container: footerContainer } = render(<LegalLinks variant="footer" />);
    expect(container).toBeEmptyDOMElement();
    expect(footerContainer).toBeEmptyDOMElement();
  });

  it('a set link opens in a new tab without opener or referrer', async () => {
    await loadLegalLinks({ apiUrl: API, fetchImpl: okFetch({ legal_impressum_url: ALL.legal_impressum_url }) });
    render(<LegalLinks variant="footer" />);
    const nav = screen.getByRole('navigation', { name: LEGAL_NAV_LABEL });
    const links = within(nav).getAllByRole('link');
    expect(links).toHaveLength(1);
    expect(links[0]).toHaveTextContent('Impressum');
    expect(links[0]).toHaveAttribute('href', ALL.legal_impressum_url);
    expect(links[0]).toHaveAttribute('target', '_blank');
    expect(links[0]).toHaveAttribute('rel', 'noopener noreferrer');
    expect(screen.getByTestId('legal-footer')).toBeInTheDocument();
  });

  it('appears on the login card once the API answers, beside the version', async () => {
    const fetchImpl = okFetch(ALL);
    const view = render(<LoginForm />);
    expect(screen.queryByRole('navigation', { name: LEGAL_NAV_LABEL })).toBeNull();
    // The hook asks with the build's API URL, which tests do not set: prime it.
    await act(async () => {
      await loadLegalLinks({ apiUrl: API, fetchImpl });
    });
    view.unmount();
    render(<LoginForm />);
    const nav = await screen.findByRole('navigation', { name: LEGAL_NAV_LABEL });
    expect(within(nav).getAllByRole('link').map((a) => a.textContent)).toEqual([
      'Impressum',
      'Datenschutz',
      'Nutzungsbedingungen',
    ]);
    expect(screen.getByText(/^v\d+\.\d+\.\d+$/)).toBeInTheDocument();
  });

  it('an unreachable API leaves the login card as it was: no links, no error', async () => {
    const fetchImpl = vi.fn(async () => { throw new TypeError('Failed to fetch'); });
    await act(async () => {
      await loadLegalLinks({ apiUrl: API, fetchImpl });
    });
    render(<LoginForm />);
    expect(await screen.findByRole('button', { name: 'Anmelden' })).toBeInTheDocument();
    expect(screen.queryByRole('navigation', { name: LEGAL_NAV_LABEL })).toBeNull();
    expect(screen.queryByRole('link')).toBeNull();
  });
});
