"""Every sentence of ``app/services/credit_texts_de.py`` is German (Rule §1, spec §8.1).

The routes pass these constants as NAMES (``detail=NO_HOURS_DE``), so
``.github/scripts/german_detail_lint.py`` cannot see them. This test applies the
lint's OWN rules to every constant, the way ``test_managed_training_lockstep.py``
does for ``training_texts_de``: no transliteration (TRANSLITERATIONS, checked on
every string first), at least one German marker (GERMAN_CHARS or GERMAN_WORDS)
and never English (ENGLISH_WORDS without a German marker). The module and the lint
are loaded by path (stdlib only). The texts are also pinned verbatim to the spec.
"""

from __future__ import annotations

import importlib.util
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TEXTS_PY = os.path.join(os.path.dirname(HERE), "services", "credit_texts_de.py")
LINT_PY = os.path.join(os.path.dirname(HERE), "..", "..", "..", ".github", "scripts", "german_detail_lint.py")

# §8.1, verbatim
SPEC = {
    "ROLE_REFUSED_DE": "Nur Schülerinnen und Schüler können Trainings starten.",
    "POLICY_REFUSED_DE": "Nur ACT-Modelle können in der Cloud trainiert werden.",
    "NO_HOURS_DE": "Keine freien Trainingsstunden mehr. Frag deine Lehrkraft nach weiteren Stunden.",
    "MEMBER_LIMIT_DE": "Du hast dein Stundenlimit in der Arbeitsgruppe erreicht. Frag deine Lehrkraft.",
    "DISPATCH_OFF_DE": "Das Training in der Cloud ist gerade abgeschaltet. Bitte versuche es später noch einmal.",
    "REQUEST_REUSED_DE": "Diese Anfrage wurde schon für eine andere Änderung benutzt. Bitte lade die Seite neu.",
    "DELTA_ZERO_DE": "Die Änderung darf nicht 0 sein.",
    "TEACHER_DELTA_RANGE_DE": "Die Änderung muss zwischen -1000 und 1000 Stunden liegen.",
    "ADMIN_DELTA_RANGE_DE": "Die Änderung muss zwischen -10000 und 10000 Stunden liegen.",
    "POOL_TOO_SMALL_DE": "Im Lehrer-Pool sind nicht genug freie Stunden.",
    "RECLAIM_TOO_MUCH_DE": "Zurückholen geht nur mit freien Stunden – reservierte und verbrauchte Stunden bleiben.",
    "GROUPED_STUDENT_DE": "Die Schülerin oder der Schüler ist in einer Arbeitsgruppe – bitte die Stunden über die "
                          "Gruppe verwalten.",
    "REVOKE_TOO_MUCH_DE": "Entziehen geht höchstens bis zum freien Guthaben der Lehrkraft: {hours}.",
    "LIMIT_RANGE_DE": "Das Limit muss zwischen 1 und 1000 Stunden liegen.",
    "REASON_TOO_LONG_DE": "Der Grund darf höchstens 200 Zeichen lang sein.",
    "CREDIT_CHANGE_FAILED_DE": "Die Stunden konnten nicht geändert werden. Bitte versuche es erneut.",
    "STUDENT_CREATED_POOL_SHORT_DE": "Schüler erstellt, aber im Lehrer-Pool sind nicht genug freie Stunden für die "
                                     "Startstunden.",
    "HISTORY_FAILED_DE": "Der Stundenverlauf konnte nicht geladen werden.",
    "COMPENSATION_REASON_REQUIRED_DE": "Bitte gib einen Grund für den Ausgleich an.",
    "COMPENSATION_HOURS_RANGE_DE": "Ein Ausgleich muss zwischen 1 und 100 Stunden liegen.",
    "COMPENSATION_TARGET_KIND_DE": "Ein Ausgleich geht nur an eine Schülerin, einen Schüler oder eine Arbeitsgruppe.",
    "COMPENSATION_GROUPED_DE": "Die Schülerin oder der Schüler ist in einer Arbeitsgruppe – bitte den Ausgleich der "
                               "Gruppe gutschreiben.",
    "COMPENSATION_NO_ACCOUNT_DE": "Für dieses Ziel gibt es kein nutzbares Guthaben – es gehört zu keinem Klassenzimmer "
                                  "oder wurde gelöscht.",
    "COMPENSATION_TRAINING_MISMATCH_DE": "Dieses Training wurde nicht aus diesem Guthaben bezahlt.",
}
# the existing literals the routes reuse (§8.1) and the one the spec did not name (§8.5's 404)
REUSED = {"STUDENT_NOT_FOUND_DE": "Schüler nicht gefunden", "WORKGROUP_NOT_FOUND_DE": "Arbeitsgruppe nicht gefunden",
          "TEACHER_NOT_FOUND_DE": "Lehrer nicht gefunden", "REASON_NOT_FOUND_DE": "Dieser Grund wurde nicht gefunden."}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, os.path.abspath(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TheCreditSentencesAreGerman(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.texts = _load("_credit_texts_de_under_test", TEXTS_PY)
        cls.lint = _load("_german_detail_lint_for_credit", LINT_PY)
        # the sentences are the module's public `…_DE` constants (`_UNKNOWN` and imported names are not)
        cls.constants = {k: v for k, v in vars(cls.texts).items()
                         if k.endswith("_DE") and not k.startswith("_") and isinstance(v, str)}

    def test_every_spec_sentence_verbatim(self):
        for name, want in {**SPEC, **REUSED}.items():
            with self.subTest(name=name):
                self.assertEqual(self.constants.get(name), want)
        self.assertEqual(set(self.constants), set(SPEC) | set(REUSED))

    def test_the_lints_rules_on_every_constant(self):
        gdl = self.lint
        for name, text in self.constants.items():
            with self.subTest(name=name):
                # TRANSLITERATION first, on every string (never nested inside the language test)
                self.assertIsNone(gdl.TRANSLITERATIONS.search(text), text)
                german = bool(gdl.GERMAN_CHARS.search(text) or gdl.GERMAN_WORDS.search(text))
                self.assertTrue(german, f"{name} carries no German marker: {text!r}")
                self.assertFalse(gdl.ENGLISH_WORDS.search(text) and not german, text)

    def test_no_sentence_claims_a_refund_or_says_credit(self):
        for name, text in self.constants.items():
            with self.subTest(name=name):
                for word in ("erstatt", "Credit", "Trainingsguthaben"):
                    self.assertNotIn(word, text)

    def test_the_revoke_sentence_names_the_hours_as_a_noun_phrase(self):
        self.assertEqual(self.texts.revoke_too_much(1),
                         "Entziehen geht höchstens bis zum freien Guthaben der Lehrkraft: 1 Stunde.")
        self.assertEqual(self.texts.revoke_too_much(1000),
                         "Entziehen geht höchstens bis zum freien Guthaben der Lehrkraft: 1.000 Stunden.")

    def test_the_module_is_stdlib_only(self):
        import ast

        with open(TEXTS_PY, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                self.assertNotEqual((node.module or "").split(".")[0], "app")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotEqual(alias.name.split(".")[0], "app")


if __name__ == "__main__":
    unittest.main()
