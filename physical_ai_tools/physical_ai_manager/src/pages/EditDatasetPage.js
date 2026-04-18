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
import { MdCloudUpload, MdMerge, MdDeleteSweep } from 'react-icons/md';
import HuggingfaceSection from '../features/editDataset/components/DatasetHuggingfaceSection';
import MergeSection from '../features/editDataset/components/DatasetMergeSection';
import DeleteSection from '../features/editDataset/components/DatasetDeleteSection';
import { SectionHeader } from '../components/EbUI';

const TOAST_LIMIT = 3;

const SECTION_TYPES = {
  HUGGINGFACE: 'huggingface',
  MERGE: 'merge',
  DELETE: 'delete',
};

const SECTION_CONFIG = {
  [SECTION_TYPES.HUGGINGFACE]: {
    label: 'Hoch- & herunterladen',
    icon: MdCloudUpload,
    description: 'Hugging Face',
  },
  [SECTION_TYPES.MERGE]: {
    label: 'Zusammenführen',
    icon: MdMerge,
    description: 'Mehrere Datensätze kombinieren',
  },
  [SECTION_TYPES.DELETE]: {
    label: 'Episoden löschen',
    icon: MdDeleteSweep,
    description: 'Einzelne Episoden entfernen',
  },
};

const manageTostLimit = (toasts) => {
  toasts
    .filter((t) => t.visible)
    .filter((_, i) => i >= TOAST_LIMIT)
    .forEach((t) => toast.dismiss(t.id));
};

export default function EditDatasetPage() {
  const { toasts } = useToasterStore();
  const [isEditable] = useState(true);
  const [activeSection, setActiveSection] = useState(SECTION_TYPES.HUGGINGFACE);

  useEffect(() => {
    manageTostLimit(toasts);
  }, [toasts]);

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
    <div className="h-full w-full overflow-y-auto" style={{ background: 'var(--bg)' }}>
      <div className="eb-shell flex flex-col gap-5 md:gap-6">
        <SectionHeader
          eyebrow="Daten"
          title="Datenwerkzeuge"
          description="Datensätze verwalten, zusammenführen, hochladen."
        />

        {/* Tool switcher (segmented) */}
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
          {Object.entries(SECTION_CONFIG).map(([sectionType, config]) => {
            const IconComponent = config.icon;
            const isActive = activeSection === sectionType;
            return (
              <button
                key={sectionType}
                onClick={() => setActiveSection(sectionType)}
                className={clsx(
                  'p-5 text-left rounded-[var(--radius-lg)] border transition',
                  isActive
                    ? 'bg-[var(--accent-wash)] border-[var(--accent)] ring-1 ring-[color:var(--accent-wash)]'
                    : 'bg-white border-[var(--line)] hover:border-[var(--ink-4)]'
                )}
              >
                <div
                  className={clsx(
                    'w-10 h-10 rounded-[10px] flex items-center justify-center mb-3',
                    isActive
                      ? 'bg-white text-[var(--accent)]'
                      : 'bg-[var(--bg-sunk)] text-[var(--ink-3)]'
                  )}
                >
                  <IconComponent size={22} />
                </div>
                <div
                  className={clsx(
                    'font-semibold text-[15px]',
                    isActive ? 'text-[var(--accent-ink)]' : 'text-[var(--ink)]'
                  )}
                >
                  {config.label}
                </div>
                <div className="text-[12px] text-[var(--ink-3)] mt-0.5">
                  {config.description}
                </div>
              </button>
            );
          })}
        </div>

        {/* Selected tool gets full width */}
        <div>{renderActiveSection()}</div>
      </div>
    </div>
  );
}
