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
import { useRosServiceCaller } from '../hooks/useRosServiceCaller';
import { setUserList, setDatasetRepoId } from '../features/training/trainingSlice';
import { setSelectedUser, setSelectedDataset } from '../features/training/trainingSlice';
import useGroupDatasets from '../hooks/useGroupDatasets';
import { syncDatasets } from '../services/datasetsApi';
import { Pill } from './EbUI';
import Icon from './icons/Icon';
import { Led } from './sucher';

export default function DatasetSelector() {
  const dispatch = useDispatch();

  const userList = useSelector((state) => state.training.userList);
  const selectedUser = useSelector((state) => state.training.selectedUser);
  const selectedDataset = useSelector((state) => state.training.selectedDataset);
  const isTraining = useSelector((state) => state.training.isTraining);

  // Cloud-registered datasets (own + group-shared). Lives alongside the
  // ROS-backed local browse list — the two never overlap because the ROS
  // service only sees datasets cached locally on the kit, while the cloud
  // registry covers everything any group sibling has uploaded to HF Hub.
  const {
    datasets: cloudDatasets,
    error: cloudError,
    refetch: refetchCloud,
  } = useGroupDatasets();

  // Cloud identity needed for the manual "Datensätze synchronisieren" action.
  const accessToken = useSelector((state) => state.auth.session?.access_token);
  const hfUsername = useSelector((state) => state.auth.hfUsername);

  const { getUserList, getDatasetList } = useRosServiceCaller();

  const heartbeatStatus = useSelector((state) => state.tasks.heartbeatStatus);
  const rosConnected = heartbeatStatus === 'connected';
  const [loadingUsers, setLoadingUsers] = useState(false);
  const [loadingDatasets, setLoadingDatasets] = useState({});
  const [expandedUsers, setExpandedUsers] = useState({});
  const [userDatasets, setUserDatasets] = useState({});
  const [manualMode, setManualMode] = useState(false);
  const [manualRepoId, setManualRepoId] = useState('');
  const [syncing, setSyncing] = useState(false);

  // Fetch user list
  const fetchUsers = useCallback(async () => {
    setLoadingUsers(true);
    try {
      const result = await getUserList();
      console.log('Users received:', result);

      if (result && result.user_list) {
        if (result.success) {
          dispatch(setUserList(result.user_list));
          toast.success('Benutzerliste geladen.');
        } else {
          toast.error(`Benutzerliste konnte nicht geladen werden: ${result.message}`);
        }
      } else {
        toast.error('Benutzerliste konnte nicht geladen werden: ungültige Antwort.');
      }
    } catch (error) {
      console.error('Error fetching users:', error);
      toast.error(`Benutzerliste konnte nicht geladen werden: ${error.message}`);
    } finally {
      setLoadingUsers(false);
    }
  }, [getUserList, dispatch]);

  // Fetch dataset list for specific user
  const fetchDatasets = useCallback(
    async (userId) => {
      setLoadingDatasets((prev) => ({ ...prev, [userId]: true }));
      try {
        const result = await getDatasetList(userId);
        console.log('Datasets received for user', userId, ':', result);

        if (result && result.dataset_list) {
          if (result.success) {
            setUserDatasets((prev) => ({
              ...prev,
              [userId]: result.dataset_list,
            }));
            toast.success(`Datensätze von ${userId} geladen.`);
          } else {
            toast.error(`Datensätze konnten nicht geladen werden: ${result.message}`);
          }
        } else {
          toast.error('Datensätze konnten nicht geladen werden: ungültige Antwort.');
        }
      } catch (error) {
        console.error('Error fetching datasets:', error);
        toast.error(`Datensätze konnten nicht geladen werden: ${error.message}`);
      } finally {
        setLoadingDatasets((prev) => ({ ...prev, [userId]: false }));
      }
    },
    [getDatasetList]
  );

  // Toggle user folder expansion
  const toggleUserExpansion = useCallback((userId) => {
    setExpandedUsers((prev) => ({
      ...prev,
      [userId]: !prev[userId],
    }));
  }, []);

  // Auto-fetch datasets when user folder is expanded
  useEffect(() => {
    const expandedUserIds = Object.keys(expandedUsers).filter((userId) => expandedUsers[userId]);

    expandedUserIds.forEach((userId) => {
      if (!userDatasets[userId] && !loadingDatasets[userId]) {
        fetchDatasets(userId);
      }
    });
  }, [expandedUsers, userDatasets, loadingDatasets, fetchDatasets]);

  // Handle dataset selection
  const handleDatasetSelection = useCallback(
    (userId, datasetPath) => {
      dispatch(setSelectedUser(userId));
      dispatch(setSelectedDataset(datasetPath));
      dispatch(setDatasetRepoId(`${userId}/${datasetPath}`));

      // Truncate long dataset path for toast display
      const fullPath = `${userId}/${datasetPath}`;
      const maxLength = 50;
      let displayPath = fullPath;

      if (fullPath.length > maxLength) {
        const start = fullPath.substring(0, 20);
        const end = fullPath.substring(fullPath.length - 25);
        displayPath = `${start}...${end}`;
      }

      toast.success(`Datensatz ausgewählt:\n${displayPath}`);
    },
    [dispatch]
  );

  // Refresh datasets for a specific user
  const refreshUserDatasets = useCallback(
    async (userId, e) => {
      e.stopPropagation(); // Prevent folder toggle
      await fetchDatasets(userId);
    },
    [fetchDatasets]
  );

  // Handle manual repo ID submission
  const handleManualSubmit = useCallback(() => {
    const trimmed = manualRepoId.trim();
    if (!trimmed) {
      toast.error('Bitte gib eine HuggingFace Repo-ID ein');
      return;
    }
    if (!trimmed.includes('/')) {
      toast.error('Format: benutzername/datensatzname');
      return;
    }
    const [user, ...rest] = trimmed.split('/');
    const dataset = rest.join('/');
    dispatch(setSelectedUser(user));
    dispatch(setSelectedDataset(dataset));
    dispatch(setDatasetRepoId(trimmed));
    toast.success(`Datensatz ausgewählt:\n${trimmed}`);
  }, [manualRepoId, dispatch]);

  // Manually reconcile the cloud registry with HF Hub: enumerate the
  // student's HF datasets (by their linked hf_username) and register the
  // missing ones. The escape hatch when the auto-register on upload missed
  // a dataset (offline at upload, a transient 5xx, or a dataset uploaded
  // outside this app).
  const handleSyncDatasets = useCallback(async () => {
    if (!accessToken) {
      toast.error('Nicht angemeldet — bitte zuerst im Training-Tab anmelden.');
      return;
    }
    setSyncing(true);
    try {
      const result = await syncDatasets(accessToken);
      // Pull the freshly-registered rows into the visible list immediately
      // (don't wait for the Realtime channel).
      await refetchCloud();
      const registered = result?.registered ?? 0;
      if (registered > 0) {
        toast.success(
          registered === 1
            ? '1 neuer Datensatz synchronisiert'
            : `${registered} neue Datensätze synchronisiert`
        );
      } else {
        toast.success('Keine neuen Datensätze gefunden');
      }
    } catch (err) {
      if (err?.status === 400) {
        // hf_username not linked yet.
        toast.error(
          'Es ist noch keine HuggingFace-ID mit deinem Konto verknüpft. ' +
            'Hinterlege dein Token auf der Startseite, dann erneut synchronisieren.'
        );
      } else if (err?.status === 502) {
        toast.error(
          'HuggingFace ist gerade nicht erreichbar. Bitte später erneut versuchen.'
        );
      } else {
        toast.error(
          err?.detail ||
            err?.message ||
            'Synchronisierung fehlgeschlagen. Bitte erneut versuchen.'
        );
      }
    } finally {
      setSyncing(false);
    }
  }, [accessToken, refetchCloud]);

  // Auto-switch to manual mode if ROS is not connected
  useEffect(() => {
    if (!rosConnected && !manualMode) {
      setManualMode(true);
    }
  }, [rosConnected, manualMode]);

  // Fetch users when component mounts (only in browse mode)
  useEffect(() => {
    if (!manualMode) {
      fetchUsers();
    }
  }, [fetchUsers, manualMode]);

  const classCard = clsx('su-plate', 'su-plate--plate', 'su-trn-card', 'w-full');

  const classRefreshButton = clsx('su-key', 'su-key--md', 'w-full', 'mb-4');

  const classCurrentSelection = clsx('su-trn-selected', 'mb-4');

  const classTreeContainer = clsx('su-plate', 'su-plate--well', 'su-trn-list', 'max-h-96', 'overflow-y-auto');

  const classUserFolder = clsx(
    'flex',
    'items-center',
    'px-3',
    'py-2',
    'cursor-pointer',
    'hover:bg-gray-100',
    'border-b',
    'border-gray-200',
    'transition-colors'
  );

  const classSelectedUserFolder = clsx(
    'flex',
    'items-center',
    'px-3',
    'py-2',
    'cursor-pointer',
    'bg-teal-50',
    'border-b',
    'border-teal-200',
    'transition-colors',
    'hover:bg-teal-100'
  );

  const classDatasetItem = clsx(
    'flex',
    'items-center',
    'px-6',
    'py-2',
    'cursor-pointer',
    'hover:bg-teal-50',
    'border-b',
    'border-gray-100',
    'transition-colors',
    'text-sm'
  );

  const classSelectedDataset = clsx('bg-teal-100', 'text-teal-800', 'font-medium');

  const classRefreshIcon = clsx(
    'ml-auto',
    'p-1',
    'rounded',
    'hover:bg-gray-200',
    'transition-colors'
  );

  return (
    <div className={classCard}>
      <div className="flex items-center justify-between gap-2 mb-4 flex-wrap">
        <h1 className="su-trn-card__title">Datensatzauswahl</h1>
        <span className="su-trn-mode">
          <button
            type="button"
            onClick={() => setManualMode(!manualMode)}
            className="su-trn-mode__btn"
            disabled={isTraining}
          >
            <Icon name="pencil" size={13} />
            {manualMode ? 'Durchsuchen' : 'Manuell'}
          </button>
        </span>
      </div>

      {/* Current Selection Display */}
      {selectedUser && selectedDataset && (
        <div className={classCurrentSelection}>
          <Led tone="sig" />
          <div className="truncate">
            <strong>Ausgewählt: </strong>
            <span className="font-semibold text-[var(--ink)]" title={`${selectedUser}/${selectedDataset}`}>
              {selectedUser.length > 15 ? `${selectedUser.substring(0, 12)}...` : selectedUser}/
            </span>
            <span className="font-semibold text-[var(--ink)]" title={`${selectedUser}/${selectedDataset}`}>
              {selectedDataset.length > 30
                ? `${selectedDataset.substring(0, 25)}...${selectedDataset.substring(
                    selectedDataset.length - 10
                  )}`
                : selectedDataset}
            </span>
          </div>
        </div>
      )}

      {/* Group-shared datasets (cloud registry) — always rendered so the
          sync button + empty-state hint are reachable even with zero rows. */}
      <div className="mb-4">
        <div className="flex items-center justify-between mb-2 gap-2 flex-wrap">
          <h3 className="su-lab">
            Eigene &amp; geteilte Datensätze
          </h3>
          <div className="flex items-center gap-2">
            {cloudDatasets && cloudDatasets.length > 0 && (
              <span className="su-lab">
                {cloudDatasets.length}
              </span>
            )}
            <button
              type="button"
              onClick={handleSyncDatasets}
              disabled={syncing || isTraining}
              title="Eigene HuggingFace-Datensätze mit der Cloud abgleichen"
              className="su-key su-key--sm su-trn-sync"
            >
              <Icon name="refresh" className={syncing ? 'animate-spin' : ''} size={14} />
              {syncing ? 'Synchronisiere…' : 'Datensätze synchronisieren'}
            </button>
          </div>
        </div>

        {cloudError && (
          <div className="flex items-start gap-2 text-xs text-red-700 bg-red-50 border border-red-200 rounded-md p-2 mb-2">
            <Icon name="failed" className="shrink-0 mt-0.5" size={14} />
            <div className="min-w-0 flex-1">
              <span>{cloudError}</span>
              <button
                type="button"
                onClick={refetchCloud}
                className="ml-1 underline hover:no-underline"
              >
                Erneut laden
              </button>
            </div>
          </div>
        )}

        {!hfUsername && !cloudError && (
          <p className="text-[11px] text-amber-700 bg-amber-50 border border-amber-200 rounded-md p-2 mb-2 leading-snug">
            Noch kein Hugging-Face-Konto verknüpft. Hinterlege dein Token auf
            der Startseite, danach erscheinen deine Datensätze hier automatisch.
          </p>
        )}

        {cloudDatasets && cloudDatasets.length > 0 ? (
          <div className="su-plate su-plate--well su-trn-list max-h-60 overflow-y-auto">
            {cloudDatasets.map((d) => {
              const [user, ...rest] = (d.hf_repo_id || '/').split('/');
              const dataset = rest.join('/');
              const isSelected =
                selectedUser === user && selectedDataset === dataset;
              return (
                <button
                  key={d.id}
                  type="button"
                  onClick={() => !isTraining && handleDatasetSelection(user, dataset)}
                  className={clsx('su-trn-row', isSelected && 'su-trn-row--on')}
                  disabled={isTraining}
                >
                  <Led tone={isSelected ? 'sig' : 'off'} />
                  <div className="min-w-0 flex-1">
                    <div className="font-medium truncate">{d.name || dataset}</div>
                    <div className="text-[11.5px] text-[var(--ink-2)] font-mono truncate">
                      {d.hf_repo_id}
                    </div>
                  </div>
                  {d.is_group_shared && !d.is_owned && (
                    <Pill tone="success">Gruppe</Pill>
                  )}
                  {d.is_owned && d.is_group_shared && (
                    <Pill tone="accent">Eigene · geteilt</Pill>
                  )}
                </button>
              );
            })}
          </div>
        ) : (
          !cloudError && (
            <p className="su-plate su-plate--well text-[13px] text-[var(--ink)] p-3 leading-snug">
              Noch keine Datensätze – nimm im Aufnahme-Tab etwas auf oder klicke
              „Datensätze synchronisieren", um deine HuggingFace-Datensätze zu
              laden.
            </p>
          )
        )}
      </div>

      {/* Manual HuggingFace Repo ID Input */}
      {manualMode ? (
        <div className="space-y-3">
          <p className="text-[13px] text-[var(--ink-2)]">
            HuggingFace Datensatz-ID direkt eingeben (z.B. <span className="font-mono text-[var(--ink)]">benutzername/datensatzname</span>)
          </p>
          <input
            type="text"
            className="su-field w-full h-11 px-3 text-sm"
            placeholder="benutzername/datensatzname"
            value={manualRepoId}
            onChange={(e) => setManualRepoId(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && handleManualSubmit()}
            disabled={isTraining}
          />
          <button
            type="button"
            className="su-key su-key--signal su-key--md w-full"
            onClick={handleManualSubmit}
            disabled={!manualRepoId.trim() || isTraining}
          >
            Datensatz auswählen
          </button>
          {!rosConnected && (
            <p className="text-xs text-amber-600 bg-amber-50 rounded-md p-2">
              Roboter-Umgebung nicht verbunden. Verwende die manuelle Eingabe oder starte die Umgebung, um lokale Datensätze zu durchsuchen.
            </p>
          )}
        </div>
      ) : (
        <>

      {/* Refresh Button */}
      <button
        className={classRefreshButton}
        onClick={fetchUsers}
        disabled={loadingUsers || isTraining}
      >
        <Icon name="refresh" className={loadingUsers ? 'animate-spin' : ''} />
        {loadingUsers ? 'Laden...' : 'Benutzer aktualisieren'}
      </button>

      {/* Folder Tree View */}
      <div className={classTreeContainer}>
        {userList.length === 0 ? (
          <div className="text-center py-8 text-[var(--ink)]">
            {loadingUsers ? 'Benutzer werden geladen...' : 'Keine Benutzer gefunden'}
          </div>
        ) : (
          userList.map((user) => {
            const isSelectedUser = selectedUser === user;

            return (
              <div key={user}>
                {/* User Folder */}
                <div
                  className={isSelectedUser ? classSelectedUserFolder : classUserFolder}
                  onClick={() => toggleUserExpansion(user)}
                >
                  {/* Expand/Collapse Arrow */}
                  <div className="mr-2">
                    {expandedUsers[user] ? (
                      <Icon name="chevronDown" className="text-gray-600" />
                    ) : (
                      <Icon name="chevronRight" className="text-gray-600" />
                    )}
                  </div>

                  {/* Folder Icon */}
                  <div className="mr-2">
                    {expandedUsers[user] ? (
                      <Icon name="folderOpen"
                        className={isSelectedUser ? 'text-teal-700' : 'text-teal-600'}
                      />
                    ) : (
                      <Icon name="folder" className={isSelectedUser ? 'text-teal-700' : 'text-teal-600'} />
                    )}
                  </div>

                  {/* User Name */}
                  <span
                    className={
                      isSelectedUser ? 'font-bold text-teal-800' : 'font-medium text-gray-800'
                    }
                  >
                    {user}
                  </span>

                  {/* Selection indicator */}
                  {isSelectedUser && (
                    <div className="ml-2 px-2 py-1 bg-teal-200 text-teal-800 text-xs rounded-full">
                      Ausgewählt
                    </div>
                  )}

                  {/* Loading or Refresh Icon */}
                  {loadingDatasets[user] ? (
                    <Icon name="loading" className="ml-auto animate-spin text-gray-500" />
                  ) : expandedUsers[user] ? (
                    <button
                      className={classRefreshIcon}
                      onClick={(e) => !isTraining && refreshUserDatasets(user, e)}
                      title="Datensätze neu laden"
                      aria-label="Datensätze neu laden"
                    >
                      <Icon name="refresh" className="text-gray-500" size={16} />
                    </button>
                  ) : null}
                </div>

                {/* Dataset List (shown when expanded) */}
                {expandedUsers[user] && (
                  <div>
                    {userDatasets[user] ? (
                      userDatasets[user].length === 0 ? (
                        <div className="px-6 py-4 text-gray-500 text-sm">Keine Datensätze gefunden</div>
                      ) : (
                        userDatasets[user].map((dataset) => (
                          <div
                            key={dataset}
                            className={clsx(
                              classDatasetItem,
                              selectedUser === user &&
                                selectedDataset === dataset &&
                                classSelectedDataset
                            )}
                            onClick={() => !isTraining && handleDatasetSelection(user, dataset)}
                          >
                            <div className="mr-2">
                              <Icon name="database" className="text-green-600" />
                            </div>
                            <span>{dataset}</span>
                          </div>
                        ))
                      )
                    ) : (
                      <div className="px-6 py-4 text-gray-500 text-sm">
                        {loadingDatasets[user] ? 'Datensätze werden geladen...' : 'Klicken zum Laden der Datensätze'}
                      </div>
                    )}
                  </div>
                )}
              </div>
            );
          })
        )}
      </div>
        </>
      )}
    </div>
  );
}
