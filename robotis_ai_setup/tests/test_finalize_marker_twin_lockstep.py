"""Cross-language lockstep for the finalize-marker `user=` parse (deps-free).

The finalize marker's account field is parsed TWICE, in two languages, by two
readers that must reach the SAME verdict about the same file:

  * ``gui_app.py::_prompt_finalize_install::_marker_user`` (Python) — decides
    whether the exit-0-but-distro-invisible report names a different Windows
    account or falls back to the generic reboot advice;
  * ``installer/scripts/preflight_system.ps1`` check 5 (PowerShell) — decides
    whether disk-yes/registered-no earns a FEHLER naming one cause or the
    ambiguous WARNUNG naming both.

Neither can import the other: one is a nested closure inside a tkinter app, the
other is PowerShell and there is no interpreter for it on any runner here. So a
lockstep test is the correct coupling, exactly as for the three ``ROBOT_PROFILES``
copies, the two ``fast_rehydrate_arms`` twins and the ROS_DOMAIN_ID pair.

WHAT IS FENCED HERE, and only this — the two decisions that were still compared
by nothing:

  1. THE FIELD KEY. Both sides locate the account by the same literal ``user=``.
     A rename on one side alone makes that reader find nothing and silently
     degrade to the generic message while the other keeps accusing.
  2. CASE-INSENSITIVITY. Windows account names are case-insensitive, so a case
     difference is the SAME account. Python casefolds both operands; PowerShell
     uses an ``-i``/default operator. If one side became case-sensitive it would
     report a wrong-account split on a healthy machine — the precise bug check 5
     exists to eliminate — while the other reported nothing.

WHAT IS FENCED ELSEWHERE, so it is not restated here (say each thing once):
``tests/test_gui_install_lifecycle.py::PreflightAccountScopeTest`` pins the
shared marker PATH, the UTF-8 encoding pairing, the U+FFFD refusal and the
byte-equal ``" distro="`` cut; the Python reader's behaviour (case, umlauts,
spaces, garbage, missing file) is pinned behaviourally in the same file.

WHAT REMAINS UNCOMPARED, deliberately: both sides take the name from the FIRST
matching line and stop. That divergence is unreachable — every marker shape
``finalize_install.ps1`` writes is a single ``Set-Content``, and the only
multi-line shape (``FAILED``) carries no ``user=`` at all, so no marker in the
field can contain two candidate lines for the two readers to disagree about.

Properties are phrased over EXTRACTED structure, never as exact substrings: the
Python side through AST, the PowerShell side through a comparison-operator scan,
so a differently-spelled violation (``.lower()`` on one operand only, ``-cne``,
``[string]::Equals``) fails too rather than only a deletion.

Zero-file floor: every twin test in this repo carries one, because a directory
move would otherwise make the guard pass having read nothing.
"""

import ast
import re
import unittest
from pathlib import Path

_SETUP_DIR = Path(__file__).resolve().parent.parent  # robotis_ai_setup/
_PY = _SETUP_DIR / "gui" / "app" / "gui_app.py"
_PS = _SETUP_DIR / "installer" / "scripts" / "preflight_system.ps1"

# The Python closure that parses the marker, and the outer method holding the
# account comparison.
_PY_PARSE_FN = "_marker_user"
_PY_OUTER_FN = "_prompt_finalize_install"

# The field key, written out rather than derived from one side: deriving would
# let the two agree on whatever one of them happened to say, and a deliberate
# rename must be made in three places.
_EXPECTED_FIELD_KEY = "user="

# "A bare KEY= token". Deliberately excludes the value TERMINATOR `" distro="`
# (leading space), which is a different contract and is pinned in
# PreflightAccountScopeTest.
_FIELD_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")

# Python methods that fold case. Any of them makes a comparison
# case-INsensitive; the property is "the operands are case-folded", not
# "casefold() is spelled".
_PY_CASE_FOLDERS = frozenset({"casefold", "lower", "upper"})

