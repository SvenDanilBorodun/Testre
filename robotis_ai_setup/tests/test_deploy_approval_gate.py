# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.

"""SA-13 (2026-11-13): every job that changes production waits for a person.

The six jobs that write production (the migration apply and rollback, the two
Railway deploys, the trainer-image variable and W6's update gate) name
``environment: production``; the environment's required reviewers are the
gate (owner setting, docs/deploy/DEPLOY.md section 0). Read as text, stdlib
only: each job block must carry exactly that line, and no other job carries
an environment, so the gated set cannot drift silently either way.

That gate exists only on a public repository or with GitHub Enterprise: on a
private repository under Free, Pro or Team an environment has no required
reviewers (verification round B, VB-02). So, by owner decision (2026-11-15),
the migrations (W1) never run on a push: `MigrationsAreManual` holds
supabase-migrate.yml free of a push trigger and evaluates the two gated
`if:` expressions (a small evaluator of GitHub's expression subset) for a
push to main, a release tag, a dispatched release and the „Run workflow"
button.
"""

import pathlib
import re
import unittest

_WF = pathlib.Path(__file__).resolve().parents[2] / '.github' / 'workflows'

GATED = {
    ('supabase-migrate.yml', 'apply-production'),
    ('supabase-migrate.yml', 'rollback'),
    ('railway-deploy-cloud-api.yml', 'deploy'),
    ('railway-deploy-teacher-web.yml', 'deploy'),
    ('release.yml', 'trainer-image-var'),
    ('release.yml', 'publish-gui-version'),
}

_JOB_RE = re.compile(r'^  ([A-Za-z0-9_-]+):\n', re.M)


def _jobs(text: str) -> dict:
    body = text[text.index('\njobs:\n') + len('\njobs:\n'):]
    starts = [(m.group(1), m.start()) for m in _JOB_RE.finditer(body)]
    out = {}
    for i, (name, start) in enumerate(starts):
        end = starts[i + 1][1] if i + 1 < len(starts) else len(body)
        out[name] = body[start:end]
    return out


class DeployApprovalGate(unittest.TestCase):
    def test_exactly_the_production_writers_name_the_environment(self):
        found = set()
        for path in sorted(_WF.glob('*.yml')):
            for job, block in _jobs(path.read_text(encoding='utf-8')).items():
                envs = re.findall(r'^    environment:\s*(.+)$', block, re.M)
                if envs:
                    self.assertEqual(envs, ['production'], (path.name, job))
                    found.add((path.name, job))
        self.assertEqual(found, GATED)

    def test_the_runbook_tells_the_owner_to_add_required_reviewers(self):
        doc = (_WF.parents[1] / 'docs' / 'deploy' / 'DEPLOY.md').read_text(encoding='utf-8')
        self.assertIn('## 0. The production approval gate', doc)
        self.assertIn('Required reviewers', doc)
        self.assertIn('named exactly `production`', doc)


# ── W1 is manual (owner decision 2026-11-15, verification round B VB-02) ────
#
# GitHub offers required reviewers on a PRIVATE repository only with
# Enterprise, so the environment above cannot hold a merge's migration for a
# person on Free, Pro or Team. The owner chose: a merge deploys W2 and W3, and
# the database changes only through a release tag (release.yml calls
# supabase-migrate.yml) or the manual „Run workflow" button.

_TOKEN_RE = re.compile(
    r"\s*(?:(\|\||&&|==|!=|!|\(|\)|,)|'((?:[^']|'')*)'|([A-Za-z_][A-Za-z0-9_.-]*))")


def _tokens(expr: str) -> list:
    out, pos = [], 0
    expr = expr.strip()
    while pos < len(expr):
        m = _TOKEN_RE.match(expr, pos)
        if not m or m.end() == pos:
            raise ValueError(f'cannot read the expression at {expr[pos:pos + 20]!r}')
        op, string, name = m.groups()
        if op is not None:
            out.append(('op', op))
        elif string is not None:
            out.append(('str', string.replace("''", "'")))
        else:
            out.append(('name', name))
        pos = m.end()
    return out


def _number(value):
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if text == '':
        return 0.0
    try:
        return float(text)
    except ValueError:
        return float('nan')


