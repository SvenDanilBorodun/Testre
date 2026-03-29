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
import {
  MdRefresh,
  MdFolder,
  MdFolderOpen,
  MdDataset,
  MdKeyboardArrowRight,
  MdKeyboardArrowDown,
  MdEdit,
} from 'react-icons/md';
import { useRosServiceCaller } from '../hooks/useRosServiceCaller';
import { setUserList, setDatasetRepoId } from '../features/training/trainingSlice';
import { setSelectedUser, setSelectedDataset } from '../features/training/trainingSlice';

export default function DatasetSelector() {
  const dispatch = useDispatch();

  const userList = useSelector((state) => state.training.userList);
  const selectedUser = useSelector((state) => state.training.selectedUser);
  const selectedDataset = useSelector((state) => state.training.selectedDataset);
  const isTraining = useSelector((state) => state.training.isTraining);

  const { getUserList, getDatasetList } = useRosServiceCaller();

  const heartbeatStatus = useSelector((state) => state.tasks.heartbeatStatus);
  const rosConnected = heartbeatStatus === 'connected';
  const [loadingUsers, setLoadingUsers] = useState(false);
  const [loadingDatasets, setLoadingDatasets] = useState({});
  const [expandedUsers, setExpandedUsers] = useState({});
  const [userDatasets, setUserDatasets] = useState({});
  const [manualMode, setManualMode] = useState(false);
  const [manualRepoId, setManualRepoId] = useState('');

  // Fetch user list
  const fetchUsers = useCallback(async () => {
    setLoadingUsers(true);
    try {
      const result = await getUserList();
      console.log('Users received:', result);

      if (result && result.user_list) {
        if (result.success) {
          dispatch(setUserList(result.user_list));
          toast.success('User list loaded successfully');
        } else {
          toast.error('Failed to get user list: ' + result.message);
        }
      } else {
        toast.error('Failed to get user list: Invalid response');
      }
    } catch (error) {
      console.error('Error fetching users:', error);
      toast.error(`Failed to get user list: ${error.message}`);
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
            toast.success(`Dataset list loaded for user: ${userId}`);
          } else {
            toast.error('Failed to get dataset list: ' + result.message);
          }
        } else {
          toast.error('Failed to get dataset list: Invalid response');
        }
      } catch (error) {
        console.error('Error fetching datasets:', error);
        toast.error(`Failed to get dataset list: ${error.message}`);
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

  const classCard = clsx(
    'bg-white',
    'border',
    'border-gray-200',
    'rounded-2xl',
    'shadow-lg',
    'p-6',
    'w-full',
    'max-w-lg'
  );

  const classTitle = clsx('text-xl', 'font-bold', 'text-gray-800', 'mb-6', 'text-left');

  const classRefreshButton = clsx(
    'w-full',
    'px-4',
    'py-2',
    'bg-gray-500',
    'text-white',
    'rounded-md',
    'font-medium',
    'transition-colors',
    'hover:bg-gray-600',
    'disabled:bg-gray-400',
    'disabled:cursor-not-allowed',
    'flex',
    'items-center',
    'justify-center',
    'gap-2',
    'mb-4'
  );

  const classCurrentSelection = clsx(
    'text-sm',
    'text-gray-600',
    'bg-gray-100',
    'px-3',
    'py-2',
    'rounded-md',
    'text-center',
    'mb-4'
  );

  const classTreeContainer = clsx(
    'border',
    'border-gray-300',
    'rounded-md',
    'max-h-96',
    'overflow-y-auto',
    'bg-gray-50'
  );

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
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-xl font-bold text-gray-800">Datensatzauswahl</h1>
        <button
          onClick={() => setManualMode(!manualMode)}
          className={clsx(
            'flex items-center gap-1 px-3 py-1 text-xs font-medium rounded-full transition-colors',
            manualMode
              ? 'bg-teal-100 text-teal-700 hover:bg-teal-200'
              : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
          )}
          disabled={isTraining}
        >
          <MdEdit size={14} />
          {manualMode ? 'Durchsuchen' : 'Manuell'}
        </button>
      </div>

      {/* Current Selection Display */}
      {selectedUser && selectedDataset && (
        <div className={classCurrentSelection}>
          <div className="truncate">
            <strong>Ausgewählt: </strong>
            <span className="text-teal-500" title={`${selectedUser}/${selectedDataset}`}>
              {selectedUser.length > 15 ? `${selectedUser.substring(0, 12)}...` : selectedUser}/
            </span>
            <span className="text-teal-500" title={`${selectedUser}/${selectedDataset}`}>
              {selectedDataset.length > 30
                ? `${selectedDataset.substring(0, 25)}...${selectedDataset.substring(
                    selectedDataset.length - 10
                  )}`
                : selectedDataset}
            </span>
          </div>
        </div>
      )}

      {/* Manual HuggingFace Repo ID Input */}
      {manualMode ? (
        <div className="space-y-3">
          <p className="text-sm text-gray-500">
            HuggingFace Datensatz-ID direkt eingeben (z.B. <span className="font-mono text-teal-600">benutzername/datensatzname</span>)
          </p>
          <input
            type="text"
            className={clsx(
              'w-full px-3 py-2 border rounded-md text-sm',
              'focus:outline-none focus:ring-2 focus:ring-teal-500 focus:border-transparent',
              'border-gray-300'
            )}
            placeholder="benutzername/datensatzname"
            value={manualRepoId}
            onChange={(e) => setManualRepoId(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && handleManualSubmit()}
            disabled={isTraining}
          />
          <button
            className={clsx(
              'w-full px-4 py-2 rounded-md font-medium transition-colors text-sm',
              manualRepoId.trim()
                ? 'bg-teal-600 text-white hover:bg-teal-700'
                : 'bg-gray-300 text-gray-500 cursor-not-allowed'
            )}
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
        <MdRefresh className={loadingUsers ? 'animate-spin' : ''} />
        {loadingUsers ? 'Laden...' : 'Benutzer aktualisieren'}
      </button>

      {/* Folder Tree View */}
      <div className={classTreeContainer}>
        {userList.length === 0 ? (
          <div className="text-center py-8 text-gray-500">
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
                      <MdKeyboardArrowDown className="text-gray-600" />
                    ) : (
                      <MdKeyboardArrowRight className="text-gray-600" />
                    )}
                  </div>

                  {/* Folder Icon */}
                  <div className="mr-2">
                    {expandedUsers[user] ? (
                      <MdFolderOpen
                        className={isSelectedUser ? 'text-teal-700' : 'text-teal-600'}
                      />
                    ) : (
                      <MdFolder className={isSelectedUser ? 'text-teal-700' : 'text-teal-600'} />
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
                    <MdRefresh className="ml-auto animate-spin text-gray-500" />
                  ) : expandedUsers[user] ? (
                    <button
                      className={classRefreshIcon}
                      onClick={(e) => !isTraining && refreshUserDatasets(user, e)}
                      title="Refresh datasets"
                    >
                      <MdRefresh className="text-gray-500" size={16} />
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
                              <MdDataset className="text-green-600" />
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
