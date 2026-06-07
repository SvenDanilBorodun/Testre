// Copyright 2026 EduBotics
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

// Name-based dataset quick-pick for the Daten tools (leLab-comparison PR-1):
// students pick "Benutzer -> Datensatz" from two dropdowns instead of
// navigating the filesystem tree in the FileBrowserModal. Built on the same
// ROS services the training page's DatasetSelector uses
// (/training/get_user_list + /training/get_dataset_list) but deliberately
// NOT that component — DatasetSelector is prop-less and coupled to the
// training Redux slice, while delete/merge need a plain LOCAL PATH:
//   <DATASET_PATH><benutzer>/<datensatz>
// The browse modal stays available as the fallback for unusual locations.

import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useSelector } from 'react-redux';
import toast from 'react-hot-toast';
import { MdRefresh } from 'react-icons/md';
import { useRosServiceCaller } from '../../../hooks/useRosServiceCaller';
import { DEFAULT_PATHS } from '../../../constants/paths';

const SELECT_CLASS =
  'text-sm h-9 px-2 border border-gray-300 rounded-md bg-white ' +
  'focus:outline-none focus:ring-2 focus:ring-blue-500 ' +
  'disabled:bg-gray-100 disabled:cursor-not-allowed';

export default function LocalDatasetQuickPick({ onPick, disabled = false }) {
  const { getUserList, getDatasetList } = useRosServiceCaller();
  const heartbeatStatus = useSelector((state) => state.tasks.heartbeatStatus);
  const rosConnected = heartbeatStatus === 'connected';

  const [users, setUsers] = useState([]);
  const [selectedUser, setSelectedUser] = useState('');
  const [datasets, setDatasets] = useState([]);
  const [loading, setLoading] = useState(false);
  const loadedOnceRef = useRef(false);

  const loadUsers = useCallback(async () => {
    setLoading(true);
    try {
      const result = await getUserList();
      if (result?.success && Array.isArray(result.user_list)) {
        setUsers(result.user_list);
        if (result.user_list.length === 0) {
          toast('Noch keine lokalen Datensätze aufgenommen.', { icon: 'ℹ️' });
        }
      } else {
        toast.error('Benutzerliste konnte nicht geladen werden.');
      }
    } catch (error) {
      console.error('LocalDatasetQuickPick getUserList failed:', error);
      toast.error('Benutzerliste konnte nicht geladen werden.');
    } finally {
      setLoading(false);
    }
  }, [getUserList]);

  const loadDatasets = useCallback(
    async (user) => {
      setLoading(true);
      setDatasets([]);
      try {
        const result = await getDatasetList(user);
        if (result?.success && Array.isArray(result.dataset_list)) {
          setDatasets(result.dataset_list);
          if (result.dataset_list.length === 0) {
            toast(`Keine Datensätze für „${user}" gefunden.`, { icon: 'ℹ️' });
          }
        } else {
          toast.error('Datensatzliste konnte nicht geladen werden.');
        }
      } catch (error) {
        console.error('LocalDatasetQuickPick getDatasetList failed:', error);
        toast.error('Datensatzliste konnte nicht geladen werden.');
      } finally {
        setLoading(false);
      }
    },
    [getDatasetList]
  );

  // Populate the user dropdown once the local rosbridge is up.
  useEffect(() => {
    if (rosConnected && !loadedOnceRef.current) {
      loadedOnceRef.current = true;
      loadUsers();
    }
  }, [rosConnected, loadUsers]);

  const handleUserChange = (user) => {
    setSelectedUser(user);
    if (user) loadDatasets(user);
  };

  const handleDatasetChange = (name) => {
    if (!name || !selectedUser) return;
    // DATASET_PATH carries a trailing slash (constants/paths.js).
    onPick(`${DEFAULT_PATHS.DATASET_PATH}${selectedUser}/${name}`);
  };

  return (
    <div className="flex flex-row items-center gap-2 w-full flex-wrap">
      <span className="text-sm font-medium text-gray-700">Schnellauswahl:</span>
      <select
        className={SELECT_CLASS}
        value={selectedUser}
        onChange={(e) => handleUserChange(e.target.value)}
        disabled={disabled || loading || !rosConnected}
        aria-label="Benutzer wählen"
      >
        <option value="">Benutzer wählen …</option>
        {users.map((user) => (
          <option key={user} value={user}>
            {user}
          </option>
        ))}
      </select>
      <select
        className={SELECT_CLASS}
        value=""
        onChange={(e) => handleDatasetChange(e.target.value)}
        disabled={disabled || loading || !selectedUser}
        aria-label="Datensatz wählen"
      >
        <option value="">Datensatz wählen …</option>
        {datasets.map((name) => (
          <option key={name} value={name}>
            {name}
          </option>
        ))}
      </select>
      <button
        type="button"
        onClick={() => {
          loadUsers();
          if (selectedUser) loadDatasets(selectedUser);
        }}
        disabled={disabled || loading || !rosConnected}
        className="flex items-center justify-center text-blue-500 rounded-md p-1 hover:text-blue-700 hover:bg-gray-200 disabled:opacity-50"
        aria-label="Listen aktualisieren"
        title="Listen aktualisieren"
      >
        <MdRefresh className="w-6 h-6" />
      </button>
    </div>
  );
}
