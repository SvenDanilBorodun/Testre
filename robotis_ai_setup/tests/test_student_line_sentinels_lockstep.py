"""A student's text line can never START a sentinel (B9, owner decision OQ-5 B).

„melde [1, b]" used to read „(1, b)": `handlers/output.py::emit_student_text`
replaced every `[`/`]` with `(`/`)` so a student line could not spoof
`[VAR:…]`, `[SPEAK:…]`, `[TONE:…]`, `[SOUND]`, `[TOAST:…]`, `[CNT:…]` or
`[TECHNIK] `. Now the brackets stay ASCII and only a line whose first
character other than a space or U+FEFF is `[` gets an invisible U+2060 WORD
JOINER in front (`output.STUDENT_LINE_GUARD`). That is a guard ONLY because
every client parser of a student line is anchored at the line's FIRST
character — which this test fences on the client's own source text
(deps-free, no node):

1. The set of non-test files under `physical_ai_manager/src/` that mention the
   student-line stream (`log_message` or `workshop.log`) is EXACTLY the
   reviewed set: a new reader fails here until it is reviewed and added.
2. In `useRosTopicSubscription.js::interceptToken` and `RunControls.jsx`,
   every regex literal and every string comparand that starts with `[` —
   one passed as an IDENTIFIER resolved to its constant included — is
   `^`-anchored or used through `startsWith(` / `===`, the set of heads is
   exactly the table below (a new sentinel parser must join it, anchored),
   and `interceptToken` matches the raw `message` (no trim/replace first).
3. `handlers/output.py` defines `STUDENT_LINE_GUARD == '\\u2060'`.

U+2060 survives JS `trim()`/`trimStart()` and Python `strip()` and `\\s` (it is a
format character, Cf), and `/^\\[VAR:/.test('\\u2060[VAR:x=1]')` is false.
"""

import ast
import pathlib
import re
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / 'physical_ai_tools' / 'physical_ai_manager' / 'src'
_OUTPUT = (_REPO_ROOT / 'physical_ai_tools' / 'physical_ai_server' / 'physical_ai_server'
           / 'workflow' / 'handlers' / 'output.py')
_HOOK = _SRC / 'hooks' / 'useRosTopicSubscription.js'
_RUN_CONTROLS = _SRC / 'components' / 'Workshop' / 'RunControls.jsx'

# The reviewed readers (and the one writer) of the student-line stream.
_READERS = {
    'hooks/useRosTopicSubscription.js',
    'components/Workshop/RunControls.jsx',
    'components/Workshop/SimStage.jsx',
    'features/workshop/workshopSlice.js',
    'hooks/useSimPreview.js',
}

# Every head a client parser of a student line looks for at a line's start.
_HEADS = {'[SOUND]', '[TONE:', '[SPEAK:', '[TOAST:', '[VAR:', '[CNT:', '[TECHNIK] '}

_STRING = re.compile(r"""(['"`])((?:\\.|(?!\1).)*)\1""")
_REGEX_AT = re.compile(r'(?:^|(?<=[\s(=,!&|?:;{}\[]))/((?:\\.|[^/\\\n])+)/([a-z]*)')
_REGEX_META = set('()[]\\.*+?{}|$')


def _strip_comments(text: str) -> str:
    """Drop `//` line comments and `/* */` blocks outside strings and regex
    literals — enough for these two files (both use comments heavily, and a
    comment's `[VAR:…]` example is no parser)."""
    out = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in '\'"`':
            m = _STRING.match(text, i)
            if m:
                out.append(m.group(0))
                i = m.end()
                continue
        if text.startswith('//', i):
            j = text.find('\n', i)
            i = n if j < 0 else j
            continue
        if text.startswith('/*', i):
            j = text.find('*/', i + 2)
            i = n if j < 0 else j + 2
            continue
        if c == '/':
            m = _REGEX_AT.match(text, i) if (i == 0 or re.match(r'[\s(=,!&|?:;{}\[]', text[i - 1])) else None
            if m:
                out.append(m.group(0))
                i = m.end()
                continue
        out.append(c)
        i += 1
    return ''.join(out)


def _intercept_token_body(text: str) -> str:
    start = text.index('const interceptToken = useCallback((message) => {')
    end = text.index('\n  }, [', start)
    return text[start:end]


def _regex_head(body: str) -> str:
    """The literal characters a regex demands first, after `^`: `^\\[TONE:(…` → `[TONE:`."""
    assert body.startswith('^'), body
    i, head = 1, ''
    while i < len(body):
        c = body[i]
        if c == '\\' and i + 1 < len(body) and not body[i + 1].isalnum():
            head += body[i + 1]
            i += 2
            continue
        if c in _REGEX_META:
            break
        head += c
        i += 1
    return head


