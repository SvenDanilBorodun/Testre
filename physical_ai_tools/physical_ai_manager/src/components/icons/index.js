/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The app's one icon style (owner decision D7): Lucide plus three custom icons
// drawn in its rules. React UI uses <Icon>; Blockly and CodeMirror SVG use
// appendSvgIcon/iconNodes; toasts use toastIcon.

export { default as Icon } from './Icon';
export { ICONS, ICON_NAMES, isIconName } from './registry';
export {
  appendSvgIcon, iconMarkup, iconNodes, SVG_NS,
} from './svg';
export { toastIcon, TOAST_ICON_KINDS } from './toast';
