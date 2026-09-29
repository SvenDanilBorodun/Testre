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

// HFStatus enum-like object for HF status
// Use this for better code readability and maintainability

const HFStatus = {
  IDLE: 'Idle',
  UPLOADING: 'Uploading',
  DOWNLOADING: 'Downloading',
  DELETING: 'Deleting',
  FETCHING: 'Fetching',
  PROCESSING: 'Processing',
  SUCCESS: 'Success',
  FAILED: 'Failed',
};

// What a student reads for each status (the values above are the wire values
// the server sends and the code compares; they stay English).
const HF_STATUS_DE = Object.freeze({
  [HFStatus.IDLE]: 'Bereit',
  [HFStatus.UPLOADING]: 'Wird hochgeladen',
  [HFStatus.DOWNLOADING]: 'Wird heruntergeladen',
  [HFStatus.DELETING]: 'Wird gelöscht',
  [HFStatus.FETCHING]: 'Wird abgerufen',
  [HFStatus.PROCESSING]: 'Wird verarbeitet',
  [HFStatus.SUCCESS]: 'Erfolgreich',
  [HFStatus.FAILED]: 'Fehlgeschlagen',
});

/** The German label of an HF status; an unknown status is shown as it came. */
export function hfStatusLabelDe(status) {
  return Object.prototype.hasOwnProperty.call(HF_STATUS_DE, status) ? HF_STATUS_DE[status] : status;
}

export default HFStatus;
