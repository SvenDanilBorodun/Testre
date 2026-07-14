// Copyright 2025 EduBotics
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

import React, { useState, useEffect, useRef, useCallback } from 'react';
import clsx from 'clsx';
import { useSelector, useDispatch } from 'react-redux';
import rosConnectionManager from '../utils/rosConnectionManager';
import { isCloudOnlyMode } from '../utils/cloudMode';
import { usePiMode, PI_PORT_BLOCKED_HINT } from '../utils/piMode';
import PageType from '../constants/pageType';
import { moveToPage } from '../features/ui/uiSlice';

const STARTUP_TIMEOUT_MS = 90000;
const SETTLE_DELAY_MS = 3000;
const POLL_INTERVAL_MS = 500;

function Spinner() {
  return (
    <svg
      className="animate-spin h-5 w-5 text-teal-500"
      xmlns="http://www.w3.org/2000/svg"
      fill="none"
      viewBox="0 0 24 24"
    >
      <circle
        className="opacity-25"
        cx="12"
        cy="12"
        r="10"
        stroke="currentColor"
        strokeWidth="4"
      />
      <path
        className="opacity-75"
        fill="currentColor"
        d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
      />
    </svg>
  );
}

function Checkmark() {
  return (
    <svg
      className="h-5 w-5 text-green-500"
      xmlns="http://www.w3.org/2000/svg"
      fill="none"
      viewBox="0 0 24 24"
      stroke="currentColor"
      strokeWidth="2.5"
    >
      <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
    </svg>
  );
}

function ProgressStep({ label, done }) {
  return (
    <div className="flex items-center gap-3">
      {done ? <Checkmark /> : <Spinner />}
      <span
        className={clsx(
          'text-sm transition-colors duration-300',
          done ? 'text-green-700' : 'text-gray-800 font-medium'
        )}
      >
        {label}
      </span>
    </div>
  );
}

export default function StartupGate({ children }) {
  // If we're in cloud-only mode, render children directly without any gate.
  // Early-return before calling any hooks — cloud mode doesn't change
  // between renders so the hook-order rule is satisfied.
  if (isCloudOnlyMode()) {
    return <>{children}</>;
  }
  return <StartupGateRouter>{children}</StartupGateRouter>;
}

// Routes to the Windows gate or the Pi carve-out once the static `/pi-mode.json`
// marker has resolved. Deploy plan §6: the Pi-mode flag MUST resolve before the
// gate decides whether to block — so while it is still resolving we show a
// neutral boot splash (a few ms; the marker is a static file) that never times
// out and never mentions Docker.
function StartupGateRouter({ children }) {
  const { piMode, piModeResolved } = usePiMode();
  if (!piModeResolved) {
    return <BootSplash />;
  }
  if (piMode) {
    return <PiStartupGate>{children}</PiStartupGate>;
  }
  return <StartupGateImpl>{children}</StartupGateImpl>;
}

// Neutral full-screen splash shown only for the brief window while the Pi-mode
// marker resolves. Visually matches the gate overlays so the hand-off is
// seamless on a non-Pi rig (splash -> StartupGateImpl).
function BootSplash() {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-white">
      <div className="flex flex-col items-center gap-6">
        <div className="flex flex-col items-center gap-2">
          <h1 className="text-3xl font-bold text-gray-800">EduBotics</h1>
          <p className="text-sm text-gray-500">System wird gestartet...</p>
        </div>
        <Spinner />
      </div>
    </div>
  );
}

