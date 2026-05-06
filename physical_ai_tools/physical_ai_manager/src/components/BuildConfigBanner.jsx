// Copyright 2025 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License").
//
// Renders a high-contrast banner at the top of the app whenever the Docker
// build was missing one of the React build args (REACT_APP_SUPABASE_URL,
// REACT_APP_SUPABASE_ANON_KEY, REACT_APP_CLOUD_API_URL). Without this,
// supabaseClient.js used to throw at module load and the student saw a
// hard white screen with the cause buried in DevTools — see audit.

import React from 'react';
import { isSupabaseConfigured } from '../lib/supabaseClient';
import { isCloudApiConfigured } from '../services/cloudConfig';

export default function BuildConfigBanner() {
  // Render nothing in the happy path so production builds have zero UI cost.
  if (isSupabaseConfigured && isCloudApiConfigured) return null;

  const missing = [];
  if (!isSupabaseConfigured) missing.push('REACT_APP_SUPABASE_URL/ANON_KEY');
  if (!isCloudApiConfigured) missing.push('REACT_APP_CLOUD_API_URL');

  return (
    <div
      role="alert"
      style={{
        position: 'fixed',
        top: 0,
        left: 0,
        right: 0,
        zIndex: 9999,
        background: '#b91c1c',
        color: 'white',
        padding: '12px 16px',
        textAlign: 'center',
        fontFamily: 'system-ui, sans-serif',
        fontSize: 14,
        lineHeight: 1.4,
        boxShadow: '0 2px 8px rgba(0,0,0,0.25)',
      }}
    >
      <strong>Build ist falsch konfiguriert.</strong>{' '}
      Cloud-Funktionen sind deaktiviert (fehlende Variablen: {missing.join(', ')}).
      Bitte das physical-ai-manager-Image mit den korrekten Build-Argumenten neu bauen.
    </div>
  );
}
