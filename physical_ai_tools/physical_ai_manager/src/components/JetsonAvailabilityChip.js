// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

import React from 'react';
import { useSelector } from 'react-redux';

import { useJetsonConnection } from '../hooks/useJetsonConnection';

/**
 * Status chip + connect/disconnect buttons for the classroom Jetson.
 * Mounted at the top of the Inference tab. When no Jetson is paired
 * with the classroom, renders a small grey info chip; when one exists,
 * shows the appropriate state + action button.
 */
function JetsonAvailabilityChip() {
  const status = useSelector((s) => s.jetson.status);
  const ownerUsername = useSelector((s) => s.jetson.ownerUsername);
  const ownerFullName = useSelector((s) => s.jetson.ownerFullName);
  const online = useSelector((s) => s.jetson.online);
  const error = useSelector((s) => s.jetson.error);
  const { connect, disconnect } = useJetsonConnection();

  // status-driven rendering. Keep the styling minimal — the consuming
  // page can wrap this however it likes.
  switch (status) {
    case 'unknown':
      return null;  // Loading — render nothing rather than flashing.

    case 'no_jetson':
      return (
        <div className="flex items-center gap-2 px-3 py-1.5 rounded-md bg-gray-100 text-gray-600 text-sm">
          <span className="w-2 h-2 rounded-full bg-gray-400" />
          <span>Kein Klassen-Jetson in diesem Raum</span>
        </div>
      );

    case 'available':
      return (
        <div className="flex items-center gap-3 px-3 py-1.5 rounded-md bg-green-50 text-green-700 text-sm">
          <span className={`w-2 h-2 rounded-full ${online ? 'bg-green-500' : 'bg-gray-400'}`} />
          <span>{online ? 'Jetson frei' : 'Jetson offline'}</span>
          <button
            type="button"
            onClick={connect}
            disabled={!online}
            className="ml-2 px-3 py-1 rounded bg-green-600 text-white text-xs hover:bg-green-700 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            Verbinde mit Klassen-Jetson
          </button>
        </div>
      );

    case 'busy': {
      const who = ownerFullName || ownerUsername || 'einem anderen Schüler';
      return (
        <div className="flex items-center gap-2 px-3 py-1.5 rounded-md bg-yellow-50 text-yellow-800 text-sm">
          <span className="w-2 h-2 rounded-full bg-yellow-500" />
          <span>Jetson belegt von {who}</span>
        </div>
      );
    }

    case 'claiming':
      return (
        <div className="flex items-center gap-2 px-3 py-1.5 rounded-md bg-blue-50 text-blue-700 text-sm">
          <span className="w-2 h-2 rounded-full bg-blue-500 animate-pulse" />
          <span>Verbinde...</span>
        </div>
      );

    case 'connected':
      return (
        <div className="flex items-center gap-3 px-3 py-1.5 rounded-md bg-green-100 text-green-800 text-sm">
          <span className="w-2 h-2 rounded-full bg-green-500" />
          <span>Verbunden mit Klassen-Jetson</span>
          <button
            type="button"
            onClick={disconnect}
            className="ml-2 px-3 py-1 rounded bg-red-600 text-white text-xs hover:bg-red-700"
          >
            Trennen
          </button>
        </div>
      );

    case 'disconnecting':
      return (
        <div className="flex items-center gap-2 px-3 py-1.5 rounded-md bg-gray-100 text-gray-700 text-sm">
          <span className="w-2 h-2 rounded-full bg-gray-500 animate-pulse" />
          <span>Jetson wird vorbereitet...</span>
        </div>
      );

    case 'error':
      return (
        <div className="flex items-center gap-2 px-3 py-1.5 rounded-md bg-red-50 text-red-700 text-sm">
          <span className="w-2 h-2 rounded-full bg-red-500" />
          <span>{error || 'Unbekannter Jetson-Fehler'}</span>
        </div>
      );

    default:
      return null;
  }
}

export default JetsonAvailabilityChip;
