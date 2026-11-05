/*
 * Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
 *
 * Proprietary and confidential. Licensed under the EduBotics End-User
 * Licence Agreement; see LICENSE at the repository root.
 */

// Audit F35: single source of truth for MJPEG stream quality.
// CameraFeedOverlay and ImageGridCell used to disagree (70 vs 50);
// pinning them here lets the teacher tune for school Wi-Fi.
export const STREAM_QUALITY = 70;
