// Copyright 2025 ROBOTIS CO., LTD.
// Modifications copyright 2026 Sven Danil Borodun (EduBotics)
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.
//
// Author: Kiwoong Park

import React, { useEffect } from 'react';
import { useSelector } from 'react-redux';
import toast, { useToasterStore } from 'react-hot-toast';
import LoginForm from '../components/LoginForm';
import Icon from '../components/icons/Icon';
import { Plate } from '../components/sucher';
import '../components/sucher-pages/inference.css';
import CloudInferenceView from '../components/CloudInference/CloudInferenceView';
import { cloudTargetState } from '../features/cloudInference/gating';
import { isCloudApiConfigured } from '../services/cloudConfig';
import { isCloudOnlyMode } from '../utils/cloudMode';

const TOAST_LIMIT = 3;

// The Inferenz page runs the student's model „In der Cloud" (Cloud-Inferenz):
// the model on a cloud GPU, the arm and the cameras at this robot. It is the
// page's only target: the shipped robot images carry CPU torch, so there is no
// local GPU target.
export default function InferencePage({ isActive = true }) {
  // Hooks must always run in the same order, so every selector + every
  // hook below runs regardless of which screen we end up rendering. The
  // gating happens entirely in the JSX return below.
  const { toasts } = useToasterStore();
  const capabilities = useSelector((state) => state.tasks.taskStatus.capabilities);

  const isAuthenticated = useSelector((s) => s.auth.isAuthenticated);
  const isAuthLoading = useSelector((s) => s.auth.isLoading);
  const profileLoaded = useSelector((s) => s.auth.profileLoaded);
  const username = useSelector((s) => s.auth.username);
  const fullName = useSelector((s) => s.auth.fullName);

  // Toast cap — applies to every screen of this page.
  useEffect(() => {
    toasts
      .filter((t) => t.visible)
      .filter((_, i) => i >= TOAST_LIMIT)
      .forEach((t) => toast.dismiss(t.id));
  }, [toasts]);

  // Whether „In der Cloud" is offered and may be started is derived on every
  // render, never stored (features/cloudInference/gating.js); a disabled cloud
  // target says why in the panel.
  const cloud = cloudTargetState({
    cloudOnly: isCloudOnlyMode(),
    capabilities,
    cloudApiConfigured: isCloudApiConfigured,
  });

  // 1) Auth bootstrap — Supabase session lookup still pending.
  if (isAuthLoading) {
    return <CenteredSpinner label="Laden…" />;
  }

  // 2) Not signed in — show the LoginForm (mirrors the Training tab).
  //    StudentApp's <main> asks for a login first (utils/authGate), so this is
  //    the second line of defence for the one path that gets past it,
  //    „Ohne Anmeldung fortfahren". KEEP IT: that path is exactly what it
  //    guards, and the teacher web (WebApp) has no StudentApp gate at all.
  if (!isAuthenticated) {
    return <LoginForm subtitle="Anmelden für die Inferenz" />;
  }

  // 3) Session is live but /me hasn't responded yet — show a slim spinner.
  if (!profileLoaded) {
    return <CenteredSpinner label="Profil wird geladen…" />;
  }

  // 4) No robot to drive (`?cloud=1`): StudentApp hides the tab there
  //    (hardwareOnly); this screen is the fallback if the page is shown anyway.
  if (!cloud.offered) {
    return (
      <StateScreen
        icon={<Icon name="cloud" size={44} className="text-gray-400" />}
        title="Inferenz braucht deinen Roboter"
        body="Dein Modell läuft in der Cloud, bewegt dabei aber deinen Roboter. Im Cloud-Modus ohne Roboter ist die Inferenz deshalb nicht verfügbar."
        identity={{ username, fullName }}
      />
    );
  }

  return <CloudInferenceView isActive={isActive} cloud={cloud} />;
}

// ─── Sub-components for the state screens ─────────────────────────────

function CenteredSpinner({ label }) {
  return (
    <div className="flex flex-col items-center justify-center h-full w-full gap-4 px-6 text-center" style={{ background: 'var(--bg)' }}>
      <Icon name="loading" size={32} className="animate-spin text-[color:var(--accent)]" />
      <div className="su-serif text-[30px] leading-none text-[var(--ink)]">{label}</div>
    </div>
  );
}

function StateScreen({ icon, title, body, identity }) {
  return (
    <div className="h-full w-full overflow-y-auto" style={{ background: 'var(--bg)' }}>
      <div className="min-h-full w-full flex items-center justify-center px-4 py-8">
        <Plate className="su-state w-full max-w-[560px] flex flex-col items-center gap-5 text-center">
          {icon && <div className="su-state__disc">{icon}</div>}
          <h1 className="su-serif text-[34px] leading-none text-[var(--ink)]">{title}</h1>
          <div className="text-[var(--ink-2)] text-[15px] leading-relaxed">{body}</div>
          {identity && (
            <div className="mt-6 pt-5 border-t border-[var(--su-hair)] w-full text-center text-sm">
              <div className="text-[var(--ink-2)]">
                Angemeldet als{' '}
                <span className="text-[var(--ink)] font-medium">
                  {identity.fullName || identity.username || '—'}
                </span>
              </div>
            </div>
          )}
        </Plate>
      </div>
    </div>
  );
}
