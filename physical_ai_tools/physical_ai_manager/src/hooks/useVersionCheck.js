import { useEffect } from 'react';

// Baked into the bundle at build time by the Dockerfile (short git sha in CI,
// literal "dev" for local builds). The `process.env.*` reference is inlined by
// Webpack, so this value never changes while the app is running.
const BUILT_ID = process.env.REACT_APP_BUILD_ID;

// sessionStorage guard to prevent a reload loop if something is misconfigured
// on the server (e.g. version.json advertises a new id but the served bundle
// somehow has the old one). Two reloads inside this window are ignored.
const RELOAD_KEY = '__edubotics_version_reload_at';
const MIN_RELOAD_INTERVAL_MS = 60_000;

async function fetchLiveBuildId() {
  const res = await fetch(`/version.json?_=${Date.now()}`, { cache: 'no-store' });
  if (!res.ok) return null;
  try {
    const json = await res.json();
    return typeof json.buildId === 'string' ? json.buildId : null;
  } catch {
    // Dev server or SPA fallthrough returned index.html — not a real deploy.
    return null;
  }
}

function reloadIfStale(liveId) {
  if (!liveId || !BUILT_ID) return;
  if (BUILT_ID === 'dev' || liveId === 'dev') return;
  if (liveId === BUILT_ID) return;

  const lastReloadAt = Number(sessionStorage.getItem(RELOAD_KEY) || 0);
  if (Date.now() - lastReloadAt < MIN_RELOAD_INTERVAL_MS) return;
  sessionStorage.setItem(RELOAD_KEY, String(Date.now()));

  window.location.reload();
}

/**
 * Detect "I'm running an old bundle" without asking the user to hard-refresh.
 *
 * On mount, every `intervalMs`, and whenever the tab regains focus, fetch
 * `/version.json` (served with `no-store`). If the `buildId` it returns
 * differs from the one baked into this bundle, reload the page so the new
 * hashed JS/CSS bundles load. Skipped entirely for dev builds where no build
 * id was baked in.
 */
export default function useVersionCheck({ intervalMs = 30_000 } = {}) {
  useEffect(() => {
    if (!BUILT_ID || BUILT_ID === 'dev') return undefined;

    let cancelled = false;

    const check = async () => {
      try {
        const liveId = await fetchLiveBuildId();
        if (cancelled) return;
        reloadIfStale(liveId);
      } catch {
        // Container restart in progress, network hiccup, etc. Retry next tick.
      }
    };

    check();
    const timer = setInterval(check, intervalMs);

    const onFocus = () => { check(); };
    const onVisibility = () => {
      if (document.visibilityState === 'visible') check();
    };
    window.addEventListener('focus', onFocus);
    document.addEventListener('visibilitychange', onVisibility);

    return () => {
      cancelled = true;
      clearInterval(timer);
      window.removeEventListener('focus', onFocus);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [intervalMs]);
}
