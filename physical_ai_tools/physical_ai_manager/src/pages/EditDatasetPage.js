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

import React, { useEffect, useState } from 'react';
import clsx from 'clsx';
import toast, { useToasterStore } from 'react-hot-toast';
import {
  MdWidgets,
  MdCloudUpload,
  MdMerge,
  MdDeleteSweep,
  MdOutlineTouchApp,
} from 'react-icons/md';
import HuggingfaceSection from '../features/editDataset/components/DatasetHuggingfaceSection';
import MergeSection from '../features/editDataset/components/DatasetMergeSection';
import DeleteSection from '../features/editDataset/components/DatasetDeleteSection';

// Constants
const TOAST_LIMIT = 3;

const SECTION_TYPES = {
  HUGGINGFACE: 'huggingface',
  MERGE: 'merge',
  DELETE: 'delete',
};

const SECTION_CONFIG = {
  [SECTION_TYPES.HUGGINGFACE]: {
    label: 'Daten hoch- & herunterladen',
    icon: MdCloudUpload,
    description: 'Hugging Face',
  },
  [SECTION_TYPES.MERGE]: {
    label: 'Datens\u00e4tze zusammenf\u00fchren',
    icon: MdMerge,
    description: 'Mehrere Datens\u00e4tze kombinieren',
  },
  [SECTION_TYPES.DELETE]: {
    label: 'Episoden l\u00f6schen',
    icon: MdDeleteSweep,
    description: 'Bestimmte Episoden aus dem Datensatz entfernen',
  },
};

// Style Classes
const STYLES = {
  container: clsx(
    'w-full',
    'h-full',
    'flex',
    'flex-col',
    'items-start',
    'justify-start',
    'overflow-scroll'
  ),
};

// Utility Functions
const manageTostLimit = (toasts) => {
  toasts
    .filter((t) => t.visible) // Only consider visible toasts
    .filter((_, i) => i >= TOAST_LIMIT) // Is toast index over limit?
    .forEach((t) => toast.dismiss(t.id)); // Dismiss
};

export default function EditDatasetPage() {
  // Hooks and state management
  const { toasts } = useToasterStore();

  // Local state
  const [isEditable] = useState(true);
  const [activeSection, setActiveSection] = useState(SECTION_TYPES.HUGGINGFACE);

  // Effects
  useEffect(() => {
    manageTostLimit(toasts);
  }, [toasts]);

  // Section selector component
  const renderSectionSelector = () => (
    <div className="w-full bg-white rounded-xl shadow-sm border border-gray-200 p-3 mb-2">
      <div className="flex items-center text-sm text-gray-500 mb-2">
        <span className="mr-2">
          <MdOutlineTouchApp className="inline-block w-5 h-5 text-gray-400" />
        </span>
        Was m\u00f6chtest du tun?
      </div>
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        {Object.entries(SECTION_CONFIG).map(([sectionType, config]) => {
          const IconComponent = config.icon;
          const isActive = activeSection === sectionType;

          return (
            <button
              key={sectionType}
              onClick={() => setActiveSection(sectionType)}
              className={clsx(
                'flex flex-col items-center justify-center p-4 rounded-lg border transition-all duration-200',
                'hover:shadow-sm focus:outline-none focus:ring-1 focus:ring-teal-500 focus:ring-opacity-30',
                {
                  'border-teal-300 bg-teal-50/50 shadow-sm': isActive,
                  'border-gray-200 bg-white hover:border-gray-300 hover:shadow-sm': !isActive,
                }
              )}
            >
              <IconComponent
                className={clsx('w-10 h-10 mb-2', {
                  'text-teal-500': isActive,
                  'text-gray-400': !isActive,
                })}
              />
              <h3
                className={clsx('text-base font-medium mb-1', {
                  'text-teal-700': isActive,
                  'text-gray-600': !isActive,
                })}
              >
                {config.label}
              </h3>
              <p
                className={clsx('text-xs text-center', {
                  'text-teal-600': isActive,
                  'text-gray-500': !isActive,
                })}
              >
                {config.description}
              </p>
            </button>
          );
        })}
      </div>
    </div>
  );

  // Render active section
  const renderActiveSection = () => {
    switch (activeSection) {
      case SECTION_TYPES.HUGGINGFACE:
        return <HuggingfaceSection isEditable={isEditable} />;
      case SECTION_TYPES.MERGE:
        return <MergeSection isEditable={isEditable} />;
      case SECTION_TYPES.DELETE:
        return <DeleteSection isEditable={isEditable} />;
      default:
        return <HuggingfaceSection isEditable={isEditable} />;
    }
  };

  return (
    <div className={STYLES.container}>
      <div className="w-full flex flex-col items-start justify-start p-10 gap-6">
        <h1 className="text-4xl font-bold flex flex-row items-center justify-start gap-2">
          <MdWidgets className="w-10 h-10" />
          Datenwerkzeuge
        </h1>

        {renderSectionSelector()}
        {renderActiveSection()}
      </div>
    </div>
  );
}
