"""The hand-run SQL assertion files under ``supabase/tests/`` say in their
header how many PASS lines a correct run prints. Whoever runs one BY HAND
compares against that number, so it must be the number of distinct PASS
labels the file can emit (2026-09-27 review round, n4: the 041 file said 20
and emitted 21). Deliberately stdlib-only.

The header's run instructions must work too (review round 2, ni5/ni6). Until
2026-11-10 the squashed baseline did not replay on a fresh database (its
migration-005 body renamed a runpod_job_id column its own CREATE TABLE never
declares), and every header carried a hand patch for that line. The baseline now
guards the rename, so the file replays as shipped (measured: 000 → 053 on
PostgreSQL 16, and through `supabase db push` 2.122.0), and no header may ask
for a patched copy any more. The 041 header names the two 040 assertions 041
breaks by design (T4b, T8), measured on a local stack at 040 and at 041.
"""

import pathlib
import re
import unittest

_SUPABASE = pathlib.Path(__file__).resolve().parents[1] / 'supabase'
_TESTS = _SUPABASE / 'tests'
_BASELINE = _SUPABASE / 'migrations' / '00000000000000_baseline.sql'
_EXPECTED_RE = re.compile(r'^-- Expected: (\d+) PASS\b', re.M)
_LABEL_RE = re.compile(r'PASS (T\d+[a-z]?)\b')
_RENAME = 'ALTER TABLE public.trainings RENAME COLUMN runpod_job_id TO cloud_job_id;'


def _header(text: str) -> str:
    """The leading comment block (the run instructions)."""
    lines = []
    for line in text.splitlines():
        if not line.startswith('--'):
            break
        lines.append(line[2:].strip())
    return ' '.join(lines)


class AssertionHeaders(unittest.TestCase):
    def test_every_header_counts_the_labels_its_file_emits(self):
        files = sorted(_TESTS.glob('*_assertions.sql'))
        self.assertTrue(files, _TESTS)
        for path in files:
            text = path.read_text(encoding='utf-8')
            match = _EXPECTED_RE.search(text)
            self.assertIsNotNone(match, f'{path.name}: no "Expected: N PASS" header')
            labels = set(_LABEL_RE.findall(text))
            self.assertEqual(int(match.group(1)), len(labels),
                             f'{path.name}: header says {match.group(1)}, the file emits '
                             f'{len(labels)} distinct PASS labels')

    def test_no_header_asks_for_a_patched_baseline(self):
        for path in sorted(_TESTS.glob('*_assertions.sql')):
            header = _header(path.read_text(encoding='utf-8'))
            with self.subTest(file=path.name):
                # The workaround text of the unreplayable baseline, in any of its spellings.
                self.assertNotIn('runpod_job_id', header)
                self.assertNotIn('guarded exactly as', header)
                self.assertNotIn('NOT replay', header)
                self.assertNotIn('not replay', header)
                # Each header still says how its migrations get applied.
                self.assertTrue('supabase start' in header or 'psql' in header, header[:200])

    def test_the_baseline_replays_its_005_rename_guarded(self):
        baseline = _BASELINE.read_text(encoding='utf-8')
        # The rename is still there for a database that has the old column...
        self.assertEqual(baseline.count(_RENAME), 1)
        # ...but never as a top-level statement: on a fresh database the
        # CREATE TABLE above it already declares cloud_job_id, and the bare
        # statement failed with 'column "runpod_job_id" does not exist'.
        bare = [ln for ln in baseline.splitlines() if ln.startswith(_RENAME)]
        self.assertEqual(bare, [], 'the 005 rename runs unguarded again')
        section = baseline[baseline.index('BEGIN: 005_cloud_job_id.sql'):baseline.index('END: 005_cloud_job_id.sql')]
        block = section[section.index('DO $$'):section.index('END $$;')]
        self.assertIn(_RENAME, block)
        self.assertIn("column_name = 'runpod_job_id'", block)
        self.assertIn("column_name = 'cloud_job_id'", block)
        self.assertLess(block.index("column_name = 'runpod_job_id'"), block.index(_RENAME))

    def test_the_050_header_names_the_049_assertions_050_breaks(self):
        header = _header((_TESTS / '050_training_continuation_assertions.sql').read_text(encoding='utf-8'))
        # the 049 labels E50's charging change breaks, and the counts at 049 and after 050.
        for label in ('T1', 'T5a', 'T5d', 'T29b', 'T35b', 'T36a', 'T36b'):
            self.assertIn(label, header)
        self.assertIn('69 PASS', header)
        self.assertIn('62 PASS', header)

    def test_the_041_header_names_the_040_assertions_041_breaks(self):
        header = _header((_TESTS / '041_code_destinations_assertions.sql').read_text(encoding='utf-8'))
        for label in ('T4b', 'T8'):
            self.assertIn(label, header)
        self.assertIn('14 PASS', header)
        # The two premises of that note, in 040's own file.
        text040 = (_TESTS / '040_code_programs_assertions.sql').read_text(encoding='utf-8')
        self.assertIn(''''{"legacy": true}'::jsonb''', text040)
        self.assertIn("'public.update_workflow_code(uuid,uuid,jsonb,text)'", text040)
        labels040 = set(_LABEL_RE.findall(text040))
        self.assertEqual(len(labels040) - 2, 14)


if __name__ == '__main__':
    unittest.main()
