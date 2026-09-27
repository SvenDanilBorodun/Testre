"""The hand-run SQL assertion files under ``supabase/tests/`` say in their
header how many PASS lines a correct run prints. Whoever runs one BY HAND
compares against that number, so it must be the number of distinct PASS
labels the file can emit (2026-09-27 review round, n4: the 041 file said 20
and emitted 21). Deliberately stdlib-only.
"""

import pathlib
import re
import unittest

_TESTS = pathlib.Path(__file__).resolve().parents[1] / 'supabase' / 'tests'
_EXPECTED_RE = re.compile(r'^-- Expected: (\d+) PASS\b', re.M)
_LABEL_RE = re.compile(r'PASS (T\d+[a-z]?)\b')


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


if __name__ == '__main__':
    unittest.main()
