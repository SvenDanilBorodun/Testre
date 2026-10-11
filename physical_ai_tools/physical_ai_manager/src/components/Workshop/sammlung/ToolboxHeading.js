/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

/**
 * A toolbox group heading („Meine Sammlung", „Bausteine"): a toolbox item of
 * kind `heading` that draws a small caption between categories.
 *
 * A category toolbox has no label item, so the heading is a ToolboxSeparator
 * subclass: like a separator it is never selectable and keyboard navigation
 * skips it. Its container is `aria-hidden`, because the toolbox is an ARIA tree
 * whose children must be tree items; the category names stay self-describing.
 *
 * The class is built INSIDE registerToolboxHeading(), never at import: nine
 * page tests mock `blockly/core` with a few members only, and a top-level
 * `extends Blockly.ToolboxSeparator` would take every one of them down.
 * Registered from BlocklyWorkspace::registerAllBlocksOnce, before the first
 * inject. Blockly skips an unregistered item kind with a console warning, so a
 * test that injects `buildToolbox()` itself registers the heading first.
 *
 * Styles: `.eduToolboxHeading` / `.eduToolboxHeadingLabel` in src/index.css
 * (never Blockly.Css.register, which throws once a workspace exists).
 */

import * as Blockly from 'blockly/core';

/** The toolbox JSON item kind (blocks/toolbox.js spells the same string). */
export const TOOLBOX_HEADING_KIND = 'heading';

/** Register the `heading` toolbox item. Idempotent. */
export function registerToolboxHeading() {
  const { registry } = Blockly;
  if (registry.hasItem(registry.Type.TOOLBOX_ITEM, TOOLBOX_HEADING_KIND)) return;

  class ToolboxHeading extends Blockly.ToolboxSeparator {
    constructor(def, toolbox) {
      super(def, toolbox);
      this.text_ = String((def && def.text) || '');
      this.cssConfig_ = { container: 'eduToolboxHeading' };
    }

    createDom_() {
      const container = super.createDom_();
      const label = document.createElement('span');
      label.className = 'eduToolboxHeadingLabel';
      label.textContent = this.text_;
      container.appendChild(label);
      container.setAttribute('aria-hidden', 'true');
      return container;
    }
  }

  registry.register(registry.Type.TOOLBOX_ITEM, TOOLBOX_HEADING_KIND, ToolboxHeading);
}
