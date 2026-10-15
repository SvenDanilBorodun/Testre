/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The app's one icon style (owner decision D7): Lucide plus one custom icon
// drawn in its rules (the Python snake); robots are renders of their real URDFs
// (RobotGlyph). React UI uses <Icon>; Blockly and CodeMirror SVG use
// appendSvgIcon/iconNodes; toasts use toastIcon.

export { default as Icon } from './Icon';
export { ICONS, ICON_NAMES, isIconName } from './registry';
export {
  appendSvgIcon, iconMarkup, iconNodes, SVG_NS,
} from './svg';
export { toastIcon, TOAST_ICON_KINDS, ROBOT_TOAST_KIND } from './toast';
export { default as RobotGlyph } from './RobotGlyph';
export {
  robotGlyphName, robotGlyphUrl, phasePicture, PHASE_PICTURES,
} from './robotAssets';
