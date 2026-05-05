/*
 * Copyright 2025 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

import React, { useEffect, useRef } from 'react';
import * as Blockly from 'blockly/core';
import 'blockly/blocks';
import * as De from 'blockly/msg/de';
import { TOOLBOX } from './blocks/toolbox';
import { registerMotionBlocks } from './blocks/motion';
import { registerPerceptionBlocks } from './blocks/perception';
import { registerDestinationBlocks } from './blocks/destinations';
import { registerOutputBlocks } from './blocks/output';

let blocksRegistered = false;
function registerAllBlocksOnce() {
  if (blocksRegistered) return;
  Blockly.setLocale(De);
  registerMotionBlocks();
  registerPerceptionBlocks();
  registerDestinationBlocks();
  registerOutputBlocks();
  blocksRegistered = true;
}

function BlocklyWorkspace({ initialJson, onChange, readOnly = false }) {
  const containerRef = useRef(null);
  const workspaceRef = useRef(null);

  useEffect(() => {
    registerAllBlocksOnce();
    if (!containerRef.current) return undefined;

    const workspace = Blockly.inject(containerRef.current, {
      toolbox: TOOLBOX,
      readOnly,
      trashcan: !readOnly,
      grid: { spacing: 20, length: 1, colour: '#e5e7eb', snap: true },
      zoom: { controls: true, wheel: true, startScale: 0.9, maxScale: 1.5, minScale: 0.5 },
      move: { scrollbars: true, drag: true, wheel: false },
    });
    workspaceRef.current = workspace;

    if (initialJson) {
      try {
        Blockly.serialization.workspaces.load(initialJson, workspace);
      } catch (e) {
        console.error('BlocklyWorkspace: failed to load initial JSON', e);
      }
    }

    let disposed = false;
    const handleChange = () => {
      if (disposed || !onChange) return;
      try {
        const json = Blockly.serialization.workspaces.save(workspace);
        onChange(json);
      } catch (e) {
        console.error('BlocklyWorkspace: failed to serialize', e);
      }
    };
    workspace.addChangeListener(handleChange);

    // React 19 StrictMode mounts each effect twice; without an explicit
    // dispose() Blockly leaks a workspace per mount and the SVG defs
    // accumulate. The 5x mount/unmount test in the verification gate
    // depends on this cleanup.
    return () => {
      disposed = true;
      workspace.removeChangeListener(handleChange);
      workspace.dispose();
      workspaceRef.current = null;
    };
  }, [initialJson, onChange, readOnly]);

  return <div ref={containerRef} className="w-full h-full" />;
}

export default BlocklyWorkspace;
