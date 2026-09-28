/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// THE icon table: one semantic name per meaning, one Lucide component each
// (owner decision D7: Lucide everywhere, plus four custom Lucide-style icons in
// custom.js). This is the ONLY module that imports `react-icons/lu`, and no
// module in src/ may import any other react-icons set
// (components/icons/__tests__/noIconGlyphs.test.js fences both). Named imports
// only: `react-icons` is `sideEffects: false`, so the bundle carries exactly the
// icons listed here.
//
// A name says what the icon MEANS on screen, never what it looks like, so a
// later redesign changes one line here and no call site.

import {
  LuAlarmClock,
  LuArrowRightLeft,
  LuArrowUp,
  LuBlocks,
  LuBookmark,
  LuBox,
  LuBug,
  LuCamera,
  LuChartLine,
  LuCheck,
  LuChevronDown,
  LuChevronLeft,
  LuChevronRight,
  LuChevronUp,
  LuChevronsLeft,
  LuChevronsRight,
  LuCircle,
  LuCircleAlert,
  LuCircleCheck,
  LuCircleDot,
  LuCircleX,
  LuClipboardList,
  LuCloud,
  LuCloudUpload,
  LuCoffee,
  LuConstruction,
  LuCopy,
  LuCornerDownLeft,
  LuCpu,
  LuDatabase,
  LuDownload,
  LuEllipsis,
  LuExternalLink,
  LuFileDown,
  LuFileText,
  LuFileUp,
  LuFlame,
  LuFolder,
  LuFolderOpen,
  LuGamepad2,
  LuGraduationCap,
  LuHand,
  LuHistory,
  LuHourglass,
  LuHouse,
  LuInfo,
  LuKeyRound,
  LuLink,
  LuLoaderCircle,
  LuLock,
  LuLockOpen,
  LuLogOut,
  LuMapPin,
  LuMaximize2,
  LuMenu,
  LuMerge,
  LuMinus,
  LuNotebookPen,
  LuPalette,
  LuPause,
  LuPencil,
  LuPiggyBank,
  LuPlay,
  LuPlus,
  LuPuzzle,
  LuRedo2,
  LuRefreshCw,
  LuRotateCcw,
  LuSave,
  LuSend,
  LuSettings,
  LuSkipForward,
  LuSlidersHorizontal,
  LuSparkles,
  LuSquare,
  LuStar,
  LuStepForward,
  LuTarget,
  LuTerminal,
  LuTrash2,
  LuTriangleAlert,
  LuUndo2,
  LuUnlink,
  LuUnplug,
  LuUpload,
  LuUsers,
  LuVideo,
  LuVolume2,
  LuVolumeX,
  LuX,
  LuZap,
} from 'react-icons/lu';
import { IconLeaderArm, IconPython, IconRobotArm } from './custom';

export const ICONS = Object.freeze({
  // Vormachen: the three kinds (owner decision D10) and the arm controls.
  record: LuCircleDot,
  pose: LuMapPin,
  ziel: LuTarget,
  hand: LuHand,
  lock: LuLock,
  lockOpen: LuLockOpen,
  robotArm: IconRobotArm,
  leaderArm: IconLeaderArm,

  // Running, stepping, reviewing. The media controls are SOLID (see
  // SOLID_ICON_NAMES below); `record` above is the Bewegung KIND, an outline,
  // while `liveRecording` is the filled dot of a recording in progress.
  play: LuPlay,
  pause: LuPause,
  step: LuStepForward,
  stop: LuSquare,
  skipForward: LuSkipForward,
  again: LuRotateCcw,
  check: LuCheck,
  checkCircle: LuCircleCheck,
  close: LuX,
  cancel: LuCircleX,
  failed: LuCircleAlert,
  warning: LuTriangleAlert,
  info: LuInfo,
  dot: LuCircle,
  liveRecording: LuCircle,
  loading: LuLoaderCircle,
  hourglass: LuHourglass,
  debug: LuBug,

  // Editing.
  undo: LuUndo2,
  redo: LuRedo2,
  fit: LuMaximize2,
  save: LuSave,
  fileExport: LuFileUp,
  fileImport: LuFileDown,
  fileText: LuFileText,
  palette: LuPalette,
  pencil: LuPencil,
  plus: LuPlus,
  minus: LuMinus,
  trash: LuTrash2,
  copy: LuCopy,
  more: LuEllipsis,
  chevronDown: LuChevronDown,
  chevronLeft: LuChevronLeft,
  chevronRight: LuChevronRight,
  chevronUp: LuChevronUp,
  chevronsLeft: LuChevronsLeft,
  chevronsRight: LuChevronsRight,
  arrowUp: LuArrowUp,
  enterKey: LuCornerDownLeft,
  externalLink: LuExternalLink,
  refresh: LuRefreshCw,
  history: LuHistory,
  send: LuSend,
  sparkles: LuSparkles,
  sliders: LuSlidersHorizontal,
  menu: LuMenu,

  // Program notations (NewProgramDialog).
  blocks: LuPuzzle,
  python: IconPython,
  java: LuCoffee,

  // Places and things.
  camera: LuCamera,
  video: LuVideo,
  gamepad: LuGamepad2,
  box: LuBox,
  graduationCap: LuGraduationCap,
  folder: LuFolder,
  folderOpen: LuFolderOpen,
  home: LuHouse,
  bookmark: LuBookmark,
  star: LuStar,
  database: LuDatabase,
  cloud: LuCloud,
  cloudUpload: LuCloudUpload,
  download: LuDownload,
  upload: LuUpload,
  mergeData: LuMerge,
  cpu: LuCpu,
  terminal: LuTerminal,
  settings: LuSettings,
  logout: LuLogOut,
  key: LuKeyRound,
  link: LuLink,
  unlink: LuUnlink,
  unplugged: LuUnplug,
  alarm: LuAlarmClock,
  flame: LuFlame,
  zap: LuZap,
  construction: LuConstruction,
  users: LuUsers,
  notebook: LuNotebookPen,
  piggyBank: LuPiggyBank,
  chart: LuChartLine,
  moveAcross: LuArrowRightLeft,
  task: LuClipboardList,
  widgets: LuBlocks,
  volumeOn: LuVolume2,
  volumeOff: LuVolumeX,
});

export const ICON_NAMES = Object.freeze(Object.keys(ICONS));

// Icons drawn FILLED wherever they appear: the media controls (a Stopp, Start
// or Pause reads as a solid glyph, as it did before the outline set) and the
// dots (a breakpoint, a status dot, the red dot of a recording in progress).
// <Icon> and appendSvgIcon fill them with the stroke colour, so the three run
// surfaces (ControlPanel, the run bar, Vormachen) can never disagree.
export const SOLID_ICON_NAMES = Object.freeze(['play', 'pause', 'step', 'stop', 'skipForward', 'dot', 'liveRecording']);

/** True when icon `name` is drawn filled (SOLID_ICON_NAMES). */
export function isSolidIcon(name) {
  return SOLID_ICON_NAMES.includes(name);
}

/** True when `name` is a registered icon. */
export function isIconName(name) {
  return typeof name === 'string' && Object.prototype.hasOwnProperty.call(ICONS, name);
}
