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

import React, { useState, useCallback, useEffect } from 'react';
import clsx from 'clsx';
import { useSelector, useDispatch } from 'react-redux';
import toast from 'react-hot-toast';
import {
  setHFUserId,
  setHFRepoIdUpload,
  setHFRepoIdDownload,
  setHFDataType,
} from '../editDatasetSlice';
import { useRosServiceCaller } from '../../../hooks/useRosServiceCaller';
import FileBrowserModal from '../../../components/FileBrowserModal';
import { useHfUserList } from '../../../hooks/useHfUserList';
import SectionSelector from './SectionSelector';
import { DEFAULT_PATHS, TARGET_FOLDERS, TARGET_FILES } from '../../../constants/paths';
import HFStatus, { hfStatusLabelDe } from '../../../constants/HFStatus';
import Icon from '../../../components/icons/Icon';

// Constants
const SECTION_NAME = {
  UPLOAD: 'upload',
  DOWNLOAD: 'download',
};

// HuggingFace repository name validation
const validateHfRepoName = (repoName) => {
  if (!repoName) return { isValid: false, message: '' };

  // Check length (max 96 characters)
  if (repoName.length > 96) {
    return {
      isValid: false,
      message: 'Der Repository-Name darf höchstens 96 Zeichen lang sein.',
    };
  }

  // Check if starts or ends with '-' or '.'
  if (
    repoName.startsWith('-') ||
    repoName.startsWith('.') ||
    repoName.endsWith('-') ||
    repoName.endsWith('.')
  ) {
    return {
      isValid: false,
      message: 'Der Repository-Name darf nicht mit „-“ oder „.“ beginnen oder enden.',
    };
  }

  // Check for forbidden patterns '--' and '..'
  if (repoName.includes('--') || repoName.includes('..')) {
    return {
      isValid: false,
      message: 'Der Repository-Name darf kein „--“ und kein „..“ enthalten.',
    };
  }

  // Check for allowed characters only (alphanumeric, '-', '_', '.')
  const allowedPattern = /^[a-zA-Z0-9._-]+$/;
  if (!allowedPattern.test(repoName)) {
    return {
      isValid: false,
      message: 'Der Repository-Name darf nur Buchstaben, Ziffern, „-“, „_“ und „.“ enthalten.',
    };
  }

  return { isValid: true, message: '' };
};

// Style Classes
const STYLES = {
  textInput: clsx(
    'text-sm',
    'w-full',
    'h-10',
    'p-2',
    'border',
    'border-gray-300',
    'rounded-md',
    'focus:outline-none',
    'focus:ring-2',
    'focus:ring-blue-500',
    'focus:border-transparent'
  ),
  selectUserID: clsx(
    'text-md',
    'w-full',
    'max-w-120',
    'min-w-60',
    'h-8',
    'px-2',
    'border',
    'border-gray-300',
    'rounded-md',
    'focus:outline-none',
    'focus:ring-2',
    'focus:ring-blue-500',
    'focus:border-transparent'
  ),
  loadUserButton: clsx('px-3', 'py-1', 'text-md', 'font-medium', 'rounded-xl', 'transition-colors'),
  cancelButton: clsx('px-6', 'py-2', 'text-sm', 'font-medium', 'rounded-lg', 'transition-colors'),
};

// Folder Browse Button Component
const FolderBrowseButton = ({ onClick, disabled = false, ariaLabel }) => {
  return (
    <button
      type="button"
      onClick={onClick}
      className={clsx('flex items-center justify-center w-10 h-10 rounded-md transition-colors', {
        'text-blue-500 bg-gray-200 hover:text-blue-700': !disabled,
        'text-gray-400 bg-gray-100 cursor-not-allowed': disabled,
      })}
      aria-label={ariaLabel}
      disabled={disabled}
    >
      <Icon name="folderOpen" className="w-8 h-8" />
    </button>
  );
};

