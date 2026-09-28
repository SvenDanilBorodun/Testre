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

import React from 'react';
import clsx from 'clsx';

import { useSelector } from 'react-redux';
import Icon from './icons/Icon';

const SystemStatus = ({
  label = 'System',
  type = 'storage', // 'cpu', 'ram', 'storage'
  className,
}) => {
  const cpuPercentage = useSelector((state) => state.tasks.taskStatus.usedCpu);
  const totalRamSize = useSelector((state) => state.tasks.taskStatus.totalRamSize);
  const usedRamSize = useSelector((state) => state.tasks.taskStatus.usedRamSize);
  const totalStorageSize = useSelector((state) => state.tasks.taskStatus.totalStorageSize);
  const usedStorageSize = useSelector((state) => state.tasks.taskStatus.usedStorageSize);

  const totalCapacity =
    type === 'ram' ? totalRamSize * 1024 * 1024 * 1024 : totalStorageSize * 1024 * 1024 * 1024;
  const usedCapacity =
    type === 'ram' ? usedRamSize * 1024 * 1024 * 1024 : usedStorageSize * 1024 * 1024 * 1024;

  // Calculate usage percentage
  let usagePercentage;
  let freeCapacity = 0;

  if (type === 'cpu') {
    usagePercentage = cpuPercentage || 0;
  } else {
    usagePercentage = totalCapacity > 0 ? (usedCapacity / totalCapacity) * 100 : 0;
    freeCapacity = totalCapacity - usedCapacity;
  }

  // Format bytes to human readable format
  const formatBytes = (bytes) => {
    if (bytes === 0) return '0 B';

    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));

    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
  };

  // Get color based on usage percentage
  const getUsageColor = (percentage) => {
    if (percentage >= 90) return 'bg-red-500';
    if (percentage >= 75) return 'bg-orange-500';
    if (percentage >= 50) return 'bg-yellow-500';
    return 'bg-green-500';
  };

  // Get text color based on usage percentage
  const getTextColor = (percentage) => {
    if (percentage >= 90) return 'text-red-600';
    if (percentage >= 75) return 'text-orange-600';
    if (percentage >= 50) return 'text-yellow-600';
    return 'text-green-600';
  };

  const containerClass = clsx(
    'bg-white rounded-2xl shadow-md p-4 border border-gray-200',
    className
  );

  const progressBarClass = clsx(
    'h-3 rounded-full transition-all duration-300 ease-in-out',
    getUsageColor(usagePercentage)
  );

  const usageTextClass = clsx('font-semibold text-lg', getTextColor(usagePercentage));

  // CPU Type - Simple percentage display
  if (type === 'cpu') {
    return (
      <div className={containerClass}>
        {/* Header */}
        <div className="flex items-center justify-center gap-2 mb-3">
          <h3 className="text-sm font-medium text-gray-700">{`${label}-Auslastung`}</h3>
        </div>

        {/* CPU Usage Display */}
        <div className="text-center">
          <div className={clsx('text-4xl font-bold mb-2', getTextColor(usagePercentage))}>
            {Math.round(usagePercentage)}%
          </div>
          <div className="text-sm text-gray-600">Aktuelle CPU-Auslastung</div>
        </div>

        {/* Warning Messages */}
        {usagePercentage >= 90 && (
          <div className="mt-3 p-2 bg-red-50 border border-red-200 rounded-md">
            <div className="flex items-center">
              <Icon name="warning" className="w-4 h-4 text-red-500 mr-2" />
              <span className="text-xs text-red-700 font-medium">Die CPU ist sehr stark ausgelastet!</span>
            </div>
          </div>
        )}

        {usagePercentage >= 75 && usagePercentage < 90 && (
          <div className="mt-3 p-2 bg-orange-50 border border-orange-200 rounded-md">
            <div className="flex items-center">
              <Icon name="info" className="w-4 h-4 text-orange-500 mr-2" />
              <span className="text-xs text-orange-700 font-medium">Die CPU ist stark ausgelastet.</span>
            </div>
          </div>
        )}
      </div>
    );
  }

  // RAM/Storage Type - Capacity display
  return (
    <div className={containerClass}>
      {/* Header */}
      <div className="flex items-center justify-between gap-2 mb-3">
        <h3 className="text-sm font-medium text-gray-700">{`${label}-Auslastung`}</h3>
        <div className="flex items-center space-x-1">
          <div className="w-2 h-2 rounded-full bg-gray-400"></div>
          <span
            className="text-xs text-gray-500"
            style={{ minWidth: '5ch', display: 'inline-block', textAlign: 'right' }}
          >
            {usagePercentage.toFixed(1)}%
          </span>
        </div>
      </div>

      {/* Progress Bar */}
      <div className="mb-3">
        <div className="w-full bg-gray-200 rounded-full h-3">
          <div
            className={progressBarClass}
            style={{ width: `${Math.min(usagePercentage, 100)}%` }}
          ></div>
        </div>
      </div>

      {/* Usage Statistics */}
      <div className="space-y-2">
        <div className="flex justify-between items-center">
          <span className="text-sm text-gray-600">Belegt:</span>
          <span className={usageTextClass}>{formatBytes(usedCapacity)}</span>
        </div>

        <div className="flex justify-between items-center">
          <span className="text-sm text-gray-600">Frei:</span>
          <span
            className="text-sm font-medium text-gray-700"
            style={{ minWidth: '6ch', display: 'inline-block', textAlign: 'right' }}
          >
            {formatBytes(freeCapacity)}
          </span>
        </div>

        <div className="flex justify-between items-center border-t pt-2">
          <span className="text-sm text-gray-600">Gesamt:</span>
          <span
            className="text-sm font-medium text-gray-900"
            style={{ minWidth: '6ch', display: 'inline-block', textAlign: 'right' }}
          >
            {formatBytes(totalCapacity)}
          </span>
        </div>
      </div>

      {/* Warning Message */}
      {usagePercentage >= 90 && (
        <div className="mt-3 p-2 bg-red-50 border border-red-200 rounded-md">
          <div className="flex items-center">
            <Icon name="warning" className="w-4 h-4 text-red-500 mr-2" />
            <span className="text-xs text-red-700 font-medium">
              {type === 'ram' ? 'Der Arbeitsspeicher ist fast voll!' : 'Der Speicher ist fast voll!'}
            </span>
          </div>
        </div>
      )}

      {usagePercentage >= 75 && usagePercentage < 90 && (
        <div className="mt-3 p-2 bg-orange-50 border border-orange-200 rounded-md">
          <div className="flex items-center">
            <Icon name="info" className="w-4 h-4 text-orange-500 mr-2" />
            <span className="text-xs text-orange-700 font-medium">
              {type === 'ram'
                ? 'Schließe am besten einige Programme.'
                : 'Räume am besten Speicherplatz frei.'}
            </span>
          </div>
        </div>
      )}
    </div>
  );
};

export default SystemStatus;
