// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
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

export default function useReducedMotion() {
  const [reduced, setReduced] = useState(readReducedMotion);
  useEffect(() => {
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
  }, []);
  return reduced;
}
