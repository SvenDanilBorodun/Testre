/*
 * Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
 *
 * Proprietary and confidential. Licensed under the EduBotics End-User
 * Licence Agreement; see LICENSE at the repository root.
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
