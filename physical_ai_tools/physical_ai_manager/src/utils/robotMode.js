// Robot-profile identity from the URL. The GUI appends `?robot=<profile_id>`
// (e.g. `omx_full` / `omx_follower`) to the WebView URL, mirroring `?cloud=1`,
// so the app can show a robot badge INSTANTLY on load — before the first
// /task/status tick arrives. Identity/badge ONLY: the authoritative
// capabilities come from the server via TaskStatus.capabilities_json, never
// from this URL param (a client-side profile→caps map would duplicate the
// server registry). Components that want the seed import this single source of
// truth instead of duplicating the URL parse.
export function robotProfileId() {
  if (typeof window === 'undefined') return '';
  try {
    const params = new URLSearchParams(window.location.search);
    return params.get('robot') || '';
  } catch {
    return '';
  }
}
