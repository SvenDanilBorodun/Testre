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

// The Daten tab (Daten 2.0, spec §G1): a thin composition of the new page
// (components/Daten/DatenPage), which also says what cloud mode means when the
// tab is reached there. The toast cap of three stays here, as before.

import React, { useEffect } from 'react';
import toast, { useToasterStore } from 'react-hot-toast';
import DatenPage from '../components/Daten/DatenPage';

const TOAST_LIMIT = 3;

const manageTostLimit = (toasts) => {
  toasts
    .filter((t) => t.visible)
    .filter((_, i) => i >= TOAST_LIMIT)
    .forEach((t) => toast.dismiss(t.id));
};

export default function EditDatasetPage() {
  const { toasts } = useToasterStore();

  useEffect(() => {
    manageTostLimit(toasts);
  }, [toasts]);

  return <DatenPage />;
}
