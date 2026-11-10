// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.

// Blockly's media comes from our own origin (2026-11-11). Without the `media`
// option Blockly loads its sprites, cursors and sounds from
// https://blockly-demo.appspot.com/static/media/ — a Google host, contacted by
// every page with a canvas, and refused by the teacher web's CSP (`'self'`).

import fs from 'fs';
import path from 'path';

const APP = path.resolve(__dirname, '../../../..');
const SHIPPED = path.join(APP, 'public/blockly-media');
const PACKAGE = path.join(APP, 'node_modules/blockly/media');

describe('Blockly media is self-hosted', () => {
  it('public/blockly-media is a byte-identical copy of the installed blockly package media', () => {
    const shipped = fs.readdirSync(SHIPPED).sort();
    const packaged = fs.readdirSync(PACKAGE).sort();
    // A Blockly bump that adds, renames or changes a media file fails here:
    // copy node_modules/blockly/media/* over public/blockly-media/ again.
    expect(shipped).toEqual(packaged);
    for (const name of packaged) {
      expect(fs.readFileSync(path.join(SHIPPED, name)).equals(fs.readFileSync(path.join(PACKAGE, name)))).toBe(true);
    }
    expect(packaged).toEqual(expect.arrayContaining(['sprites.png', 'click.mp3', 'handopen.cur']));
  });

  it('the one inject passes media: BLOCKLY_MEDIA_PATH = "/blockly-media/"', () => {
    const src = fs.readFileSync(path.join(APP, 'src/components/Workshop/BlocklyWorkspace.jsx'), 'utf8');
    expect(src).toMatch(/export const BLOCKLY_MEDIA_PATH = '\/blockly-media\/';/);
    const injects = [...src.matchAll(/Blockly\.inject\(container, \{\s*\n\s*media: BLOCKLY_MEDIA_PATH,/g)];
    expect(injects).toHaveLength(1);
    expect(src.match(/Blockly\.inject\(container/g)).toHaveLength(1);
  });

  it('no source file injects a workspace anywhere else or names the appspot host', () => {
    const hits = [];
    const walk = (dir) => {
      for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        const p = path.join(dir, entry.name);
        if (entry.isDirectory()) {
          if (entry.name !== '__tests__') walk(p);
        } else if (/\.(js|jsx)$/.test(entry.name)) {
          const text = fs.readFileSync(p, 'utf8');
          if (/blockly-demo\.appspot\.com/.test(text)) hits.push(path.relative(APP, p));
          if (/\bBlockly\.inject\(/.test(text) && !p.endsWith('BlocklyWorkspace.jsx')) hits.push(path.relative(APP, p));
        }
      }
    };
    walk(path.join(APP, 'src'));
    expect(hits).toEqual([]);
  });
});
