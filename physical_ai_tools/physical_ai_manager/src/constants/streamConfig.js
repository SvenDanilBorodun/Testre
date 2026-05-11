/*
 * Copyright 2025 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// Audit F35: single source of truth for MJPEG stream quality.
// CameraFeedOverlay and ImageGridCell used to disagree (70 vs 50);
// pinning them here lets the teacher tune for school Wi-Fi.
export const STREAM_QUALITY = 70;
