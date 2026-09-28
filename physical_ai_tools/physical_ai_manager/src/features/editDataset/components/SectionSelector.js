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
import toast from 'react-hot-toast';
import Icon from '../../../components/icons/Icon';

const SectionSelector = ({
  activeSection,
  onSectionChange,
  canChangeSection = true,
  className = '',
}) => {
  const handleSectionClick = (section) => {
    if (canChangeSection) {
      onSectionChange(section);
    } else if (!canChangeSection && activeSection !== section) {
      toast.error('Während eines Hoch- oder Herunterladens kannst du nicht wechseln.');
    }
  };

  return (
    <div className={clsx('w-full flex justify-center', className)}>
      <div className="flex bg-gray-200 rounded-lg p-1">
        <button
          className={clsx('px-4 py-2 text-sm font-medium rounded-md transition-all duration-200', {
            'bg-white text-blue-600 shadow-sm': activeSection === 'upload',
            'text-gray-600 hover:text-gray-800': activeSection !== 'upload',
            'cursor-not-allowed opacity-50': !canChangeSection && activeSection !== 'upload',
          })}
          onClick={() => handleSectionClick('upload')}
          disabled={!canChangeSection && activeSection !== 'upload'}
          aria-label="Zum Hochladen wechseln"
        >
          <div className="flex items-center gap-2">
            <Icon name="upload" className="w-4 h-4" />
            Hochladen
          </div>
        </button>
        <button
          className={clsx('px-4 py-2 text-sm font-medium rounded-md transition-all duration-200', {
            'bg-white text-blue-600 shadow-sm': activeSection === 'download',
            'text-gray-600 hover:text-gray-800': activeSection !== 'download',
            'cursor-not-allowed opacity-50': !canChangeSection && activeSection !== 'download',
          })}
          onClick={() => handleSectionClick('download')}
          disabled={!canChangeSection && activeSection !== 'download'}
          aria-label="Zum Herunterladen wechseln"
        >
          <div className="flex items-center gap-2">
            <Icon name="download" className="w-4 h-4" />
            Herunterladen
          </div>
        </button>
      </div>
    </div>
  );
};

export default SectionSelector;