# Every PowerShell comparison operator, with its optional case prefix. A `-c`
# form is case-SENSITIVE; a bare or `-i` form is case-insensitive (PowerShell's
# default for string comparison is case-insensitive).
_PS_OP_RE = re.compile(
    r"(?<![\w-])-(c|i)?"
    r"(eq|ne|notlike|notmatch|notcontains|notin|like|match|contains|in)"
    r"(?![\w-])",
    re.IGNORECASE)

# .NET string comparison reachable from PowerShell. `String.Equals(a, b)` and
# `String.Compare(a, b)` default to ORDINAL/culture-sensitive CASE-SENSITIVE
# comparison, so switching to them is a case-sensitivity regression that carries
# no `-c` operator for the scan above to find.
_PS_DOTNET_COMPARE = ("::Equals", "::Compare", ".Equals(", ".CompareTo(")

# The PowerShell variables the account decision is made from. Used to LOCATE the
# predicate rather than to pin its text.
_PS_MARKER_VAR = "$markerUser"
_PS_CURRENT_VAR = "$env:USERNAME"


def _ps_code() -> str:
    """preflight_system.ps1 with comment-only lines removed.

    Stripping is load-bearing: check 5's own rationale names ``user=`` and
    explains why ``-ine`` rather than ``-ne``, so an un-stripped scan would be
    reading the DOCUMENTATION and would pass over a parse that no longer does
    either. (Same stance as PreflightAccountScopeTest._marker_read_block.)
    """
    src = _PS.read_text(encoding="utf-8-sig")
    return "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))


def _py_function(name: str) -> ast.FunctionDef:
    """Any ``def name(...)`` in gui_app.py, nested closures included."""
    tree = ast.parse(_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {_PY}")


def _string_constants(node: ast.AST) -> list:
    return [n.value for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def _py_field_keys() -> set:
    """``KEY=``-shaped literals inside the Python marker parse."""
    return {s for s in _string_constants(_py_function(_PY_PARSE_FN))
            if _FIELD_KEY_RE.fullmatch(s)}


def _ps_field_keys() -> set:
    """``KEY=``-shaped double-quoted literals in the PowerShell script.

    Whole-file rather than a re-derived block: check 5's parse is the only place
    the script carries such a literal, and a block-boundary regex is one more
    thing that rots silently (measured — the set is exactly ``{'user='}``). The
    tradeoff is a false alarm if the script ever grows an unrelated ``KEY=``
    literal, which is a review question rather than a shipped defect.
    """
    return {s for s in re.findall(r'"([^"\n]*)"', _ps_code())
            if _FIELD_KEY_RE.fullmatch(s)}


def _ps_account_predicates() -> list:
    """Comment-stripped lines that COMPARE the marker user with %USERNAME%.

    A line qualifies only if it names both variables AND carries a comparison
    operator, which is what separates the predicate from the diagnostic
    ``Write-Diag`` line and the German ``Emit`` messages (both name the two
    variables for interpolation).
    """
    return [ln.strip() for ln in _ps_code().splitlines()
            if _PS_MARKER_VAR in ln and _PS_CURRENT_VAR in ln
            and _PS_OP_RE.search(ln)]


def _is_case_folded(node: ast.AST) -> bool:
    return (isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _PY_CASE_FOLDERS)


def _py_account_compares() -> list:
    """The ``Compare`` nodes that decide "a different Windows account".

    The two variable names are DERIVED — one from the assignment fed by
    ``_marker_user()``, the other from the assignment that reads ``USERNAME`` —
    so renaming either does not quietly empty this list.
    """
    outer = _py_function(_PY_OUTER_FN)
    marker_var = current_var = None
    for node in ast.walk(outer):
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)):
            continue
        rhs = ast.unparse(node.value)
        if f"{_PY_PARSE_FN}()" in rhs:
            marker_var = node.targets[0].id
        elif "USERNAME" in rhs:
            current_var = node.targets[0].id
    if not marker_var or not current_var:
        raise AssertionError(
            f"{_PY}::{_PY_OUTER_FN} no longer assigns both the marker user "
            f"(from {_PY_PARSE_FN}()) and the current user (from USERNAME): "
            f"marker={marker_var!r} current={current_var!r}")
    out = []
    for node in ast.walk(outer):
        if not isinstance(node, ast.Compare):
            continue
        names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
        if {marker_var, current_var} <= names:
            out.append(node)
    return out


