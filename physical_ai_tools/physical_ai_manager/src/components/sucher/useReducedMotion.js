// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.
//
// `prefers-reduced-motion: reduce`, live. JS-driven motion (the Lichtband's
// afterglow, rolling digits, the chronophotography trail, recharts) reads this;
// CSS loops are stopped by the global rule in sucher.css. Without matchMedia
// (jsdom, very old browsers) the answer is `false`.

import { useEffect, useState } from 'react';

export const REDUCED_MOTION_QUERY = '(prefers-reduced-motion: reduce)';

function mediaQuery() {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return null;
  try {
    return window.matchMedia(REDUCED_MOTION_QUERY);
  } catch {
    return null;
  }
}

/** The current answer, read once (no subscription). */
export function readReducedMotion() {
  const mq = mediaQuery();
  return !!(mq && mq.matches);
}

/**
 * `subscribe = false` skips the media query entirely (no read, no listener)
 * and answers `false`: for a caller that already holds the answer (RobotIcon's
 * `reducedMotion` prop). The hook is still called unconditionally.
 */
export default function useReducedMotion(subscribe = true) {
  const [reduced, setReduced] = useState(() => (subscribe ? readReducedMotion() : false));
  useEffect(() => {
    if (!subscribe) return undefined;
    const mq = mediaQuery();
    if (!mq) return undefined;
    const onChange = () => setReduced(!!mq.matches);
    onChange();
    if (typeof mq.addEventListener === 'function') {
      mq.addEventListener('change', onChange);
      return () => mq.removeEventListener('change', onChange);
    }
    if (typeof mq.addListener === 'function') {
      mq.addListener(onChange);
      return () => mq.removeListener(onChange);
    }
    return undefined;
  }, [subscribe]);
  return subscribe ? reduced : false;
}
