// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// The leader arm's colour in the player (spec §F5, §F7, G-17): the legend's
// dashed violet, the charts' leader line AND the 3D ghost. ONE constant — the
// page sets it as the `--leader` CSS variable and hands the same value to
// UrdfTwin's poseSource ghost — so the legend and the twin cannot disagree.
export const LEADER_COLOR = '#7A6FE0';
