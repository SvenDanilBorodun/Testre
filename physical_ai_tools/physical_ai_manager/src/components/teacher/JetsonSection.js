// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

import React, { useCallback, useEffect, useState } from 'react';
import toast from 'react-hot-toast';
import {
  MdAdd,
  MdLink,
  MdLinkOff,
  MdLockOpen,
  MdRefresh,
} from 'react-icons/md';
import { useDispatch, useSelector } from 'react-redux';
import { Btn, Card, Pill } from '../EbUI';
import {
  forceReleaseJetson,
  getClassroomJetson,
  pairJetson,
  regeneratePairingCode,
  unpairJetson,
} from '../../services/jetsonClient';
import {
  clearJetsonForClassroom,
  setJetsonError,
  setJetsonInfo,
  setJetsonLastPairingCode,
  setJetsonLoading,
} from '../../features/teacher/teacherSlice';
import useTeacherJetsonRealtime from '../../hooks/useTeacherJetsonRealtime';
import PairJetsonModal from './PairJetsonModal';

const HEARTBEAT_OFFLINE_MS = 60 * 1000; // matches Cloud API _is_online threshold

function formatAge(isoString) {
  if (!isoString) return '—';
  const then = new Date(isoString).getTime();
  if (Number.isNaN(then)) return '—';
  const seconds = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (seconds < 5) return 'gerade eben';
  if (seconds < 60) return `vor ${seconds} s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `vor ${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `vor ${hours} h`;
  const days = Math.floor(hours / 24);
  return `vor ${days} Tagen`;
}

function StatusPill({ jetson }) {
  if (!jetson) return null;
  if (!jetson.online) {
    return <Pill tone="amber" dot>Offline</Pill>;
  }
  if (jetson.current_owner_user_id) {
    return <Pill tone="accent" dot>Belegt</Pill>;
  }
  return <Pill tone="success" dot>Bereit</Pill>;
}

/**
 * Renders the classroom's Jetson card inside ClassroomDetail. Four
 * states:
 *   1. loading           → spinner placeholder
 *   2. no Jetson paired  → "Kein Jetson gepaart" + Hinzufügen button
 *   3. paired             → mdns_name + lan_ip + online/owner + action
 *                          buttons (Pairing-Code erneuern,
 *                          Lock freigeben, Vom Klassenzimmer trennen)
 *   4. error              → German error chip + Retry button
 *
 * Subscribes to Supabase realtime on the jetsons row so the teacher
 * sees ownership transitions live (no manual refresh during a lesson).
 */
export default function JetsonSection({ classroomId }) {
  const dispatch = useDispatch();
  const token = useSelector((s) => s.auth.session?.access_token);
  const entry = useSelector(
    (s) => s.teacher.jetsonByClassroom[classroomId] || null
  );
  const jetson = entry?.info || null;
  const loading = entry?.loading ?? !entry;
  const error = entry?.error || null;
  const lastPairingCode = entry?.lastPairingCode || null;

  const [showPairModal, setShowPairModal] = useState(false);
  const [working, setWorking] = useState(false);

  // Refetch the Jetson info from the Cloud API. Dispatches loading/
  // info/error reducers accordingly. Called on mount, on realtime
  // updates, and after every mutation (pair / unpair / regenerate /
  // force-release) as a belt-and-suspenders refresh.
  const refresh = useCallback(async () => {
    if (!token || !classroomId) return;
    dispatch(setJetsonLoading({ classroomId, loading: true }));
    try {
      const info = await getClassroomJetson(token, classroomId);
      dispatch(setJetsonInfo({ classroomId, info }));
    } catch (err) {
      if (err?.status === 404) {
        // No Jetson paired — that's a valid state, not an error.
        dispatch(setJetsonInfo({ classroomId, info: null }));
      } else {
        dispatch(
          setJetsonError({ classroomId, error: err?.message || 'Fehler beim Laden' })
        );
      }
    }
  }, [classroomId, token, dispatch]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // Realtime: when the Cloud API or another tab mutates the row,
  // refetch so this card stays current without polling.
  useTeacherJetsonRealtime(classroomId, refresh);

  const handlePair = useCallback(
    async (pairingCode) => {
      if (!token) throw new Error('Nicht angemeldet');
      setWorking(true);
      try {
        const result = await pairJetson(token, classroomId, pairingCode);
        await refresh();
        return result;
      } finally {
        setWorking(false);
      }
    },
    [token, classroomId, refresh]
  );

  const handleRegenerate = async () => {
    if (!token || !jetson) return;
    if (
      !window.confirm(
        'Pairing-Code wirklich erneuern? Der bisherige Code wird ungültig.'
      )
    ) {
      return;
    }
    setWorking(true);
    try {
      const result = await regeneratePairingCode(token, classroomId);
      dispatch(
        setJetsonLastPairingCode({
          classroomId,
          code: result.pairing_code,
          expiresAt: result.pairing_code_expires_at,
        })
      );
      toast.success(`Neuer Pairing-Code: ${result.pairing_code}`);
    } catch (err) {
      toast.error(err?.message || 'Fehler beim Erneuern');
    } finally {
      setWorking(false);
    }
  };

  const handleForceRelease = async () => {
    if (!token || !jetson) return;
    if (
      !window.confirm(
        'Lock wirklich freigeben? Der aktuelle Schüler wird sofort getrennt.'
      )
    ) {
      return;
    }
    setWorking(true);
    try {
      await forceReleaseJetson(token, classroomId);
      toast.success('Lock freigegeben');
      await refresh();
    } catch (err) {
      toast.error(err?.message || 'Fehler beim Freigeben');
    } finally {
      setWorking(false);
    }
  };

  const handleUnpair = async () => {
    if (!token || !jetson) return;
    if (
      !window.confirm(
        'Jetson wirklich vom Klassenzimmer trennen? Der aktuelle Schüler wird getrennt und das Gerät verliert die Klassenbindung.'
      )
    ) {
      return;
    }
    setWorking(true);
    try {
      await unpairJetson(token, classroomId);
      dispatch(clearJetsonForClassroom(classroomId));
      toast.success('Jetson vom Klassenzimmer getrennt');
    } catch (err) {
      toast.error(err?.message || 'Fehler beim Trennen');
    } finally {
      setWorking(false);
    }
  };

  // Render branches
  if (loading) {
    return (
      <Card title="Klassen-Jetson" className="mb-4" padded>
        <div className="text-sm text-[var(--ink-3)]">Lade Jetson-Status…</div>
      </Card>
    );
  }

  if (error) {
    return (
      <Card
        title="Klassen-Jetson"
        className="mb-4"
        padded
        right={
          <Btn variant="secondary" size="sm" onClick={refresh}>
            <MdRefresh /> Erneut versuchen
          </Btn>
        }
      >
        <p className="text-sm text-[color:var(--danger)]">{error}</p>
      </Card>
    );
  }

  // No Jetson paired — invite the teacher to pair one.
  if (!jetson) {
    return (
      <>
        <Card
          title="Klassen-Jetson"
          subtitle="Optional: ein Jetson Orin Nano steht der ganzen Klasse als Inferenz-Ziel zur Verfügung."
          className="mb-4"
          padded
          right={
            <Btn variant="primary" onClick={() => setShowPairModal(true)}>
              <MdAdd /> Jetson hinzufügen
            </Btn>
          }
        >
          <p className="text-sm text-[var(--ink-3)] leading-snug">
            Kein Jetson gepaart. Folge der Anleitung in{' '}
            <span className="font-mono">docs/JETSON_DEPLOY.md</span>: SSH auf
            den Jetson, <span className="font-mono">sudo ./setup.sh</span>{' '}
            ausführen, den 6-stelligen Code hier oben mit <em>Jetson
            hinzufügen</em> eintragen.
          </p>
        </Card>
        {showPairModal && (
          <PairJetsonModal
            onClose={() => setShowPairModal(false)}
            classroomId={classroomId}
            onPaired={handlePair}
          />
        )}
      </>
    );
  }

  // Paired — main detail card. Owner display + action buttons.
  const ownerLabel = jetson.current_owner_full_name
    || jetson.current_owner_username
    || jetson.current_owner_user_id;

  return (
    <>
      <Card
        title="Klassen-Jetson"
        className="mb-4"
        padded
        right={<StatusPill jetson={jetson} />}
      >
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-4">
          <div>
            <div className="text-[10px] font-semibold uppercase tracking-wider text-[var(--ink-3)]">
              mDNS
            </div>
            <div className="text-sm font-mono text-[var(--ink)] mt-1">
              {jetson.mdns_name || '—'}
            </div>
          </div>
          <div>
            <div className="text-[10px] font-semibold uppercase tracking-wider text-[var(--ink-3)]">
              LAN-IP
            </div>
            <div className="text-sm font-mono text-[var(--ink)] mt-1">
              {jetson.lan_ip || '—'}
            </div>
          </div>
          <div>
            <div className="text-[10px] font-semibold uppercase tracking-wider text-[var(--ink-3)]">
              Agent-Version
            </div>
            <div className="text-sm font-mono text-[var(--ink)] mt-1">
              {jetson.agent_version || '—'}
            </div>
          </div>
          <div>
            <div className="text-[10px] font-semibold uppercase tracking-wider text-[var(--ink-3)]">
              Letztes Heartbeat
            </div>
            <div
              className={`text-sm font-mono mt-1 ${
                jetson.online ? 'text-[var(--ink)]' : 'text-[color:var(--amber)]'
              }`}
            >
              {formatAge(jetson.last_seen_at)}
            </div>
          </div>
        </div>

        {jetson.current_owner_user_id && (
          <div className="bg-[var(--accent-wash)] border border-[var(--accent-wash)] rounded-[var(--radius-sm)] px-4 py-3 mb-4">
            <div className="text-[10px] font-semibold uppercase tracking-wider text-[var(--accent-ink)]">
              Aktuell belegt
            </div>
            <div className="text-sm text-[var(--accent-ink)] mt-0.5">
              <span className="font-medium">{ownerLabel}</span>
              {jetson.claimed_at && (
                <span className="text-[var(--ink-3)]"> · seit {formatAge(jetson.claimed_at)}</span>
              )}
            </div>
          </div>
        )}

        {lastPairingCode?.code && (
          <div className="bg-[var(--bg-sunk)] border border-[var(--line)] rounded-[var(--radius-sm)] px-4 py-3 mb-4">
            <div className="text-[10px] font-semibold uppercase tracking-wider text-[var(--ink-3)]">
              Neuer Pairing-Code (für Re-Pair)
            </div>
            <div className="text-2xl font-mono tracking-[0.4em] text-[var(--ink)] mt-1.5">
              {lastPairingCode.code}
            </div>
            <p className="text-[11px] text-[var(--ink-3)] mt-1.5 leading-snug">
              Läuft in 30 Minuten ab. Aktuelles Pairing bleibt bestehen — der
              Code wird nur gebraucht, wenn dieser Jetson erneut gepaart
              werden soll.
            </p>
          </div>
        )}

        <div className="flex flex-wrap gap-2">
          <Btn variant="secondary" onClick={handleRegenerate} disabled={working}>
            <MdRefresh /> Pairing-Code erneuern
          </Btn>
          {jetson.current_owner_user_id && (
            <Btn variant="secondary" onClick={handleForceRelease} disabled={working}>
              <MdLockOpen /> Lock freigeben
            </Btn>
          )}
          <Btn variant="danger" onClick={handleUnpair} disabled={working}>
            <MdLinkOff /> Vom Klassenzimmer trennen
          </Btn>
          <div className="ml-auto flex items-center gap-1.5 text-xs text-[var(--ink-3)]">
            <MdLink />
            <span className="font-mono">
              ws://{jetson.lan_ip || jetson.mdns_name || '—'}:9091
            </span>
          </div>
        </div>
      </Card>
    </>
  );
}
