import { useEffect, useRef } from 'react';

/**
 * Calls `refetch` whenever the browser tab regains focus or becomes visible
 * after being hidden. Debounces so two events within `minIntervalMs` only
 * fire one refetch.
 *
 * Use in any component that fetches server data on mount but does not have
 * realtime — ensures the view refreshes the moment the student comes back
 * to the tab from another window or a long idle.
 */
export default function useRefetchOnFocus(refetch, { minIntervalMs = 2000 } = {}) {
  const refetchRef = useRef(refetch);
  refetchRef.current = refetch;

  useEffect(() => {
    if (typeof refetch !== 'function') return undefined;

    let lastCall = 0;
    const maybeFire = () => {
      const now = Date.now();
      if (now - lastCall < minIntervalMs) return;
      lastCall = now;
      try {
        refetchRef.current?.();
      } catch {
        // The user's refetch swallows errors itself; suppress any rethrown.
      }
    };

    const onFocus = () => maybeFire();
    const onVisibility = () => {
      if (document.visibilityState === 'visible') maybeFire();
    };

    window.addEventListener('focus', onFocus);
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      window.removeEventListener('focus', onFocus);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [refetch, minIntervalMs]);
}