class TestFinalizeMarkerTwinLockstep(unittest.TestCase):

    # ── zero-file floor ──────────────────────────────────────────────────
    def test_both_sources_were_actually_found_and_parsed(self):
        """A guard that cannot fail is worse than no guard.

        Every assertion below is a set comparison or a node walk, and all of
        them are satisfied by NOTHING — an empty set equals an empty set, and a
        loop over zero nodes checks zero operands. So the floor asserts both
        files exist, both parses produced a real body, and both extracted symbol
        sets are non-empty."""
        for label, path in (("python", _PY), ("powershell", _PS)):
            with self.subTest(label):
                self.assertTrue(
                    path.is_file(),
                    f"{path} is missing — did a directory move? Every "
                    "assertion below would pass vacuously")

        parse_fn = _py_function(_PY_PARSE_FN)
        self.assertGreaterEqual(
            len(parse_fn.body), 2,
            f"{_PY}::{_PY_PARSE_FN} has no body outside its docstring")

        code = _ps_code()
        self.assertGreater(len(code.splitlines()), 50,
                           f"{_PS} stripped to almost nothing")
        self.assertIn(_PS_MARKER_VAR, code,
                      f"{_PS} carries no marker parse at all")

        self.assertTrue(_py_field_keys(),
                        "no KEY=-shaped literal in the Python marker parse")
        self.assertTrue(_ps_field_keys(),
                        "no KEY=-shaped literal in the PowerShell script")
        self.assertTrue(_py_account_compares(),
                        "no Python comparison of the two account names")
        self.assertTrue(_ps_account_predicates(),
                        "no PowerShell comparison of the two account names")

    # ── 1. the field key ─────────────────────────────────────────────────
    def test_both_readers_locate_the_account_by_the_same_field_key(self):
        """A rename on ONE side makes that reader find nothing and fall through
        to its generic message, while the other keeps naming an account — the two
        then disagree about the same file, which is the whole class this file
        closes."""
        self.assertEqual(_py_field_keys(), {_EXPECTED_FIELD_KEY},
                         f"{_PY}::{_PY_PARSE_FN} parses a different field")
        self.assertEqual(_ps_field_keys(), {_EXPECTED_FIELD_KEY},
                         f"{_PS} check 5 parses a different field")
        # Belt, the idiom test_arm_scan_twin_lockstep uses: the two assertions
        # above could both be updated to a new-but-matching key; this one fails
        # the moment they diverge from EACH OTHER.
        self.assertEqual(_py_field_keys(), _ps_field_keys())

    def test_the_powershell_offset_matches_the_key_it_skips(self):
        """PowerShell has no ``partition``, so it locates the key and then skips
        its LENGTH — a magic number coupled to the key. Renaming the key without
        moving the number leaves the parsed name carrying the key's own tail.

        Conditional on the API, not vacuous: an implementation that splits
        instead of indexing legitimately has no offset, but one that DOES locate
        the key with ``IndexOf`` must skip it, or the parsed name carries the
        key's own tail. Deleting the ``Substring`` would otherwise leave this
        test passing over an empty list."""
        code = _ps_code()
        offsets = [int(n) for n in re.findall(
            r"Substring\(\s*\$\w+\s*\+\s*(\d+)\s*\)", code)]
        if re.search(r'IndexOf\(\s*"%s"\s*\)' % re.escape(_EXPECTED_FIELD_KEY),
                     code):
            self.assertTrue(
                offsets,
                f"the marker parse locates {_EXPECTED_FIELD_KEY!r} with "
                f"IndexOf but never skips past it — the parsed name would "
                f"start with the key itself")
        for offset in offsets:
            self.assertEqual(
                offset, len(_EXPECTED_FIELD_KEY),
                f"the marker parse skips {offset} characters past the key "
                f"index, but {_EXPECTED_FIELD_KEY!r} is "
                f"{len(_EXPECTED_FIELD_KEY)} long")

    # ── 2. case-insensitivity, on BOTH sides ─────────────────────────────
    def test_the_python_reader_compares_case_insensitively(self):
        """Windows account names are case-insensitive: „Student" and „student"
        are one account, and reporting them as a split replaces correct reboot
        advice with advice that cannot help.

        Asserted as a property of the OPERANDS — both must be case-folding calls
        — so folding only one side fails too, and any of casefold/lower/upper
        passes."""
        compares = _py_account_compares()
        self.assertEqual(
            len(compares), 1,
            f"expected exactly ONE account comparison in {_PY_OUTER_FN}, found "
            f"{[ast.unparse(c) for c in compares]}")
        node = compares[0]
        for operand in [node.left] + list(node.comparators):
            self.assertTrue(
                _is_case_folded(operand),
                f"`{ast.unparse(operand)}` is compared without folding case — "
                f"one of {sorted(_PY_CASE_FOLDERS)} on BOTH operands, or a case "
                f"difference reads as a different Windows account")

    def test_the_powershell_reader_compares_case_insensitively(self):
        """The other half of the same decision. ``-ne``/``-ine``/``-ieq`` are all
        case-insensitive (PowerShell's string default); a ``-c`` form is not, and
        neither is .NET's ``String.Equals``/``String.Compare``, which carry no
        operator for an operator scan to find.

        Deliberately broader than strictly necessary: it refuses a case-sensitive
        operator ANYWHERE in the predicate, including on the
        ``$markerUser -ne ""`` emptiness test where case cannot matter. Deciding
        which operator joins which pair is not something a regex should be
        doing, and a false alarm here is a review question, not a wrong
        accusation shipped to a student."""
        predicates = _ps_account_predicates()
        self.assertEqual(
            len(predicates), 1,
            f"expected exactly ONE account predicate in {_PS}, found "
            f"{predicates}")
        line = predicates[0]
        sensitive = [m.group(0) for m in _PS_OP_RE.finditer(line)
                     if (m.group(1) or "").lower() == "c"]
        self.assertEqual(
            sensitive, [],
            f"case-SENSITIVE comparison operator(s) {sensitive} in the account "
            f"predicate — Windows account names are case-insensitive, so this "
            f"reports a split on the SAME account: {line}")
        insensitive = [m.group(0) for m in _PS_OP_RE.finditer(line)]
        self.assertTrue(
            insensitive,
            f"the account predicate carries no comparison operator: {line}")
        for marker in _PS_DOTNET_COMPARE:
            self.assertNotIn(
                marker, line,
                f"`{marker}` compares case-SENSITIVELY by default — use a "
                f"PowerShell comparison operator: {line}")

    def test_the_two_languages_agree_that_case_does_not_matter(self):
        """The LOCKSTEP itself, stated once: neither reader may become
        case-sensitive on its own. The two tests above each pass on their own
        side; this one is the sentence a reader should find when asking why they
        have to move together."""
        py_folded = all(
            _is_case_folded(op)
            for node in _py_account_compares()
            for op in [node.left] + list(node.comparators))
        ps_folded = all(
            (m.group(1) or "").lower() != "c"
            for line in _ps_account_predicates()
            for m in _PS_OP_RE.finditer(line))
        self.assertEqual(
            (py_folded, ps_folded), (True, True),
            f"python case-folded={py_folded}, powershell "
            f"case-insensitive={ps_folded} — one reader would name an account "
            "the other does not")


if __name__ == "__main__":
    unittest.main()
