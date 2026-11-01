"""``credit_texts_de.hours_de`` gives exactly the React twin's text (spec §8.1, review 2 m7).

ONE case table, ``physical_ai_manager/src/utils/__tests__/hoursDe.cases.json`` (written
by the React side, implementer C), holds both helpers: „1 Stunde" for exactly 1, every
other count plural, a non-integer with one decimal and a comma (rounded on the number's
SHORTEST decimal spelling, half away from zero), integers ≥ 1 000 with the de-DE
thousands dot. And no sentence of the API ever says „1 Stunden" or „1 freie Stunden".
"""

from __future__ import annotations

import json
import os
import unittest

from app.services import credit_texts_de as C
from app.services import training_texts_de as T

HERE = os.path.dirname(os.path.abspath(__file__))
CASES = os.path.join(HERE, "..", "..", "..", "..", "physical_ai_tools", "physical_ai_manager", "src", "utils",
                     "__tests__", "hoursDe.cases.json")


class HoursDeIsTheReactTwin(unittest.TestCase):
    def test_every_row_of_the_shared_table(self):
        with open(CASES, encoding="utf-8") as fh:
            cases = json.load(fh)
        self.assertGreaterEqual(len(cases), 40, "the shared table is C's, not a stub")
        for value, text in cases:
            with self.subTest(value=value):
                self.assertEqual(C.hours_de(value), text)

    def test_unknown_is_never_zero(self):
        for value in (None, True, False, float("nan"), float("inf"), "x"):
            self.assertEqual(C.hours_de(value), "—", value)
        self.assertEqual(C.hours_de("2"), "2 Stunden", "a numeric string is the number")

    def test_one_hour_is_singular_everywhere(self):
        self.assertEqual(T.s5(3600), T.S5.format(hours="1 Stunde"))
        self.assertIn("Das Limit von 1 Stunde Trainingszeit", T.s5(3600))
        self.assertIn("Nach 1 Stunde bei Schritt", T.s23(3600, 2500, 50000))
        self.assertIn("Nach 3,5 Stunden bei Schritt 7.500 von 50.000", T.s23(12600, 7500, 50000))
        self.assertIn("2 Stunden", T.s5(7200))
        self.assertTrue(C.revoke_too_much(1).endswith("Lehrkraft: 1 Stunde."))
        for text in (T.s5(3600), T.s23(3600, 1, 2), C.revoke_too_much(1)):
            self.assertNotIn("1 Stunden", text)
            self.assertNotIn("1 freie Stunden", text)

    def test_the_old_hours_text_is_gone(self):
        self.assertFalse(hasattr(T, "hours_text"))


if __name__ == "__main__":
    unittest.main()
