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

import React from 'react';
import { Toaster } from 'react-hot-toast';
import StudentApp from './StudentApp';
import WebApp from './WebApp';
import { APP_MODE } from './constants/appMode';

function App() {
  const inner = APP_MODE === 'web' ? <WebApp /> : <StudentApp />;
  return (
    <>
      {inner}
      <Toaster
        position="top-center"
        gutter={8}
        toastOptions={{
          duration: 3000,
          style: {
            background: '#363636',
            color: '#fff',
            maxWidth: '500px',
            wordWrap: 'break-word',
            whiteSpace: 'pre-wrap',
            lineHeight: '1.4',
          },
          success: {
            duration: 3000,
            style: {
              background: '#10b981',
              maxWidth: '500px',
              wordWrap: 'break-word',
              whiteSpace: 'pre-wrap',
              lineHeight: '1.4',
            },
          },
          error: {
            duration: 6000,
            style: {
              background: '#ef4444',
              maxWidth: '500px',
              wordWrap: 'break-word',
              whiteSpace: 'pre-wrap',
              lineHeight: '1.4',
            },
          },
        }}
      />
    </>
  );
}

export default App;