// ── Pi-mode carve-out (deploy plan §5/§6) ────────────────────────────────────
//
// On a freshly booted Pi the manager is up but the robot tier is DOWN BY
// DESIGN, and the student must reach the System tab to press „Umgebung starten".
// A global block would trap them (the §5 chicken-and-egg, one layer up). So the
// Pi gate blocks ONLY while the robot tier is genuinely coming up and rosbridge
// hasn't connected yet — and it NEVER covers the System tab (where the start
// button lives). When the robot tier is down, or the student is on the System
// tab, children render fully interactive. If the wait times out, the screen
// names the port-9090/network cause (never „Docker prüfen") and offers an escape
// back to the System window.
function PiStartupGate({ children }) {
  const { agentStatus } = usePiMode();
  const dispatch = useDispatch();
  const currentPage = useSelector((s) => s.ui.currentPage);
  const robotTierUp = !!(agentStatus && agentStatus.robot_tier_up);

  const [connected, setConnected] = useState(() => rosConnectionManager.isConnected());
  const [settled, setSettled] = useState(false);
  const [timedOut, setTimedOut] = useState(false);
  const settleRef = useRef(null);
  const pollRef = useRef(null);
  const timeoutRef = useRef(null);

  const suppressed = currentPage === PageType.SYSTEM || !robotTierUp;
  const revealed = connected && settled;
  const overlayVisible = !suppressed && !revealed;

  // Poll rosbridge liveness only while an overlay could show.
  useEffect(() => {
    if (!overlayVisible) return undefined;
    pollRef.current = setInterval(() => {
      if (rosConnectionManager.isConnected()) setConnected(true);
    }, POLL_INTERVAL_MS);
    return () => clearInterval(pollRef.current);
  }, [overlayVisible]);

  // Settle once connected so subscriptions establish before revealing.
  useEffect(() => {
    if (connected && !settled) {
      settleRef.current = setTimeout(() => setSettled(true), SETTLE_DELAY_MS);
    }
    return () => clearTimeout(settleRef.current);
  }, [connected, settled]);

  // 90 s of a visible overlay without a connection → offer the network
  // diagnosis + escape. Reset the flag whenever the overlay isn't showing.
  useEffect(() => {
    if (!overlayVisible) {
      setTimedOut(false);
      return undefined;
    }
    timeoutRef.current = setTimeout(() => setTimedOut(true), STARTUP_TIMEOUT_MS);
    return () => clearTimeout(timeoutRef.current);
  }, [overlayVisible]);

  const handleRetry = useCallback(() => {
    setTimedOut(false);
    rosConnectionManager.resetReconnectCounter();
    window.location.reload();
  }, []);

  const goToSystem = useCallback(() => {
    dispatch(moveToPage(PageType.SYSTEM));
  }, [dispatch]);

  return (
    <>
      {children}
      {overlayVisible && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-white">
          <div className="flex flex-col items-center gap-8">
            <div className="flex flex-col items-center gap-2">
              <h1 className="text-3xl font-bold text-gray-800">EduBotics</h1>
              <p className="text-sm text-gray-500">Roboter wird verbunden...</p>
            </div>

            <div className="flex flex-col gap-4 bg-gray-50 rounded-2xl px-8 py-6 shadow-lg border border-gray-100 min-w-80">
              <ProgressStep label="Verbindung zum Roboter (Port 9090)..." done={connected} />
              <ProgressStep label="Dienste werden initialisiert..." done={settled} />
            </div>

            {timedOut && !settled && (
              <div className="flex flex-col items-center gap-3 max-w-sm text-center">
                {/* Pi-specific: the robot tier is REPORTED running but rosbridge
                    (:9090) is unreachable from the browser — a port-filter / VLAN
                    ACL, not a crashed stack. Never the Windows „Docker prüfen". */}
                <p className="text-sm text-orange-600">{PI_PORT_BLOCKED_HINT}</p>
                <div className="flex items-center gap-2">
                  <button
                    onClick={goToSystem}
                    className="px-4 py-2 bg-teal-500 text-white rounded-md text-sm font-medium hover:bg-teal-600 transition-colors"
                  >
                    Zum System-Fenster
                  </button>
                  <button
                    onClick={handleRetry}
                    className="px-4 py-2 bg-white text-teal-600 border border-teal-500 rounded-md text-sm font-medium hover:bg-teal-50 transition-colors"
                  >
                    Erneut versuchen
                  </button>
                </div>
              </div>
            )}

            {!timedOut && !settled && (
              <div className="flex gap-1.5">
                <div className="w-2 h-2 bg-teal-400 rounded-full animate-bounce" style={{ animationDelay: '0ms' }} />
                <div className="w-2 h-2 bg-teal-400 rounded-full animate-bounce" style={{ animationDelay: '150ms' }} />
                <div className="w-2 h-2 bg-teal-400 rounded-full animate-bounce" style={{ animationDelay: '300ms' }} />
              </div>
            )}
          </div>
        </div>
      )}
    </>
  );
}

