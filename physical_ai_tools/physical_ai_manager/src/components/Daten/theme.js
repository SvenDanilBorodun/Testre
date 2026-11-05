// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.

// The leader arm's colour in the player (spec §F5, §F7, G-17): the legend's
// dashed violet, the charts' leader line AND the 3D ghost. ONE constant — the
// page sets it as the `--leader` CSS variable and hands the same value to
// UrdfTwin's poseSource ghost — so the legend and the twin cannot disagree.
export const LEADER_COLOR = '#7A6FE0';