def _loose_eq(a, b) -> bool:
    """GitHub's `==`: strings case-insensitively, otherwise both as numbers
    (null and '' are both 0, so `null == ''` holds)."""
    if isinstance(a, str) and isinstance(b, str):
        return a.lower() == b.lower()
    if type(a) is type(b):
        return a == b
    return _number(a) == _number(b)


def _truthy(value) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    if isinstance(value, str):
        return value != ''
    return True


def evaluate(expr: str, context: dict):
    """A small evaluator for the subset of GitHub's expression language the
    gated `if:` lines use: `||`, `&&`, `!`, `==`, `!=`, parentheses, string
    literals, `null`/`true`/`false`, `startsWith(…)` and dotted context names
    (an unknown name is null, like an input the caller never passed)."""
    toks = _tokens(expr)
    pos = [0]

    def peek():
        return toks[pos[0]] if pos[0] < len(toks) else (None, None)

    def take(kind=None, value=None):
        tok = peek()
        if kind is not None and tok[0] != kind:
            raise ValueError(f'expected {kind}, got {tok}')
        if value is not None and tok[1] != value:
            raise ValueError(f'expected {value!r}, got {tok}')
        pos[0] += 1
        return tok

    def primary():
        kind, value = peek()
        if (kind, value) == ('op', '('):
            take()
            result = or_()
            take('op', ')')
            return result
        if kind == 'str':
            take()
            return value
        if kind == 'name':
            take()
            if value == 'null':
                return None
            if value in ('true', 'false'):
                return value == 'true'
            if peek() == ('op', '('):
                take()
                args = [or_()]
                while peek() == ('op', ','):
                    take()
                    args.append(or_())
                take('op', ')')
                if value == 'startsWith':
                    hay, needle = (str(a if a is not None else '') for a in args)
                    return hay.lower().startswith(needle.lower())
                raise ValueError(f'unknown function {value}')
            return context.get(value)
        raise ValueError(f'unexpected token {peek()}')

    def comparison():
        left = primary()
        if peek() in (('op', '=='), ('op', '!=')):
            op = take()[1]
            right = primary()
            same = _loose_eq(left, right)
            return same if op == '==' else not same
        return left

    def unary():
        if peek() == ('op', '!'):
            take()
            return not _truthy(unary())
        return comparison()

    def and_():
        result = unary()
        while peek() == ('op', '&&'):
            take()
            right = unary()
            result = right if _truthy(result) else result
        return result

    def or_():
        result = and_()
        while peek() == ('op', '||'):
            take()
            right = and_()
            result = result if _truthy(result) else right
        return result

    value = or_()
    if pos[0] != len(toks):
        raise ValueError(f'trailing tokens {toks[pos[0]:]}')
    return _truthy(value)


def _job_if(block: str) -> str:
    """The `if:` of one job block, folded or plain, without `${{ }}`."""
    m = re.search(r'^    if: >-\n((?:      .*\n)+)', block, re.M)
    if m:
        text = ' '.join(line.strip() for line in m.group(1).splitlines())
    else:
        m = re.search(r'^    if: \|\n((?:      .*\n)+)', block, re.M)
        if m:
            text = ' '.join(line.strip() for line in m.group(1).splitlines())
        else:
            m = re.search(r'^    if: (.+)$', block, re.M)
            if not m:
                raise AssertionError('job has no if:')
            text = m.group(1)
    text = text.strip()
    if text.startswith('${{') and text.endswith('}}'):
        text = text[3:-2]
    return text


def _on_block(text: str) -> str:
    m = re.search(r'^on:\n((?:(?:[ #].*)?\n)+)', text, re.M)
    if not m:
        raise AssertionError('no on: block')
    return m.group(1)


_MIGRATE = _WF / 'supabase-migrate.yml'