def _bracket_parsers(code: str, consts: dict) -> tuple[set, list]:
    """(heads, problems) of every bracket parser in ``code``."""
    heads, problems = set(), []
    for m in _REGEX_AT.finditer(code):
        body = m.group(1)
        if body.lstrip('^').startswith('\\['):
            if not body.startswith('^'):
                problems.append(f'unanchored regex /{body}/')
            else:
                heads.add(_regex_head(body))
    for m in _STRING.finditer(code):
        value = m.group(2)
        if not value.startswith('[') and not value.startswith('\\\\['):
            continue
        before = code[max(0, m.start() - 40):m.start()].rstrip()
        if before.endswith('==='):
            heads.add(value)
        elif before.endswith('startsWith('):
            heads.add(value)
        elif re.search(r'const\s+([A-Z_][A-Z0-9_]*)\s*=$', before):
            pass   # judged where the constant is used
        else:
            problems.append(f'{value!r} used outside startsWith/===: …{before[-30:]}')
    for name, value in consts.items():
        if not value.startswith('['):
            continue
        for use in re.finditer(rf'\b{name}\b', code):
            before = code[max(0, use.start() - 40):use.start()].rstrip()
            after = code[use.end():use.end() + 1]
            if re.search(r'\bconst$', before):
                continue   # the declaration itself
            if before.endswith('startsWith(') or before.endswith('==='):
                heads.add(value)
            elif before.endswith('.slice(') and code[use.end():].startswith('.length'):
                continue   # text.slice(PREFIX.length) — not a comparison
            else:
                problems.append(f'{name} used outside startsWith/===: …{before[-30:]}{name}{after}')
    for m in re.finditer(r'\.startsWith\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)', code):
        name = m.group(1)
        if name not in consts:
            problems.append(f'startsWith({name}) with no constant in the file')
    return heads, problems


def _consts(code: str) -> dict:
    return {m.group(1): m.group(3) for m in re.finditer(
        r"""const\s+([A-Z_][A-Z0-9_]*)\s*=\s*(['"])((?:\\.|(?!\2).)*)\2""", code)}


class TheReadersOfTheStudentLineStream(unittest.TestCase):
    def test_the_files_that_read_student_lines_are_exactly_the_reviewed_set(self):
        found = set()
        for path in _SRC.rglob('*'):
            if path.suffix not in ('.js', '.jsx') or not path.is_file():
                continue
            rel = path.relative_to(_SRC).as_posix()
            if '__tests__' in path.parts or re.search(r'\.(test|spec)\.jsx?$', path.name):
                continue
            text = path.read_text(encoding='utf-8')
            if 'log_message' in text or 'workshop.log' in text:
                found.add(rel)
        self.assertEqual(found, _READERS)


class EveryBracketParserIsAnchored(unittest.TestCase):
    def test_the_heads_are_the_table_and_every_parser_is_anchored(self):
        hook = _strip_comments(_HOOK.read_text(encoding='utf-8'))
        intercept = _intercept_token_body(hook)
        run_controls = _strip_comments(_RUN_CONTROLS.read_text(encoding='utf-8'))
        heads, problems = set(), []
        for code, consts in ((intercept, _consts(hook)), (run_controls, _consts(run_controls))):
            h, p = _bracket_parsers(code, consts)
            heads |= h
            problems += p
        self.assertEqual(problems, [])
        self.assertEqual(heads, _HEADS)

    def test_intercept_token_matches_the_raw_message(self):
        intercept = _intercept_token_body(_strip_comments(_HOOK.read_text(encoding='utf-8')))
        execs = re.findall(r'\.exec\(([^)]*)\)', intercept)
        self.assertGreaterEqual(len(execs), 5)
        self.assertEqual(set(execs), {'message'})
        self.assertNotRegex(intercept, r'\bmessage\s*=[^=]')
        self.assertNotRegex(intercept, r'\bmessage\.(trim|trimStart|trimLeft|replace|normalize)')

    def test_the_fence_bites(self):
        """The checker refuses an unanchored parser, a bracket head used with
        includes(), and a constant compared some other way."""
        _h, p = _bracket_parsers("m = /\\[VAR:(.*)\\]/.exec(message);", {})
        self.assertTrue(p)
        _h, p = _bracket_parsers("if (message.includes('[SOUND]')) {}", {})
        self.assertTrue(p)
        code = "const P = '[TECHNIK] ';\nif (text.indexOf(P) === 0) {}"
        _h, p = _bracket_parsers(code, _consts(code))
        self.assertTrue(p)
        code = "const P = '[TECHNIK] ';\nif (text.startsWith(P)) { return text.slice(P.length); }"
        h, p = _bracket_parsers(code, _consts(code))
        self.assertEqual((h, p), ({'[TECHNIK] '}, []))


class TheServerGuard(unittest.TestCase):
    def test_output_defines_the_word_joiner_guard(self):
        tree = ast.parse(_OUTPUT.read_text(encoding='utf-8'))
        values = {node.targets[0].id: ast.literal_eval(node.value) for node in tree.body
                  if isinstance(node, ast.Assign) and len(node.targets) == 1
                  and isinstance(node.targets[0], ast.Name)
                  and node.targets[0].id == 'STUDENT_LINE_GUARD'}
        self.assertEqual(values, {'STUDENT_LINE_GUARD': '⁠'})
        # Written as the escape, never the raw (invisible) character.
        self.assertNotIn('⁠', _OUTPUT.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
