// Writes build/third-party-licenses.txt: every package of the production
// dependency closure (package-lock.json entries without "dev": true) with its
// declared licence and the text of each licence file in its folder. MIT, BSD and
// ISC require their notice to travel with the code, and the minified bundle
// strips those comments, so the build ships them beside it. Served at
// /third-party-licenses.txt by all three nginx images (student, Orange Pi,
// teacher web).
//
// Runs after `vite build` (package.json "build"). Arguments, for the test:
//   node scripts/write-third-party-licenses.mjs [projectRoot] [outFile]
// Exits non-zero when the closure is empty, so a broken install fails the build
// instead of shipping an empty notice. Node standard library only.
import { existsSync, readFileSync, readdirSync, statSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const root = process.argv[2] || join(here, '..');
const outFile = process.argv[3] || join(root, 'build', 'third-party-licenses.txt');

const LICENSE_FILE = /^(licen[cs]e|copying|notice)([._-].*)?$/i;
const RULE = '='.repeat(78);

function declaredLicense(entry, pkg) {
  const pick = (v) => {
    if (!v) return '';
    if (typeof v === 'string') return v;
    if (Array.isArray(v)) return v.map(pick).filter(Boolean).join(' OR ');
    if (typeof v === 'object' && v.type) return String(v.type);
    return '';
  };
  return pick(pkg.license) || pick(pkg.licenses) || pick(entry.license) || '';
}

function licenseTexts(dir) {
  return readdirSync(dir)
    .filter((f) => LICENSE_FILE.test(f))
    .filter((f) => statSync(join(dir, f)).isFile())
    .sort()
    .map((f) => [f, readFileSync(join(dir, f), 'utf8').trim()])
    .filter(([, text]) => text.length > 0);
}

const lock = JSON.parse(readFileSync(join(root, 'package-lock.json'), 'utf8'));
const packages = lock.packages || {};
const entries = [];
let missing = 0;
for (const [key, entry] of Object.entries(packages)) {
  if (!key.startsWith('node_modules/') || entry.dev) continue;
  const dir = join(root, key);
  if (!existsSync(join(dir, 'package.json'))) {
    missing += 1; // an optional platform package that was not installed here
    continue;
  }
  const pkg = JSON.parse(readFileSync(join(dir, 'package.json'), 'utf8'));
  const name = key.slice(key.lastIndexOf('node_modules/') + 'node_modules/'.length);
  entries.push({
    name,
    version: entry.version || pkg.version || '',
    license: declaredLicense(entry, pkg),
    texts: licenseTexts(dir),
  });
}

if (entries.length === 0) {
  console.error('write-third-party-licenses: no production package found in package-lock.json');
  process.exit(1);
}

entries.sort((a, b) => (a.name === b.name ? a.version.localeCompare(b.version) : a.name.localeCompare(b.name)));
const lines = [
  'EduBotics – Open-Source-Bestandteile der Web-Oberfläche',
  'Open-source components of the EduBotics web interface',
  '',
  `${entries.length} Pakete / packages (Produktionsabhängigkeiten laut package-lock.json).`,
  'Schriftarten: siehe /fonts/OFL-*.txt (SIL Open Font License 1.1).',
  '',
];
for (const e of entries) {
  lines.push(RULE, `${e.name} ${e.version}`.trim(), `Lizenz / License: ${e.license || 'nicht angegeben / not declared'}`, RULE);
  if (e.texts.length === 0) {
    lines.push('(Keine Lizenzdatei im Paket / no licence file in the package)', '');
    continue;
  }
  for (const [file, text] of e.texts) lines.push(`--- ${file}`, text, '');
}
writeFileSync(outFile, lines.join('\n').trimEnd() + '\n', 'utf8');
const without = entries.filter((e) => e.texts.length === 0).length;
console.log(`wrote ${outFile} (${entries.length} packages, ${without} without a licence file, ${missing} not installed)`);