const HuggingfaceSection = () => {
  const dispatch = useDispatch();
  const userId = useSelector((state) => state.editDataset.hfUserId);
  const hfRepoIdUpload = useSelector((state) => state.editDataset.hfRepoIdUpload);
  const hfRepoIdDownload = useSelector((state) => state.editDataset.hfRepoIdDownload);
  const hfStatus = useSelector((state) => state.editDataset.hfStatus);
  const downloadStatus = useSelector((state) => state.editDataset.downloadStatus);
  const uploadStatus = useSelector((state) => state.editDataset.uploadStatus);
  const hfDataType = useSelector((state) => state.editDataset.hfDataType);

  const { controlHfServer } = useRosServiceCaller();
  // Benutzer-ID list from Redux (fetched once on connect, survives tab switches).
  const { hfUserList, reload: reloadHfUsers } = useHfUserList();

  // Local states
  const [activeSection, setActiveSection] = useState(SECTION_NAME.UPLOAD);
  const [hfLocalDirUpload, setHfLocalDirUpload] = useState('');
  const [isUploading, setIsUploading] = useState(false);
  const [isDownloading, setIsDownloading] = useState(false);
  const [showHfLocalDirBrowserModal, setShowHfLocalDirBrowserModal] = useState(false);
  const [showHfLocalModelDirBrowserModal, setShowHfLocalModelDirBrowserModal] = useState(false);
  const [isLoading, setIsLoading] = useState(false);

  // Validation states
  const [uploadRepoValidation, setUploadRepoValidation] = useState({ isValid: true, message: '' });
  const [downloadRepoValidation, setDownloadRepoValidation] = useState({
    isValid: true,
    message: '',
  });

  // Computed values
  const isProcessing = isUploading || isDownloading;

  // Section availability
  const canChangeSection = !isProcessing;
  const canChangeDataType = !isProcessing;

  const isHfStatusReady =
    hfStatus === HFStatus.IDLE || hfStatus === HFStatus.SUCCESS || hfStatus === HFStatus.FAILED;

  const uploadButtonEnabled =
    !isUploading &&
    !isDownloading &&
    hfRepoIdUpload?.trim() &&
    hfLocalDirUpload?.trim() &&
    uploadRepoValidation.isValid &&
    userId?.trim() &&
    isHfStatusReady;

  const downloadButtonEnabled =
    !isUploading &&
    !isDownloading &&
    hfRepoIdDownload?.trim() &&
    downloadRepoValidation.isValid &&
    userId?.trim() &&
    isHfStatusReady;
  // Button variants helper function
  const getButtonVariant = (variant, isActive = true, isLoading = false) => {
    const variants = {
      blue: {
        active: 'bg-blue-200 text-blue-800 hover:bg-blue-300',
        disabled: 'bg-gray-200 text-gray-500 cursor-not-allowed',
      },
      red: {
        active: 'bg-red-200 text-red-800 hover:bg-red-300',
        disabled: 'bg-gray-200 text-gray-500 cursor-not-allowed',
      },
      green: {
        active: 'bg-green-200 text-green-800 hover:bg-green-300',
        disabled: 'bg-gray-200 text-gray-500 cursor-not-allowed',
      },
    };

    const isDisabled = !isActive || isLoading;
    return variants[variant]?.[isDisabled ? 'disabled' : 'active'] || '';
  };

  const handleLoadUserId = useCallback(async () => {
    setIsLoading(true);
    try {
      const list = await reloadHfUsers();
      if (list && list.length > 0) {
        toast.success('Benutzer-ID-Liste erfolgreich geladen!');
      } else {
        toast.error('Keine Benutzer-ID gefunden. Bitte HuggingFace-Token in EduBotics setzen.');
      }
    } finally {
      setIsLoading(false);
    }
  }, [reloadHfUsers]);

  // File browser handlers
  const handleHfLocalDirSelect = useCallback((item) => {
    setHfLocalDirUpload(item.full_path);
  }, []);

  // Input handlers with validation
  const handleUploadRepoIdChange = (value) => {
    dispatch(setHFRepoIdUpload(value));
    const validation = validateHfRepoName(value.trim());
    setUploadRepoValidation(validation);
  };

  const handleDownloadRepoIdChange = (value) => {
    let repo_id = '';

    if (value.includes('/')) {
      const head = value.split('/')[0];
      const tail = value.split('/')[1];

      if (hfUserList.includes(head)) {
        dispatch(setHFUserId(head));
        repo_id = tail;
      } else {
        // If the head is not in userIdList, treat the whole value as repo_id
        repo_id = value;
      }
    } else {
      repo_id = value;
    }

    dispatch(setHFRepoIdDownload(repo_id));
    const validation = validateHfRepoName(repo_id.trim());
    setDownloadRepoValidation(validation);
  };

  // Operations
  const operations = {
    uploadDataset: async () => {
      if (!hfRepoIdUpload || hfRepoIdUpload.trim() === '') {
        toast.error('Bitte zuerst eine Repo-ID eingeben.');
        return;
      }

      if (!hfLocalDirUpload || hfLocalDirUpload.trim() === '') {
        toast.error('Bitte zuerst einen lokalen Ordner auswählen.');
        return;
      }

      // Additional validation check
      const validation = validateHfRepoName(hfRepoIdUpload.trim());
      if (!validation.isValid) {
        toast.error(`Ungültiger Repository-Name: ${validation.message}`);
        return;
      }

      setIsUploading(true);
      try {
        const repoId = userId + '/' + hfRepoIdUpload.trim();
        const localDir = hfLocalDirUpload.trim();
        const result = await controlHfServer('upload', repoId, hfDataType.toLowerCase(), localDir);
        console.log('Upload dataset result:', result);
        toast.success(`Upload gestartet! (${repoId})`);
      } catch (error) {
        console.error('Error uploading dataset:', error);
        toast.error(`Upload fehlgeschlagen: ${error.message}`);
      } finally {
        setIsUploading(false);
      }
    },

    downloadDataset: async () => {
      if (!hfRepoIdDownload || hfRepoIdDownload.trim() === '') {
        toast.error('Bitte zuerst eine Repo-ID eingeben.');
        return;
      }

      // Additional validation check
      const validation = validateHfRepoName(hfRepoIdDownload.trim());
      if (!validation.isValid) {
        toast.error(`Ungültiger Repository-Name: ${validation.message}`);
        return;
      }

      setIsDownloading(true);
      try {
        const repoId = userId + '/' + hfRepoIdDownload.trim();
        const result = await controlHfServer('download', repoId, hfDataType.toLowerCase());
        console.log('Download dataset result:', result);

        toast.success(`Download gestartet!\n(${repoId})`);
      } catch (error) {
        console.error('Error downloading dataset:', error);
        toast.error(`Download fehlgeschlagen: ${error.message}`);
      } finally {
        setIsDownloading(false);
      }
    },
    cancelOperation: async () => {
      try {
        const result = await controlHfServer('cancel', hfRepoIdDownload, hfDataType.toLowerCase());
        console.log('Cancel download result:', result);
        toast.success(`Wird abgebrochen … (${hfRepoIdDownload})`);
      } catch (error) {
        console.error('Error canceling download:', error);
        toast.error(`Abbrechen fehlgeschlagen: ${error.message}`);
      }
    },
  };

  // Load the Benutzer-ID list on mount if not already cached.
  useEffect(() => {
    if (hfUserList.length === 0) {
      reloadHfUsers();
    }
  }, [hfUserList.length, reloadHfUsers]);

  // track hf status update
  useEffect(() => {
    if (hfStatus === HFStatus.UPLOADING) {
      setActiveSection(SECTION_NAME.UPLOAD);
      setIsUploading(true);
    } else if (hfStatus === HFStatus.DOWNLOADING) {
      setActiveSection(SECTION_NAME.DOWNLOAD);
      setIsDownloading(true);
    } else {
      setIsUploading(false);
      setIsDownloading(false);
    }
  }, [hfStatus]);

  return (
    <div className="w-full flex flex-col items-start justify-start bg-gray-100 p-10 gap-4 rounded-xl">
      <div className="w-full flex items-center justify-start">
        <span className="text-2xl font-bold mb-4">Hugging Face: hochladen und herunterladen</span>
      </div>

      <div className="w-full flex flex-row items-start justify-start gap-4">
        <div className="flex flex-col items-center justify-start gap-12">
          {/* User ID Selection */}
          <div className="bg-white p-5 rounded-md flex flex-col items-start justify-center gap-4 shadow-md">
            <div className="w-full flex items-center justify-start">
              <span className="text-lg font-bold">Benutzer-ID</span>
            </div>
            <div
              className={clsx('w-full flex flex-row gap-3', {
                'opacity-50': isDownloading || isUploading,
              })}
            >
              <select
                className={STYLES.selectUserID}
                value={userId || ''}
                onChange={(e) => dispatch(setHFUserId(e.target.value))}
                disabled={isDownloading || isUploading}
              >
                <option value="">Benutzer-ID wählen</option>
                {hfUserList.map((userId) => (
                  <option key={userId} value={userId}>
                    {userId}
                  </option>
                ))}
              </select>
              <div className="flex gap-2">
                <button
                  className={clsx(STYLES.loadUserButton, getButtonVariant('blue', true, isLoading))}
                  onClick={() => {
                    if (!isLoading) {
                      handleLoadUserId();
                    }
                  }}
                  disabled={isLoading}
                >
                  {isLoading ? 'Wird geladen …' : 'Laden'}
                </button>
              </div>
            </div>
            {/* Data Type Selection */}
            <div className="w-full flex items-center justify-start">
              <span className="text-lg font-bold">Art der Daten</span>
            </div>
            <div className="w-full flex flex-row items-center justify-start">
              <div className="flex items-center bg-gray-200 rounded-lg p-1">
                <button
                  className={`px-6 py-2 rounded-md text-sm font-medium transition-colors ${
                    hfDataType === 'dataset'
                      ? 'bg-blue-500 text-white'
                      : 'text-gray-600 hover:text-gray-800'
                  } ${!canChangeDataType ? 'cursor-not-allowed' : 'cursor-pointer'}`}
                  onClick={() => dispatch(setHFDataType('dataset'))}
                >
                  Datensatz
                </button>
                <button
                  className={`px-6 py-2 rounded-md text-sm font-medium transition-colors ${
                    hfDataType === 'model'
                      ? 'bg-blue-500 text-white'
                      : 'text-gray-600 hover:text-gray-800'
                  } ${!canChangeDataType ? 'cursor-not-allowed' : 'cursor-pointer'}`}
                  onClick={() => dispatch(setHFDataType('model'))}
                >
                  Modell
                </button>
              </div>
            </div>
          </div>

          {/* Section Selector */}
          <div className="flex items-center justify-start">
            <SectionSelector
              activeSection={activeSection}
              onSectionChange={setActiveSection}
              canChangeSection={canChangeSection}
            />
          </div>
        </div>

        {/* Active Section Content */}
        <div className="w-full">
          {activeSection === SECTION_NAME.UPLOAD && (
            <div className="w-full bg-white p-5 rounded-md flex flex-col items-start justify-center gap-2 shadow-md">
              {/* Upload Dataset Section Header */}
              <div className="w-full flex flex-col items-start justify-start gap-2 bg-gray-50 border border-gray-200 p-3 rounded-md">
                <div className="w-full flex items-center rounded-md font-medium gap-2">
                  <Icon name="upload" className="text-lg text-green-600" />
                  {hfDataType === 'model' ? 'Modell hochladen' : 'Datensatz hochladen'}
                </div>
                <div className="text-sm text-gray-600">
                  <div className="mb-1">
                    {hfDataType === 'model'
                      ? 'Lädt ein Modell aus einem Ordner auf diesem Rechner zu Hugging Face hoch.'
                      : 'Lädt einen Datensatz aus einem Ordner auf diesem Rechner zu Hugging Face hoch.'}
                  </div>
                </div>
              </div>

              {/* Upload Dataset Section Content */}
              <div className="w-full flex flex-col gap-3">
                {/* Local Directory Input */}
                <div className="w-full flex flex-col gap-2">
                  <span className="text-lg font-bold">Ordner auf diesem Rechner</span>
                  <div className="w-full flex flex-row items-center justify-start gap-2">
                    <FolderBrowseButton
                      onClick={() =>
                        hfDataType === 'dataset'
                          ? setShowHfLocalDirBrowserModal(true)
                          : setShowHfLocalModelDirBrowserModal(true)
                      }
                      disabled={isDownloading}
                      ariaLabel="Ordner auswählen"
                    />
                    <input
                      className={clsx(STYLES.textInput, 'flex-1', {
                        'bg-gray-100 cursor-not-allowed': isDownloading,
                        'bg-white': !isDownloading,
                      })}
                      type="text"
                      placeholder="Ordnerpfad eingeben oder auswählen"
                      value={hfLocalDirUpload || ''}
                      onChange={(e) => setHfLocalDirUpload(e.target.value)}
                      disabled={isDownloading}
                    />
                  </div>
                </div>

                {/* Repo ID Input */}
                <div className="w-full flex flex-col gap-2">
                  <span className="text-lg font-bold">Repository-ID</span>
                  <div className="relative">
                    <div
                      className={clsx(
                        'flex items-center border rounded-md overflow-hidden bg-white focus-within:ring-2',
                        {
                          'border-gray-300 focus-within:ring-blue-500 focus-within:border-transparent':
                            uploadRepoValidation.isValid || !hfRepoIdUpload,
                          'border-red-300 focus-within:ring-red-500 focus-within:border-transparent':
                            !uploadRepoValidation.isValid && hfRepoIdUpload,
                        }
                      )}
                    >
                      <div className="px-3 py-2 bg-gray-50 border-r border-gray-300 text-gray-700 font-medium flex items-center">
                        <span className="text-sm">{userId || 'Benutzername'}</span>
                        <span className="mx-1 text-gray-400">/</span>
                      </div>
                      <input
                        className={clsx(
                          'flex-1 px-3 py-2 text-sm bg-transparent border-none outline-none',
                          {
                            'bg-gray-100 cursor-not-allowed text-gray-500': isUploading,
                            'text-gray-900': !isUploading,
                          }
                        )}
                        type="text"
                        placeholder="Repository-ID eingeben"
                        value={hfRepoIdUpload || ''}
                        onChange={(e) => handleUploadRepoIdChange(e.target.value)}
                        disabled={isUploading}
                      />
                    </div>
                    <div className="mt-1 text-xs">
                      <div className="text-gray-500">
                        Vollständiger Repository-Pfad:{' '}
                        <span className="font-mono text-blue-600">
                          {userId || ''}/{hfRepoIdUpload || ''}
                        </span>
                      </div>
                      {!uploadRepoValidation.isValid && hfRepoIdUpload && (
                        <div className="flex items-center gap-1 text-red-500 mt-1">
                          <Icon name="warning" />
                          <span>{uploadRepoValidation.message}</span>
                        </div>
                      )}
                    </div>
                  </div>
                </div>

                {/* Upload Button */}
                <div className="w-full flex flex-row items-center justify-start gap-3 mt-2">
                  <button
                    className={clsx(
                      'px-6',
                      'py-2',
                      'text-sm',
                      'font-medium',
                      'rounded-lg',
                      'transition-colors',
                      {
                        'bg-green-500 text-white hover:bg-green-600': uploadButtonEnabled,
                        'bg-gray-300 text-gray-500 cursor-not-allowed': !uploadButtonEnabled,
                      }
                    )}
                    onClick={operations.uploadDataset}
                    disabled={!uploadButtonEnabled}
                  >
                    <div className="flex items-center justify-center gap-2">
                      <Icon name="upload" className="w-6 h-6" />
                      Hochladen
                    </div>
                  </button>

                  {/* Cancel Button */}
                  <button
                    className={clsx(STYLES.cancelButton, {
                      'bg-red-500 text-white hover:bg-red-600': isUploading,
                      'bg-gray-300 text-gray-500 cursor-not-allowed': !isUploading,
                    })}
                    onClick={operations.cancelOperation}
                    disabled={!isUploading}
                  >
                    Abbrechen
                  </button>

                  {/* Status */}
                  <div className="flex flex-row items-center justify-start">
                    <span className="text-sm text-gray-500">
                      {isUploading && (
                        <span className="inline-flex items-center gap-1">
                          <Icon name="hourglass" />
                          Wird hochgeladen …
                        </span>
                      )}
                      {!isUploading && hfStatusLabelDe(hfStatus)}
                    </span>
                  </div>

                  {/* Upload Progress Bar */}
                  {isUploading && (
                    <div className="w-full">
                      <div className="flex flex-row items-center justify-between mb-1">
                        <span className="text-sm text-gray-500">
                          {uploadStatus.current}/{uploadStatus.total}
                        </span>
                        <span className="text-sm text-gray-500">{uploadStatus.percentage}%</span>
                      </div>
                      <div className="w-full bg-gray-200 rounded-full h-2">
                        <div
                          className="bg-blue-600 h-2 rounded-full transition-all duration-300 ease-out"
                          style={{ width: `${uploadStatus.percentage}%` }}
                        ></div>
                      </div>
                    </div>
                  )}
                </div>
              </div>
            </div>
          )}

          {activeSection === SECTION_NAME.DOWNLOAD && (
            <div className="w-full bg-white p-5 rounded-md flex flex-col items-start justify-center gap-4 shadow-md">
              {/* Download Dataset Section Header */}
              <div className="w-full flex flex-col items-start justify-start gap-2 bg-gray-50 border border-gray-200 p-3 rounded-md">
                <div className="w-full flex items-center rounded-md font-medium gap-2">
                  <Icon name="download" className="text-lg text-blue-600" />
                  {hfDataType === 'model' ? 'Modell herunterladen' : 'Datensatz herunterladen'}
                </div>
                <div className="text-sm text-gray-600">
                  <div className="mb-1">
                    {hfDataType === 'model'
                      ? 'Lädt ein Modell von Hugging Face auf diesen Rechner.'
                      : 'Lädt einen Datensatz von Hugging Face auf diesen Rechner.'}
                  </div>
                </div>
              </div>

              {/* Download Dataset Section Content */}
              <div className="w-full flex flex-col gap-3">
                {/* Repo ID Input */}
                <div className="w-full flex flex-col gap-2">
                  <span className="text-lg font-bold">Repository-ID</span>
                  <div className="relative">
                    <div
                      className={clsx(
                        'flex items-center border rounded-md overflow-hidden bg-white focus-within:ring-2',
                        {
                          'border-gray-300 focus-within:ring-blue-500 focus-within:border-transparent':
                            downloadRepoValidation.isValid || !hfRepoIdDownload,
                          'border-red-300 focus-within:ring-red-500 focus-within:border-transparent':
                            !downloadRepoValidation.isValid && hfRepoIdDownload,
                        }
                      )}
                    >
                      <div className="px-3 py-2 bg-gray-50 border-r border-gray-300 text-gray-700 font-medium flex items-center">
                        <span className="text-sm">{userId || 'Benutzername'}</span>
                        <span className="mx-1 text-gray-400">/</span>
                      </div>
                      <input
                        className={clsx(
                          'flex-1 px-3 py-2 text-sm bg-transparent border-none outline-none',
                          {
                            'bg-gray-100 cursor-not-allowed text-gray-500': isDownloading,
                            'text-gray-900': !isDownloading,
                          }
                        )}
                        type="text"
                        placeholder="Repository-ID eingeben"
                        value={hfRepoIdDownload || ''}
                        onChange={(e) => handleDownloadRepoIdChange(e.target.value)}
                        disabled={isDownloading}
                      />
                    </div>
                    <div className="mt-1 text-xs">
                      <div className="text-gray-500">
                        Vollständiger Repository-Pfad:{' '}
                        <span className="font-mono text-blue-600">
                          {userId || ''}/{hfRepoIdDownload || ''}
                        </span>
                      </div>
                      {!downloadRepoValidation.isValid && hfRepoIdDownload && (
                        <div className="flex items-center gap-1 text-red-500 mt-1">
                          <Icon name="warning" />
                          <span>{downloadRepoValidation.message}</span>
                        </div>
                      )}
                    </div>
                  </div>
                </div>

                {/* Info message: Dataset save path with folder icon */}
                <div className="w-full flex flex-row items-center mt-1">
                  <span className="text-xs text-gray-600 flex items-center gap-1">
                    {/* The dataset will be saved in the following directory */}
                    <Icon name="folderOpen" className="inline-block w-4 h-4 text-blue-700 mr-1" />
                    {hfDataType === 'model' ? 'Das Modell wird gespeichert in' : 'Der Datensatz wird gespeichert in'}{' '}
                    <span className="font-mono text-blue-700">
                      {hfDataType === 'dataset'
                        ? DEFAULT_PATHS.DATASET_PATH
                        : DEFAULT_PATHS.POLICY_MODEL_PATH}
                    </span>
                  </span>
                </div>

                {/* Download Button */}
                <div className="w-full flex flex-row items-center justify-start gap-3 mt-2">
                  <button
                    className={clsx(
                      'px-6',
                      'py-2',
                      'text-sm',
                      'font-medium',
                      'rounded-lg',
                      'transition-colors',
                      {
                        'bg-blue-500 text-white hover:bg-blue-600': downloadButtonEnabled,
                        'bg-gray-300 text-gray-500 cursor-not-allowed': !downloadButtonEnabled,
                      }
                    )}
                    onClick={operations.downloadDataset}
                    disabled={!downloadButtonEnabled}
                  >
                    <div className="flex items-center justify-center gap-2">
                      <Icon name="download" className="w-6 h-6" />
                      Herunterladen
                    </div>
                  </button>

                  {/* Cancel Button */}
                  <button
                    className={clsx(STYLES.cancelButton, {
                      'bg-red-500 text-white hover:bg-red-600': isDownloading,
                      'bg-gray-300 text-gray-500 cursor-not-allowed': !isDownloading,
                    })}
                    onClick={operations.cancelOperation}
                    disabled={!isDownloading}
                  >
                    Abbrechen
                  </button>

                  {/* Status */}
                  <div className="flex flex-row items-center justify-start gap-2">
                    <span className="text-sm text-gray-500">
                      {isDownloading && (
                        <span className="inline-flex items-center gap-1">
                          <Icon name="hourglass" />
                          Wird heruntergeladen …
                        </span>
                      )}
                      {!isDownloading && hfStatusLabelDe(hfStatus)}
                    </span>
                    {/* Spinner for model downloads - right next to status text */}
                    {isDownloading && hfDataType.toLowerCase() === 'model' && (
                      <div className="animate-spin rounded-full h-4 w-4 border-b-2 border-blue-600"></div>
                    )}
                  </div>

                  {/* Download Progress Bar - Only for datasets */}
                  {isDownloading && hfDataType.toLowerCase() === 'dataset' && (
                    <div className="w-full">
                      <div className="flex flex-row items-center justify-between mb-1">
                        <span className="text-sm text-gray-500">
                          {downloadStatus.current}/{downloadStatus.total}
                        </span>
                        <span className="text-sm text-gray-500">{downloadStatus.percentage}%</span>
                      </div>
                      <div className="w-full bg-gray-200 rounded-full h-2">
                        <div
                          className="bg-blue-600 h-2 rounded-full transition-all duration-300 ease-out"
                          style={{ width: `${downloadStatus.percentage}%` }}
                        ></div>
                      </div>
                    </div>
                  )}
                </div>
              </div>
            </div>
          )}
        </div>
      </div>

      {/* File Browser Modals for Dataset*/}
      <FileBrowserModal
        isOpen={showHfLocalDirBrowserModal}
        onClose={() => setShowHfLocalDirBrowserModal(false)}
        onFileSelect={handleHfLocalDirSelect}
        title="Ordner zum Hochladen auswählen"
        selectButtonText="Auswählen"
        allowDirectorySelect={true}
        targetFolderName={[
          TARGET_FOLDERS.DATASET_METADATA,
          TARGET_FOLDERS.DATASET_VIDEO,
          TARGET_FOLDERS.DATASET_DATA,
        ]}
        targetFileLabel="Datensatz-Ordner gefunden"
        initialPath={DEFAULT_PATHS.DATASET_PATH}
        defaultPath={DEFAULT_PATHS.DATASET_PATH}
        homePath=""
      />

      {/* File Browser Modals for Model*/}
      <FileBrowserModal
        isOpen={showHfLocalModelDirBrowserModal}
        onClose={() => setShowHfLocalModelDirBrowserModal(false)}
        onFileSelect={handleHfLocalDirSelect}
        title="Ordner zum Hochladen auswählen"
        selectButtonText="Auswählen"
        allowDirectorySelect={true}
        targetFileName={[TARGET_FILES.POLICY_MODEL]}
        targetFileLabel="Modelldatei gefunden"
        initialPath={DEFAULT_PATHS.POLICY_MODEL_PATH}
        defaultPath={DEFAULT_PATHS.POLICY_MODEL_PATH}
        homePath=""
      />

    </div>
  );
};

export default HuggingfaceSection;