# (what happens, the github/inputs context, does apply-production run?, does rollback run?)
_CASES = [
    ('a push to main (should a push trigger ever come back)',
     {'github.event_name': 'push', 'github.ref': 'refs/heads/main'}, False, False),
    ('a push to another branch',
     {'github.event_name': 'push', 'github.ref': 'refs/heads/feature/x'}, False, False),
    ('a release tag: release.yml calls this file (caller event push, ref the tag)',
     {'github.event_name': 'push', 'github.ref': 'refs/tags/v2.38.0',
      'inputs.target': 'production'}, True, False),
    ('release.yml dispatched by hand (caller event workflow_dispatch)',
     {'github.event_name': 'workflow_dispatch', 'github.ref': 'refs/heads/main',
      'inputs.target': 'production'}, True, False),
    ('the „Run workflow" button: apply to production',
     {'github.event_name': 'workflow_dispatch', 'github.ref': 'refs/heads/main',
      'inputs.target': 'production', 'inputs.rollback_to': '', 'inputs.reason': 'x'}, True, False),
    ('the „Run workflow" button: a rollback',
     {'github.event_name': 'workflow_dispatch', 'github.ref': 'refs/heads/main',
      'inputs.target': 'production', 'inputs.reason': 'x',
      'inputs.rollback_to': '20261113140000_059_hf_reserved_namespaces_rollback'}, False, True),
    ('the „Run workflow" button: target branch',
     {'github.event_name': 'workflow_dispatch', 'github.ref': 'refs/heads/main',
      'inputs.target': 'branch', 'inputs.rollback_to': '', 'inputs.reason': 'x'}, False, False),
    ('a pull request',
     {'github.event_name': 'pull_request', 'github.ref': 'refs/pull/7/merge'}, False, False),
]


class MigrationsAreManual(unittest.TestCase):
    def setUp(self):
        self.text = _MIGRATE.read_text(encoding='utf-8')
        self.jobs = _jobs(self.text)

    def test_no_push_trigger(self):
        on = _on_block(self.text)
        triggers = re.findall(r'^  ([a-z_]+):', on, re.M)
        self.assertNotIn('push', triggers,
                         'supabase-migrate.yml must not run on a push: a merge to main never '
                         'applies a migration (owner decision 2026-11-15)')
        # The ways in that stay: the release (workflow_call), the button with its
        # rollback input, and the PR branch preview.
        self.assertEqual(set(triggers), {'pull_request', 'workflow_call', 'workflow_dispatch'})
        self.assertRegex(on, r'(?m)^      rollback_to:\n')

    def test_the_release_still_calls_it(self):
        release = _jobs((_WF / 'release.yml').read_text(encoding='utf-8'))
        self.assertIn('    uses: ./.github/workflows/supabase-migrate.yml\n', release['supabase'])

    def test_the_evaluator_reads_github_semantics(self):
        # null and '' compare equal, strings case-insensitively, startsWith too.
        self.assertTrue(evaluate("inputs.x == ''", {}))
        self.assertTrue(evaluate("inputs.x == null", {'inputs.x': ''}))
        self.assertTrue(evaluate("a == 'PUSH'", {'a': 'push'}))
        self.assertFalse(evaluate("a != 'push'", {'a': 'Push'}))
        self.assertTrue(evaluate("startsWith(r, 'refs/tags/v')", {'r': 'refs/tags/v1.2.3'}))
        self.assertFalse(evaluate("!(a == 'x') && true", {'a': 'x'}))
        self.assertTrue(evaluate("(a == 'x' && false) || b == 'y'", {'a': 'x', 'b': 'y'}))

    def test_apply_production_runs_only_for_a_release_tag_or_the_button(self):
        expr = _job_if(self.jobs['apply-production'])
        for label, context, expected, _ in _CASES:
            with self.subTest(label):
                self.assertIs(evaluate(expr, context), expected, label)

    def test_the_rollback_runs_only_from_the_button(self):
        expr = _job_if(self.jobs['rollback'])
        for label, context, _, expected in _CASES:
            with self.subTest(label):
                self.assertIs(evaluate(expr, context), expected, label)

    def test_a_push_disjunct_is_always_bound_to_a_release_tag(self):
        # Belt and braces for a future edit: wherever apply-production's `if`
        # accepts the event `push`, the same conjunction demands a v… tag.
        expr = _job_if(self.jobs['apply-production'])
        for part in expr.split('||'):
            if "'push'" in part:
                self.assertIn("startsWith(github.ref, 'refs/tags/v')", part)

    def test_the_runbook_says_w1_is_run_by_hand(self):
        doc = (_WF.parents[1] / 'docs' / 'deploy' / 'MERGE-CHECKLIST.md').read_text(encoding='utf-8')
        self.assertIn('Run workflow', doc)
        self.assertIn('supabase-migrate', doc)


if __name__ == '__main__':
    unittest.main()
