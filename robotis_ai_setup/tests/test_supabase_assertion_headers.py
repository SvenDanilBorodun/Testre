"""The hand-run SQL assertion files under ``supabase/tests/`` say in their
header how many PASS lines a correct run prints. Whoever runs one BY HAND
compares against that number, so it must be the number of distinct PASS
labels the file can emit (2026-09-27 review round, n4: the 041 file said 20
and emitted 21). Deliberately stdlib-only.

The header's run instructions must work too (review round 2, ni5/ni6): the
squashed baseline does not replay on a fresh database as shipped, so every
header names the one-line patch and docs/KNOWN-ISSUES.md, and the guard the
040 header prints must still name the one baseline line it replaces; the 041
header names the two 040 assertions 041 breaks by design (T4b, T8), measured
on a local stack at 040 and at 041.
"""

import pathlib
import re
import unittest

_SUPABASE = pathlib.Path(__file__).resolve().parents[1] / 'supabase'
_TESTS = _SUPABASE / 'tests'
_BASELINE = _SUPABASE / 'migrations' / '00000000000000_baseline.sql'
_EXPECTED_RE = re.compile(r'^-- Expected: (\d+) PASS\b', re.M)
_LABEL_RE = re.compile(r'PASS (T\d+[a-z]?)\b')
_UNGUARDED = 'ALTER TABLE public.trainings RENAME COLUMN runpod_job_id TO cloud_job_id;'


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

    def test_every_header_names_the_baseline_patch_and_known_issues(self):
        for path in sorted(_TESTS.glob('*_assertions.sql')):
            header = _header(path.read_text(encoding='utf-8'))
            with self.subTest(file=path.name):
                self.assertIn('docs/KNOWN-ISSUES.md', header)
                self.assertIn('runpod_job_id', header)
                self.assertIn('SCRATCH copy', header)
                # A bare `db reset` on the shipped baseline is exactly what fails.
                self.assertNotIn('db reset --local', header)

    def test_the_printed_guard_still_replaces_the_one_baseline_line(self):
        header = _header((_TESTS / '040_code_programs_assertions.sql').read_text(encoding='utf-8'))
        self.assertIn(_UNGUARDED, header)
        self.assertIn("column_name='runpod_job_id'", header)
        baseline = _BASELINE.read_text(encoding='utf-8')
        # Exactly one unguarded line to replace; once the baseline itself is
        # fixed, this fails and the headers' patch paragraph goes.
        self.assertEqual(baseline.count(_UNGUARDED), 1)

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
