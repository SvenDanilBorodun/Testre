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
//
// Author: Kiwoong Park

import React, { useCallback, useEffect, useState } from 'react';
import { useDispatch, useSelector } from 'react-redux';
import clsx from 'clsx';
import toast from 'react-hot-toast';
import { MdRefresh } from 'react-icons/md';
import { useRosServiceCaller } from '../hooks/useRosServiceCaller';
import TaskPhase from '../constants/taskPhases';
import { selectRobotType, removeAllTags } from '../features/tasks/taskSlice';
import { setRobotTypeList, setIsFirstLoadTrue } from '../features/ui/uiSlice';
import { Btn, Card, Pill, Stat } from './EbUI';

export default function RobotTypeSelector() {
  const dispatch = useDispatch();

  const robotTypeList = useSelector((state) => state.ui.robotTypeList);
  const robotType = useSelector((state) => state.tasks.taskStatus.robotType);
  const taskStatus = useSelector((state) => state.tasks.taskStatus);

  const { getRobotTypeList, setRobotType } = useRosServiceCaller();

  const [loading, setLoading] = useState(false);
  const [fetching, setFetching] = useState(false);
  const [selectedRobotType, setSelectedRobotType] = useState('');

  const fetchRobotTypes = useCallback(
    async (retries = 5, isManual = false) => {
      setFetching(true);
      for (let attempt = 1; attempt <= retries; attempt++) {
        try {
          const result = await getRobotTypeList();
          if (result && result.robot_types) {
            dispatch(setRobotTypeList(result.robot_types));
            if (isManual) {
              toast.success('Robotertypen erfolgreich geladen');
            }
            setFetching(false);
            return;
          }
        } catch (error) {
          if (attempt < retries) {
            await new Promise((r) => setTimeout(r, 2000));
            continue;
          }
          toast.error(`Robotertypen konnten nicht geladen werden: ${error.message}`);
        }
      }
      setFetching(false);
    },
    [getRobotTypeList, dispatch]
  );

  const handleSetRobotType = async () => {
    if (!selectedRobotType) {
      toast.error('Bitte wähle einen Robotertyp');
      return;
    }

    if (taskStatus.phase > TaskPhase.READY) {
      toast.error('Robotertyp kann während einer laufenden Aufgabe nicht geändert werden', {
        duration: 4000,
      });
      return;
    }

    setLoading(true);
    try {
      const result = await setRobotType(selectedRobotType);
      if (result && result.success) {
        dispatch(selectRobotType(selectedRobotType));
        toast.success(`Robotertyp gesetzt auf: ${selectedRobotType}`);
        dispatch(setIsFirstLoadTrue('record'));
        dispatch(removeAllTags());
      } else {
        toast.error(`Robotertyp konnte nicht gesetzt werden: ${result?.message || 'Unbekannter Fehler'}`);
      }
    } catch (error) {
      toast.error(`Robotertyp konnte nicht gesetzt werden: ${error.message}`);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchRobotTypes();
  }, [fetchRobotTypes]);

  useEffect(() => {
    if (robotType && !selectedRobotType) {
      setSelectedRobotType(robotType);
    }
  }, [robotType, selectedRobotType]);

  const disabled = fetching || loading || taskStatus.phase > 0;

  return (
    <Card
      title="Robotertyp auswählen"
      subtitle="Wird an ROS-Bridge übermittelt"
      right={
        <Btn
          variant="ghost"
          size="sm"
          onClick={() => fetchRobotTypes(1, true)}
          disabled={disabled}
        >
          <MdRefresh className={clsx(fetching && 'animate-spin')} /> Aktualisieren
        </Btn>
      }
    >
      <div>
        <span className="text-xs font-medium text-[var(--ink-2)] block mb-1.5">
          Aktueller Typ
        </span>
        <div className="h-10 px-3 flex items-center justify-between bg-[var(--bg-sunk)] rounded-[var(--radius-sm)] border border-[var(--line)]">
          <span className="font-mono text-sm text-[var(--ink)]">
            {robotType || '—'}
          </span>
          {robotType && <Pill tone="success">✓ aktiv</Pill>}
        </div>
      </div>

      {taskStatus.phase > 0 && (
        <div className="mt-3 text-xs text-[color:var(--amber)] bg-[var(--amber-wash)] px-3 py-2 rounded-[var(--radius-sm)] leading-snug">
          <strong>Aufgabe läuft (Phase {taskStatus.phase})</strong>
          <div className="opacity-80 mt-0.5">
            Robotertyp kann während der Ausführung nicht geändert werden.
          </div>
        </div>
      )}

      <div className="mt-4">
        <span className="text-xs font-medium text-[var(--ink-2)] block mb-1.5">Ändern</span>
        <select
          className="eb w-full h-10 pl-3 bg-white border border-[var(--line)] rounded-[var(--radius-sm)] text-sm text-[var(--ink)] focus:outline-none focus:border-[var(--accent)] focus:ring-2 focus:ring-[color:var(--accent-wash)] transition"
          value={selectedRobotType}
          onChange={(e) => setSelectedRobotType(e.target.value)}
          disabled={disabled}
        >
          <option value="" disabled>
            Robotertyp wählen…
          </option>
          {robotTypeList.map((type) => (
            <option key={type} value={type}>
              {type}
            </option>
          ))}
        </select>
      </div>

      <Btn
        variant="primary"
        className="w-full justify-center mt-4"
        onClick={handleSetRobotType}
        disabled={disabled || !selectedRobotType}
      >
        {loading ? 'Wird gesetzt…' : 'Robotertyp festlegen'}
      </Btn>

      {robotTypeList.length === 0 && !fetching && (
        <div className="text-center text-[var(--ink-3)] text-xs mt-4">
          Keine Robotertypen verfügbar. Bitte ROS-Verbindung prüfen.
        </div>
      )}

      <div className="mt-5 pt-4 border-t border-[var(--line)] grid grid-cols-3 gap-3">
        <Stat label="Status" value={robotType ? 'OK' : '—'} tone={robotType ? 'accent' : undefined} />
        <Stat label="Typen" value={String(robotTypeList.length)} />
        <Stat label="Phase" value={String(taskStatus.phase ?? 0)} />
      </div>
    </Card>
  );
}
