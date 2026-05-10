/*
 * Copyright 2025 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

import * as Blockly from 'blockly/core';

// Lazy-load the colour-blind theme plugins so the main bundle stays
// lean. They expose a default Theme instance per package.

export const THEME_KEYS = Object.freeze({
  STANDARD: 'standard',
  TRITANOPIA: 'tritanopia',
  DEUTERANOPIA: 'deuteranopia',
  HIGHCONTRAST: 'highcontrast',
});

const themeCache = new Map();

async function loadTheme(name) {
  if (themeCache.has(name)) return themeCache.get(name);
  let theme;
  switch (name) {
    case THEME_KEYS.TRITANOPIA: {
      try {
        const mod = await import('@blockly/theme-tritanopia');
        theme = mod.default || mod.Tritanopia || null;
      } catch (e) {
        console.warn('themes: tritanopia plugin not available', e);
        theme = null;
      }
      break;
    }
    case THEME_KEYS.DEUTERANOPIA: {
      try {
        const mod = await import('@blockly/theme-deuteranopia');
        theme = mod.default || mod.Deuteranopia || null;
      } catch (e) {
        console.warn('themes: deuteranopia plugin not available', e);
        theme = null;
      }
      break;
    }
    case THEME_KEYS.HIGHCONTRAST: {
      try {
        const mod = await import('@blockly/theme-highcontrast');
        theme = mod.default || mod.HighContrast || null;
      } catch (e) {
        console.warn('themes: highcontrast plugin not available', e);
        theme = null;
      }
      break;
    }
    case THEME_KEYS.STANDARD:
    default:
      // Blockly v12 ships `Themes.Classic` and `Themes.Modern`. The
      // codebase's default look is Classic.
      theme = (Blockly.Themes && Blockly.Themes.Classic) || null;
      break;
  }
  themeCache.set(name, theme);
  return theme;
}

/**
 * Apply a theme to a Blockly workspace by name. No-op if the workspace
 * is null or the theme module fails to load.
 */
export async function applyTheme(workspace, name) {
  if (!workspace || typeof workspace.setTheme !== 'function') return;
  const theme = await loadTheme(name || THEME_KEYS.STANDARD);
  if (theme) {
    try {
      workspace.setTheme(theme);
    } catch (e) {
      console.warn('themes: setTheme failed', e);
    }
  }
}
