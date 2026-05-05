// Cloud-only mode detection. Set when the GUI launches the WebApp with
// `?cloud=1` (no robot hardware available). Components that need to gate
// hardware-dependent behavior import this single source of truth instead of
// duplicating the URL parse.
export function isCloudOnlyMode() {
  if (typeof window === 'undefined') return false;
  try {
    const params = new URLSearchParams(window.location.search);
    return params.get('cloud') === '1';
  } catch {
    return false;
  }
}