function StartupGateImpl({ children }) {
  const [rosbridgeConnected, setRosbridgeConnected] = useState(false);
  const [settled, setSettled] = useState(false);
  const [dismissed, setDismissed] = useState(false);
  const [fading, setFading] = useState(false);
  const [timedOut, setTimedOut] = useState(false);
  const timeoutRef = useRef(null);
  const settleTimerRef = useRef(null);
  const pollRef = useRef(null);

  // Poll rosConnectionManager.isConnected() to detect when rosbridge is up
  const checkConnection = useCallback(() => {
    if (rosConnectionManager.isConnected()) {
      setRosbridgeConnected(true);
    }
  }, []);

  useEffect(() => {
    if (dismissed || rosbridgeConnected) return;
    pollRef.current = setInterval(checkConnection, POLL_INTERVAL_MS);
    return () => clearInterval(pollRef.current);
  }, [dismissed, rosbridgeConnected, checkConnection]);

  // Once connected, wait for subscriptions to establish before revealing
  useEffect(() => {
    if (rosbridgeConnected && !settled) {
      settleTimerRef.current = setTimeout(() => {
        setSettled(true);
      }, SETTLE_DELAY_MS);
    }
    return () => clearTimeout(settleTimerRef.current);
  }, [rosbridgeConnected, settled]);

  // Trigger fade-out when settled
  useEffect(() => {
    if (settled && !dismissed && !fading) {
      setFading(true);
    }
  }, [settled, dismissed, fading]);

  // Timeout after 90s
  useEffect(() => {
    if (dismissed) return;
    timeoutRef.current = setTimeout(() => {
      setTimedOut(true);
    }, STARTUP_TIMEOUT_MS);
    return () => clearTimeout(timeoutRef.current);
  }, [dismissed]);

  const handleTransitionEnd = () => {
    if (fading) {
      setDismissed(true);
    }
  };

  const handleRetry = () => {
    setTimedOut(false);
    rosConnectionManager.resetReconnectCounter();
    window.location.reload();
  };

  if (dismissed) {
    return <>{children}</>;
  }

  return (
    <>
      {children}
      <div
        className={clsx(
          'fixed inset-0 z-50 flex items-center justify-center bg-white',
          'transition-opacity duration-700',
          fading && 'opacity-0 pointer-events-none'
        )}
        onTransitionEnd={handleTransitionEnd}
      >
        <div className="flex flex-col items-center gap-8">
          {/* Header */}
          <div className="flex flex-col items-center gap-2">
            <h1 className="text-3xl font-bold text-gray-800">EduBotics</h1>
            <p className="text-sm text-gray-500">System wird gestartet...</p>
          </div>

          {/* Progress steps */}
          <div className="flex flex-col gap-4 bg-gray-50 rounded-2xl px-8 py-6 shadow-lg border border-gray-100 min-w-80">
            <ProgressStep label="Verbindung zum ROS-System..." done={rosbridgeConnected} />
            <ProgressStep label="Dienste werden initialisiert..." done={settled} />
          </div>

          {/* Timeout hint */}
          {timedOut && !settled && (
            <div className="flex flex-col items-center gap-3 max-w-sm text-center">
              <p className="text-sm text-orange-600">
                Das System braucht ungewöhnlich lange. Bitte prüfe ob Docker Desktop läuft und
                alle Container gestartet sind.
              </p>
              <button
                onClick={handleRetry}
                className="px-4 py-2 bg-teal-500 text-white rounded-md text-sm font-medium hover:bg-teal-600 transition-colors"
              >
                Erneut versuchen
              </button>
            </div>
          )}

          {/* Subtle pulse animation */}
          {!timedOut && !settled && (
            <div className="flex gap-1.5">
              <div className="w-2 h-2 bg-teal-400 rounded-full animate-bounce" style={{ animationDelay: '0ms' }} />
              <div className="w-2 h-2 bg-teal-400 rounded-full animate-bounce" style={{ animationDelay: '150ms' }} />
              <div className="w-2 h-2 bg-teal-400 rounded-full animate-bounce" style={{ animationDelay: '300ms' }} />
            </div>
          )}
        </div>
      </div>
    </>
  );
}
