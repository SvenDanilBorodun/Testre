import { createClient } from '@supabase/supabase-js';

const supabaseUrl = process.env.REACT_APP_SUPABASE_URL;
const supabaseAnonKey = process.env.REACT_APP_SUPABASE_ANON_KEY;

// Whether the Docker build had Supabase build args available. Imported by
// App.js to render a visible degraded-mode banner instead of the silent
// white screen we shipped on every published image before this guard.
export const isSupabaseConfigured = Boolean(supabaseUrl) && Boolean(supabaseAnonKey);

if (!isSupabaseConfigured) {
  // Don't `throw` here — that fires synchronously during module evaluation,
  // BEFORE React mounts, so no <ErrorBoundary> can catch it and the user
  // gets a hard white screen with the cause buried in DevTools. Instead,
  // log loudly and export a stub that surfaces the same error only when a
  // consumer actually calls into Supabase, by which point React has rendered
  // the banner and the student can read what went wrong.
  // eslint-disable-next-line no-console
  console.error(
    '[edubotics] Supabase env vars missing at build time — cloud features ' +
      'are disabled. Rebuild the physical_ai_manager image with ' +
      'REACT_APP_SUPABASE_URL and REACT_APP_SUPABASE_ANON_KEY set.'
  );
}

function buildStub() {
  const fail = () => {
    throw new Error(
      'Supabase ist in dieser Build-Version nicht konfiguriert. ' +
        'Bitte das physical-ai-manager-Image mit gültigen ' +
        'REACT_APP_SUPABASE_URL und REACT_APP_SUPABASE_ANON_KEY neu bauen.'
    );
  };
  // Mirrors the surface area we actually call (auth.* + table builders).
  // Any uncovered method falls through to the Proxy default which also
  // throws — so a missed surface fails loudly at the call site, not
  // silently as `undefined is not a function`.
  const authStub = new Proxy(
    {},
    {
      get: () => fail,
    }
  );
  return new Proxy(
    { auth: authStub },
    {
      get: (target, prop) => {
        if (prop === 'auth') return target.auth;
        return fail;
      },
    }
  );
}

export const supabase = isSupabaseConfigured
  ? createClient(supabaseUrl, supabaseAnonKey)
  : buildStub();
