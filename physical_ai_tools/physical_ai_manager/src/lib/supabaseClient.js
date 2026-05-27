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

// --- Keep the Realtime websocket's JWT in sync with the auth session ---
//
// Every `postgres_changes` subscription in this app (trainings, workflows,
// datasets, tutorial_progress, jetsons) is RLS-filtered — e.g. the trainings
// SELECT policy is `auth.uid() = user_id`. Realtime enforces those policies
// using whatever JWT the websocket last authenticated with. If the socket is
// still on the anon apikey, `auth.uid()` is NULL, RLS drops EVERY event, yet
// the channel still reports SUBSCRIBED — so the hooks' poll fallback never
// engages and the UI is dead until a manual REST refetch ("click reload").
//
// supabase-js only auto-pushes the token to Realtime on the SIGNED_IN and
// TOKEN_REFRESHED auth events. It does NOT do so on INITIAL_SESSION — the
// event fired when a persisted session is restored from localStorage on every
// page load/reload. Students stay logged in across reloads, so without this
// the Realtime socket boots as anon on every visit and live updates are dead.
//
// We therefore drive `realtime.setAuth()` explicitly from the session
// lifecycle: once at startup for the restored session, and on every auth state
// change (login, refresh ~hourly during long trainings, logout → null clears).
// This fixes all RLS-filtered Realtime subscriptions at the root, not just the
// training tab.
if (isSupabaseConfigured) {
  const syncRealtimeAuth = (token) => {
    try {
      // setAuth is async in realtime-js v2; fire-and-forget, swallow rejections
      // (e.g. socket not yet connected) so we never produce an unhandled reject.
      Promise.resolve(supabase.realtime.setAuth(token ?? null)).catch(() => {});
    } catch {
      /* stub / unavailable realtime client — nothing to sync */
    }
  };

  supabase.auth
    .getSession()
    .then(({ data: { session } }) => syncRealtimeAuth(session?.access_token))
    .catch(() => {});

  supabase.auth.onAuthStateChange((_event, session) => {
    syncRealtimeAuth(session?.access_token);
  });
}
