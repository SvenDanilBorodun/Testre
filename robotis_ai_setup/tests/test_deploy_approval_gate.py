# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.

"""SA-13 (2026-11-13): every job that changes production waits for a person.

The six jobs that write production (the migration apply and rollback, the two
Railway deploys, the trainer-image variable and W6's update gate) name
``environment: production``; the environment's required reviewers are the
gate (owner setting, docs/deploy/DEPLOY.md section 0). Each job block must
carry exactly that line, and no other job carries an environment, so the
gated set cannot drift silently either way.

That gate exists only on a public repository or with GitHub Enterprise: on a
private repository under Free, Pro or Team an environment has no required
reviewers (verification round B, VB-02). So, by owner decision (2026-11-15),
the migrations (W1) never run on a push: `MigrationsAreManual` holds
supabase-migrate.yml free of a push trigger and evaluates the gated `if:`
expressions (a small evaluator of GitHub's expression subset, strict: a name
the scenario does not state is an error, never null) for a push to main, a
release tag, a dispatched release and the „Run workflow" button.
Verification round C (VC-07, VC-11): it also proves that release.yml's call
applies to production for both ways a release arrives (no input of the
caller can make it skip), and that every target other than production, a
rollback included, fails the run loudly instead of skipping or acting on
production.

For the same reason W6, the update gate that tells every Windows PC and Pi
about a release, runs only by hand (launch item E6, owner decision
2026-11-15): `W6IsManual` holds publish-gui-version.yml to workflow_dispatch,
release.yml free of any job that sets the update variables or starts W6, and
the W6 job's order (every check before the Railway CLI is installed, the five
variables in their order, GUI_VERSION last and the only one that redeploys).

Review of 2026-11-15 (and VD-03): `ProductionJobsCannotBeSkippedSilently`
simulates each production workflow's job graph for every way a production
run arrives and fails when a production job or one of its steps would be
skipped, when a deploy still runs after a job it needs failed, on any
`continue-on-error` where a failure is the signal, on a checkout of or a git
switch to another ref inside a production job, and on `cancel-in-progress`
for a workflow that writes production (release.yml's newer-tag-wins rule is
the one exception). Its negative controls are the review's mutations.
"""

import json
import pathlib
import re
import unittest

import yaml

_WF = pathlib.Path(__file__).resolve().parents[2] / '.github' / 'workflows'

GATED = {
    ('supabase-migrate.yml', 'apply-production'),
    ('supabase-migrate.yml', 'rollback'),
    ('railway-deploy-cloud-api.yml', 'deploy'),
    ('railway-deploy-teacher-web.yml', 'deploy'),
    ('release.yml', 'trainer-image-var'),
    ('publish-gui-version.yml', 'publish-gui-version'),
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

# The job-status functions; they read the `status` an evaluation is given.
STATUS_FUNCTIONS = ('success', 'failure', 'always', 'cancelled')
ALL_SUCCEEDED = {'success': True, 'failure': False, 'always': True, 'cancelled': False}


class UnknownName(ValueError):
    """A context name the scenario does not define (review of 2026-11-15:
    reading it as null let `needs.x.outputs.y == ''` pass as „runs")."""


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


def _parse(expr: str):
    """The expression as a tree, with GitHub's precedence, tightest first:
    ( ), !, == and !=, &&, || (VC-11: `!` binds to its operand)."""
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
            node = or_()
            take('op', ')')
            return node
        if kind == 'str':
            take()
            return ('lit', value)
        if kind == 'name':
            take()
            if value == 'null':
                return ('lit', None)
            if value in ('true', 'false'):
                return ('lit', value == 'true')
            if peek() == ('op', '('):
                take()
                args = []
                if peek() != ('op', ')'):
                    args.append(or_())
                    while peek() == ('op', ','):
                        take()
                        args.append(or_())
                take('op', ')')
                return ('call', value, args)
            return ('name', value)
        raise ValueError(f'unexpected token {peek()}')

    def unary():
        if peek() == ('op', '!'):
            take()
            return ('not', unary())
        return primary()

    def comparison():
        left = unary()
        if peek() in (('op', '=='), ('op', '!=')):
            op = take()[1]
            return ('eq' if op == '==' else 'ne', left, unary())
        return left

    def and_():
        node = comparison()
        while peek() == ('op', '&&'):
            take()
            node = ('and', node, comparison())
        return node

    def or_():
        node = and_()
        while peek() == ('op', '||'):
            take()
            node = ('or', node, and_())
        return node

    tree = or_()
    if pos[0] != len(toks):
        raise ValueError(f'trailing tokens {toks[pos[0]:]}')
    return tree


def _eval(node, context: dict, status):
    kind = node[0]
    if kind == 'lit':
        return node[1]
    if kind == 'name':
        if node[1] not in context:
            raise UnknownName(node[1])
        return context[node[1]]
    if kind == 'not':
        return not _truthy(_eval(node[1], context, status))
    if kind in ('eq', 'ne'):
        same = _loose_eq(_eval(node[1], context, status), _eval(node[2], context, status))
        return same if kind == 'eq' else not same
    if kind == 'and':  # GitHub's && and || short-circuit and return an operand
        left = _eval(node[1], context, status)
        return _eval(node[2], context, status) if _truthy(left) else left
    if kind == 'or':
        left = _eval(node[1], context, status)
        return left if _truthy(left) else _eval(node[2], context, status)
    name, args = node[1], node[2]
    if name in STATUS_FUNCTIONS:
        if status is None or args:
            raise ValueError(f'{name}() outside a job or step if')
        return status[name]
    values = [_eval(a, context, status) for a in args]
    if name == 'startsWith':
        hay, needle = (str(a if a is not None else '') for a in values)
        return hay.lower().startswith(needle.lower())
    if name == 'contains':
        hay, needle = values
        if isinstance(hay, list):
            return any(_loose_eq(item, needle) for item in hay)
        return str(needle if needle is not None else '').lower() in str(hay if hay is not None else '').lower()
    if name == 'fromJSON':
        arg = values[0]
        return json.loads(arg if isinstance(arg, str) else json.dumps(arg))
    raise ValueError(f'unknown function {name}')


def uses_status_function(expr: str) -> bool:
    toks = _tokens(expr)
    return any(tok == ('name', fn) and i + 1 < len(toks) and toks[i + 1] == ('op', '(')
               for i, tok in enumerate(toks) for fn in STATUS_FUNCTIONS)


def evaluate(expr: str, context: dict, status: dict | None = None):
    """A small evaluator for the subset of GitHub's expression language the
    gated `if:` lines use: `||`, `&&` (both short-circuit), `!`, `==`, `!=`,
    parentheses, string literals, `null`/`true`/`false`, `startsWith`,
    `contains`, `fromJSON`, the status functions (from `status`) and dotted
    context names. A name the context does not define raises `UnknownName`:
    the scenario must state every value an expression can read, null
    included, instead of the model guessing null (review of 2026-11-15)."""
    return _truthy(_eval(_parse(expr), context, status))


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


def _load(text: str) -> dict:
    return yaml.safe_load(text)


def _triggers(doc: dict) -> dict:
    # PyYAML reads the bare key `on` as the boolean True.
    return doc.get(True) or doc.get('on') or {}


def _declared_input_names(text: str) -> set:
    names = set()
    for trigger in _triggers(_load(text)).values():
        if isinstance(trigger, dict):
            names |= set((trigger.get('inputs') or {}).keys())
    return names


def inputs_context(text: str, given: dict | None = None) -> dict:
    """`inputs.<name>` for EVERY input the workflow declares: the given
    value, else null (GitHub's value for an input this arrival does not
    carry). Strict on purpose: a name outside the declaration is an error."""
    declared = _declared_input_names(text)
    unknown = sorted(set(given or {}) - declared)
    if unknown:
        raise ValueError(f'inputs the workflow does not declare: {unknown}')
    ctx = {f'inputs.{name}': None for name in declared}
    ctx.update({f'inputs.{k}': v for k, v in (given or {}).items()})
    return ctx


# How a release arrives in release.yml: the caller's github values and
# release.yml's own inputs (skip_supabase at its default on a dispatch).
RELEASE_ARRIVALS = {
    'a release tag': ({'github.event_name': 'push', 'github.ref': 'refs/tags/v2.99.0'}, {}),
    'a dispatched release': ({'github.event_name': 'workflow_dispatch', 'github.ref': 'refs/heads/main'},
                             {'reason': 'x', 'skip_supabase': False}),
}


def _call_input_defaults(migrate_text: str) -> dict:
    """`on.workflow_call.inputs.<name>.default` of the callee, as strings."""
    on = _on_block(migrate_text)
    m = re.search(r'(?m)^  workflow_call:\n((?:(?:    .*|\s*#.*)?\n)+)', on)
    if not m:
        return {}
    defaults, name = {}, None
    for line in m.group(1).splitlines():
        named = re.match(r'^      ([A-Za-z0-9_-]+):\s*$', line)
        if named:
            name = named.group(1)
            continue
        default = re.match(r'^        default:\s*(.+?)\s*$', line)
        if default and name:
            defaults[name] = default.group(1).strip().strip('"').strip("'")
    return defaults


def release_call_problems(release_text: str, migrate_text: str) -> list:
    """Why a release would NOT apply its migrations to production, or [].

    The release's `supabase` job must call supabase-migrate.yml, must run on a
    release tag and on a dispatched release, and may pass no input that makes
    the callee skip apply-production (VC-07): its `with:` is merged over the
    callee's workflow_call defaults and apply-production's `if` is evaluated
    for both ways a release arrives."""
    problems = []
    jobs = _jobs(release_text)
    callers = [name for name, block in jobs.items() if '    uses: ./.github/workflows/supabase-migrate.yml\n' in block]
    if callers != ['supabase']:
        return [f'release.yml calls supabase-migrate.yml from {callers!r}, not exactly the `supabase` job']
    block = jobs['supabase']
    passed = {}
    m = re.search(r'(?m)^    with:\n((?:      .*\n)+)', block)
    if m:
        for line in m.group(1).splitlines():
            kv = re.match(r'^      ([A-Za-z0-9_-]+):\s*(.*?)\s*$', line)
            if not kv:
                problems.append(f'cannot read the `with:` line {line!r}')
                continue
            value = kv.group(2).strip('"').strip("'")
            if '${{' in value:
                problems.append(f'input {kv.group(1)!r} is an expression: a release must pass a literal')
            passed[kv.group(1)] = value
    defaults = _call_input_defaults(migrate_text)
    if 'target' not in defaults:
        problems.append('supabase-migrate.yml declares no workflow_call `target` default')
    unknown = sorted(set(passed) - set(defaults))
    if unknown:
        problems.append(f'release.yml passes inputs the callee does not declare: {unknown}')
    inputs = {**defaults, **passed}
    migrate_jobs = _jobs(migrate_text)
    apply_if = _job_if(migrate_jobs['apply-production'])
    refuse_if = _job_if(migrate_jobs['refuse-unsupported-target'])
    release_if = _job_if(block)
    for label, (github, own_inputs) in RELEASE_ARRIVALS.items():
        try:
            # Each `if:` read with every job it needs succeeded (the job graph
            # itself is ProductionJobsCannotBeSkippedSilently's).
            if not evaluate(release_if, {**github, **inputs_context(release_text, own_inputs)}, ALL_SUCCEEDED):
                problems.append(f'{label}: release.yml skips its `supabase` job')
            # Inside the call, `inputs` are the callee's: its workflow_call
            # defaults merged with what release.yml passes, null otherwise.
            callee = {**github, **inputs_context(migrate_text, inputs)}
            if not evaluate(apply_if, callee, ALL_SUCCEEDED):
                problems.append(f'{label}: apply-production would be skipped')
            if evaluate(refuse_if, callee, ALL_SUCCEEDED):
                problems.append(f'{label}: the call would be refused')
        except (UnknownName, ValueError) as exc:
            problems.append(f'{label}: cannot evaluate ({type(exc).__name__}: {exc})')
    return problems

# (what happens, the github/inputs context, does apply-production run?, does rollback run?)
_CASES = [
    ('a push to main (should a push trigger ever come back)',
     {'github.event_name': 'push', 'github.ref': 'refs/heads/main'}, False, False),
    ('a push to another branch',
     {'github.event_name': 'push', 'github.ref': 'refs/heads/feature/x'}, False, False),
    ('a release tag: release.yml calls this file (caller event push, ref the tag)',
     {'github.event_name': 'push', 'github.ref': 'refs/tags/v2.38.0',
      'inputs.target': 'production'}, True, False),
    ('a runner that reports the call itself (event workflow_call)',
     {'github.event_name': 'workflow_call', 'github.ref': 'refs/tags/v2.38.0',
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
    # VC-11: a rollback with target branch rolled back PRODUCTION.
    ('the „Run workflow" button: target branch with a rollback',
     {'github.event_name': 'workflow_dispatch', 'github.ref': 'refs/heads/main',
      'inputs.target': 'branch', 'inputs.reason': 'x',
      'inputs.rollback_to': '20261113140000_059_hf_reserved_namespaces_rollback'}, False, False),
    # VC-07: a release that passed any other target skipped its migrations.
    ('a release tag whose call names another target',
     {'github.event_name': 'push', 'github.ref': 'refs/tags/v2.38.0',
      'inputs.target': 'branch'}, False, False),
    ('a dispatched release whose call names another target',
     {'github.event_name': 'workflow_dispatch', 'github.ref': 'refs/heads/main',
      'inputs.target': 'branch'}, False, False),
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

    # ── VC-07: a release can never skip its migrations in silence ──────────
    def test_the_release_call_applies_to_production(self):
        release = (_WF / 'release.yml').read_text(encoding='utf-8')
        self.assertEqual(release_call_problems(release, self.text), [])

    def test_an_explicit_target_production_is_accepted(self):
        # VD-03: a release that names the target it means passes.
        release = _with_block((_WF / 'release.yml').read_text(encoding='utf-8'), '      target: production\n')
        self.assertEqual(release_call_problems(release, self.text), [])
        self.assertEqual(workflow_problems({'release.yml': release}), [])

    def test_the_check_has_teeth(self):
        """Negative controls: each mutation that would make a release skip
        its production apply is caught, with and without an explicit
        `target: production` already in the call (VD-03: the controls used to
        add a second `with:` block, which an explicit target defeated)."""
        plain = (_WF / 'release.yml').read_text(encoding='utf-8')
        uses = '    uses: ./.github/workflows/supabase-migrate.yml\n'
        for base_label, release in (('as shipped', plain),
                                    ('with target: production', _with_block(plain, '      target: production\n'))):
            mutations = {
                'passes target branch': _with_block(release, '      target: branch\n'),
                'passes target as an expression': _with_block(release, '      target: ${{ inputs.where }}\n'),
                'passes an input the callee does not know': _with_block(
                    release, '      target: production\n      skip: true\n'),
                'stops calling it': release.replace(uses, uses.replace('supabase-migrate', 'trainer-image')),
                'skips the job on a tag': release.replace(
                    "github.event_name != 'workflow_dispatch' ||", "github.event_name == 'never' &&", 1),
            }
            for label, text in mutations.items():
                with self.subTest(base=base_label, mutation=label):
                    self.assertNotEqual(text, release, label)
                    self.assertNotEqual(release_call_problems(text, self.text), [], label)
        # ...and on the callee's side: a default other than production, for a
        # release that names no target (one that names production is immune).
        callee = self.text.replace('        default: "production"\n        type: string',
                                   '        default: "branch"\n        type: string', 1)
        self.assertNotEqual(callee, self.text)
        self.assertNotEqual(release_call_problems(plain, callee), [])
        self.assertEqual(release_call_problems(_with_block(plain, '      target: production\n'), callee), [])

    def test_any_other_target_fails_the_run_loudly(self):
        expr = _job_if(self.jobs['refuse-unsupported-target'])
        self.assertIn('exit 1', self.jobs['refuse-unsupported-target'])
        for label, context, apply_expected, rollback_expected in _CASES:
            target = context.get('inputs.target')
            loud = context['github.event_name'] != 'pull_request' and target != 'production'
            with self.subTest(label):
                self.assertIs(evaluate(expr, self._ctx(context)), loud, label)
                if loud:
                    self.assertFalse(apply_expected or rollback_expected, label)

    def _ctx(self, case: dict) -> dict:
        """A _CASES context with every declared input stated (null unless given)."""
        github = {k: v for k, v in case.items() if not k.startswith('inputs.')}
        given = {k[len('inputs.'):]: v for k, v in case.items() if k.startswith('inputs.')}
        return {**github, **inputs_context(self.text, given)}

    def test_the_evaluator_reads_github_semantics(self):
        # null and '' compare equal, strings case-insensitively, startsWith too.
        self.assertTrue(evaluate("inputs.x == ''", {'inputs.x': None}))
        self.assertTrue(evaluate("inputs.x == null", {'inputs.x': ''}))
        self.assertTrue(evaluate("a == 'PUSH'", {'a': 'push'}))
        self.assertFalse(evaluate("a != 'push'", {'a': 'Push'}))
        self.assertTrue(evaluate("startsWith(r, 'refs/tags/v')", {'r': 'refs/tags/v1.2.3'}))
        self.assertFalse(evaluate("!(a == 'x') && true", {'a': 'x'}))
        self.assertTrue(evaluate("(a == 'x' && false) || b == 'y'", {'a': 'x', 'b': 'y'}))
        # VC-11: `!` binds tighter than `==` (GitHub's precedence table).
        self.assertFalse(evaluate("!a == b", {'a': 'x', 'b': 'y'}))
        self.assertTrue(evaluate("!a == false", {'a': 'x'}))
        self.assertTrue(evaluate("!(a == b)", {'a': 'x', 'b': 'y'}))
        self.assertTrue(evaluate("!fromJSON(s || 'false')", {'s': None}))
        self.assertFalse(evaluate("!fromJSON(s || 'false')", {'s': 'true'}))
        self.assertFalse(evaluate("!fromJSON(s || 'false')", {'s': True}))
        # Review of 2026-11-15: an unknown name is an error, never null…
        with self.assertRaises(UnknownName):
            evaluate("needs.version-preflight.outputs.version == ''", {})
        # …but a short-circuited operand is never read, as on GitHub.
        self.assertTrue(evaluate("a == 'x' || nobody.knows", {'a': 'x'}))
        self.assertFalse(evaluate("a == 'y' && nobody.knows", {'a': 'x'}))
        # The status functions read the given job status.
        self.assertTrue(evaluate("always() && a == 'x'", {'a': 'x'}, {'always': True}))
        self.assertFalse(evaluate("success()", {}, {'success': False}))
        with self.assertRaises(ValueError):
            evaluate("success()", {})
        self.assertTrue(uses_status_function("always() && x"))
        self.assertFalse(uses_status_function("github.event_name == 'always'"))

    def test_apply_production_runs_only_for_a_release_tag_or_the_button(self):
        expr = _job_if(self.jobs['apply-production'])
        for label, context, expected, _ in _CASES:
            with self.subTest(label):
                self.assertIs(evaluate(expr, self._ctx(context)), expected, label)

    def test_the_rollback_runs_only_from_the_button(self):
        expr = _job_if(self.jobs['rollback'])
        for label, context, _, expected in _CASES:
            with self.subTest(label):
                self.assertIs(evaluate(expr, self._ctx(context)), expected, label)

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




# ── Production jobs cannot be skipped, softened or redirected in silence ────
#
# Review of 2026-11-15 (and VD-03, verification round D): judging each `if:`
# alone missed edits that skip or soften a production write through the job
# graph. A simulation of each production workflow's job graph, with GitHub's
# rules (a job without a status function in its `if:` runs only when every
# `needs` succeeded; a skipped need skips it), now judges, for every way a
# production run arrives: every production job and every one of its steps
# runs; a failed upstream job skips it (a deploy never tolerates a failed
# migration); no `continue-on-error` turns a failure into success; no step
# checks out or switches to another ref; and no newer run cancels one halfway.

_FILES = ('supabase-migrate.yml', 'release.yml', 'railway-deploy-cloud-api.yml',
          'railway-deploy-teacher-web.yml', 'publish-gui-version.yml')
# Jobs of release.yml that change production through the workflow they call.
_RELEASE_CALLERS = ('supabase', 'cloud-api', 'teacher-web', 'docker', 'installer', 'pi-agent-tarball')
# release.yml's own `cancel-in-progress: true` is deliberate (a newer tag
# wins; each surface keeps its own serialised group): the one exception.
_CANCEL_EXEMPT = {'release.yml'}
_CHECKOUT_REFUSED_KEYS = ('ref', 'repository', 'token', 'ssh-key')
# Commands that change the checked-out tree. Reading history (`git fetch`,
# `git merge-base --is-ancestor`, `git rev-parse`, `git log`) stays allowed.
_GIT_REF_SWITCH = re.compile(
    r'\bgit\s+(?:-C\s+\S+\s+)?(?:checkout|switch|reset|restore|pull|rebase|worktree|cherry-pick|stash|merge)(?![\w-])')
_SUPABASE_USES = '    uses: ./.github/workflows/supabase-migrate.yml\n'


def _with_block(release_text: str, body: str) -> str:
    """release.yml with the `supabase` call's `with:` block replaced by `body`."""
    i = release_text.index(_SUPABASE_USES) + len(_SUPABASE_USES)
    rest = release_text[i:]
    m = re.match(r'    with:\n(?:      .*\n)+', rest)
    if m:
        rest = rest[m.end():]
    return release_text[:i] + '    with:\n' + body + rest


def _as_list(value) -> list:
    if value is None:
        return []
    return [value] if isinstance(value, str) else list(value)


def _expr(value):
    """A job's or step's `if:` as an expression string, or None."""
    if value is None:
        return None
    if isinstance(value, bool):
        return 'true' if value else 'false'
    text = str(value).strip()
    if text.startswith('${{') and text.endswith('}}'):
        text = text[3:-2]
    return text


def _runs(expr, context: dict, needs_results: dict) -> bool:
    status = {'success': all(r == 'success' for r in needs_results.values()),
              'failure': any(r == 'failure' for r in needs_results.values()),
              'always': True, 'cancelled': False}
    if expr is None:
        return status['success']
    if not uses_status_function(expr) and not status['success']:
        return False  # GitHub's implicit `success() && (…)`
    ctx = dict(context)
    ctx.update({f'needs.{n}.result': r for n, r in needs_results.items()})
    return evaluate(expr, ctx, status)


def simulate(doc: dict, context: dict, fail=()) -> dict:
    """Each job's result for one arrival: 'success' when it runs (or
    'failure' when named in `fail`), 'skipped' when it does not."""
    jobs = doc['jobs']
    results = {}

    def result(name, seen=()):
        if name not in results:
            if name in seen:
                raise ValueError(f'a needs cycle at {name}')
            needs = {n: result(n, seen + (name,)) for n in _as_list(jobs[name].get('needs'))}
            ran = _runs(_expr(jobs[name].get('if')), context, needs)
            results[name] = ('failure' if name in fail else 'success') if ran else 'skipped'
        return results[name]

    for name in jobs:
        result(name)
    return results


def _upstream(jobs: dict, name: str) -> set:
    out, todo = set(), _as_list(jobs[name].get('needs'))
    while todo:
        need = todo.pop()
        if need not in out:
            out.add(need)
            todo.extend(_as_list(jobs[need].get('needs')))
    return out


def _call_inputs(release_text: str, migrate_text: str) -> dict:
    """What supabase-migrate.yml's `inputs` hold when release.yml calls it."""
    inputs = dict(_call_input_defaults(migrate_text))
    for line in (re.search(r'(?m)^    with:\n((?:      .*\n)+)', _jobs(release_text)['supabase']) or [None, ''])[1].splitlines():
        kv = re.match(r'^      ([A-Za-z0-9_-]+):\s*(.*?)\s*$', line)
        if kv:
            inputs[kv.group(1)] = kv.group(2).strip('"').strip("'")
    return inputs


def _arrivals(texts: dict) -> dict:
    """file → [(label, context, jobs that must run)]: every way a production
    run of that workflow arrives, with every value its expressions may read."""
    tag = {'github.event_name': 'push', 'github.ref': 'refs/tags/v2.99.0'}
    button = {'github.event_name': 'workflow_dispatch', 'github.ref': 'refs/heads/main'}
    merge = {'github.event_name': 'push', 'github.ref': 'refs/heads/main'}
    mig, rel = texts['supabase-migrate.yml'], texts['release.yml']
    call = {k: v for k, v in _call_inputs(rel, mig).items() if k in _declared_input_names(mig)}
    release_jobs = set(_load(rel)['jobs'])
    out = {
        'supabase-migrate.yml': [
            ('a release tag', {**tag, **inputs_context(mig, call)}, {'apply-production'}),
            ('a dispatched release', {**button, **inputs_context(mig, call)}, {'apply-production'}),
            ('the button', {**button, **inputs_context(mig, {'target': 'production', 'rollback_to': '',
                                                              'reason': 'x'})}, {'apply-production'}),
            ('the button, a rollback', {**button, **inputs_context(mig, {
                'target': 'production', 'reason': 'x',
                'rollback_to': '20261113140000_059_hf_reserved_namespaces_rollback'})}, {'rollback'}),
        ],
        'release.yml': [
            ('a release tag', {**tag, **inputs_context(rel, {})}, release_jobs),
            ('a dispatched release', {**button, **inputs_context(rel, {'reason': 'x', 'skip_supabase': False})},
             release_jobs - {'pi-agent-tarball', 'next-step'}),
        ],
        'publish-gui-version.yml': [
            ('the button', {**button, **inputs_context(texts['publish-gui-version.yml'], {
                'version': '2.99.0', 'installer_sha256': '', 'pi_agent_sha256': ''})}, {'publish-gui-version'}),
        ],
    }
    for name in ('railway-deploy-cloud-api.yml', 'railway-deploy-teacher-web.yml'):
        text = texts[name]
        out[name] = [
            ('a merge to main', {**merge, **inputs_context(text, {})}, {'deploy'}),
            ('a release tag (release.yml calls it)', {**tag, **inputs_context(text, {})}, {'deploy'}),
            ('the button', {**button, **inputs_context(text, {'reason': 'x'})}, {'deploy'}),
        ]
    return out


def _step_problems(where: str, job: dict, context: dict) -> list:
    problems = []
    success = ALL_SUCCEEDED
    for i, step in enumerate(job.get('steps') or []):
        label = f"{where} step {i + 1} ({step.get('name') or step.get('uses')})"
        expr = _expr(step.get('if'))
        if expr is not None and not evaluate(expr, context, success):
            problems.append(f'{label} is skipped when everything succeeds')
        if str(step.get('uses', '')).startswith('actions/checkout@'):
            refused = sorted(set(step.get('with') or {}) & set(_CHECKOUT_REFUSED_KEYS))
            if refused:
                problems.append(f"{label} checks out with {refused}: only the event's own commit")
        if _GIT_REF_SWITCH.search(step.get('run') or ''):
            problems.append(f'{label} switches git to another ref')
    return problems


def workflow_problems(overrides: dict | None = None) -> list:
    """Every reason a production run could be skipped, softened or redirected
    in silence; `overrides` replaces a workflow's text (the negative controls)."""
    texts = {name: (_WF / name).read_text(encoding='utf-8') for name in _FILES}
    texts.update(overrides or {})
    docs = {name: _load(text) for name, text in texts.items()}
    problems = list(release_call_problems(texts['release.yml'], texts['supabase-migrate.yml']))
    gated = {(f, j) for f, j in GATED if f in docs}
    protected = set(gated) | {('release.yml', j) for j in _RELEASE_CALLERS} | {
        ('supabase-migrate.yml', 'refuse-unsupported-target')}
    for f, j in list(protected):
        protected |= {(f, u) for u in _upstream(docs[f]['jobs'], j)}

    # No continue-on-error where a failure is the signal, job or step.
    for f, j in sorted(protected):
        job = docs[f]['jobs'][j]
        if job.get('continue-on-error') not in (None, False):
            problems.append(f'{f}::{j} has continue-on-error: a failure would read as success')
        for step in job.get('steps') or []:
            if step.get('continue-on-error') not in (None, False):
                problems.append(f"{f}::{j} step {step.get('name') or step.get('uses')!r} has continue-on-error")

    # Never cancelled halfway (the workflow's group and the job's own).
    for f in sorted({f for f, _ in gated} - _CANCEL_EXEMPT):
        for where, conc in [(f, docs[f].get('concurrency'))] + [
                (f'{f}::{j}', docs[f]['jobs'][j].get('concurrency')) for g, j in gated if g == f]:
            if isinstance(conc, dict) and conc.get('cancel-in-progress') not in (None, False):
                problems.append(f'{where}: cancel-in-progress would stop a production run halfway')

    for f, arrivals in _arrivals(texts).items():
        jobs = docs[f]['jobs']
        for label, context, must_run in arrivals:
            try:
                results = simulate(docs[f], context)
                for j in sorted(must_run):
                    if results.get(j) != 'success':
                        problems.append(f'{f}, {label}: {j} would be {results.get(j, "missing")}')
                for g, j in sorted(gated | {('release.yml', c) for c in _RELEASE_CALLERS}):
                    if g != f or results.get(j) != 'success':
                        continue
                    if (g, j) in gated:
                        problems += [f'{f}, {label}: {p}' for p in _step_problems(j, jobs[j], context)]
                    for upstream in sorted(_upstream(jobs, j)):
                        if simulate(docs[f], context, fail={upstream})[j] != 'skipped':
                            problems.append(f'{f}, {label}: {j} still runs after {upstream} failed')
            except (UnknownName, ValueError) as exc:
                problems.append(f'{f}, {label}: cannot evaluate ({type(exc).__name__}: {exc})')
    return problems


class ProductionJobsCannotBeSkippedSilently(unittest.TestCase):
    def test_no_production_run_is_skipped_softened_or_redirected(self):
        self.assertEqual(workflow_problems(), [])

    def test_the_simulation_reads_github_rules(self):
        doc = {'jobs': {
            'a': {},
            'b': {'needs': 'a'},
            'c': {'needs': ['a'], 'if': "${{ always() && needs.a.result != 'failure' }}"},
            'd': {'needs': ['b'], 'if': "github.ref == 'x'"},
        }}
        self.assertEqual(simulate(doc, {'github.ref': 'x'}),
                         {'a': 'success', 'b': 'success', 'c': 'success', 'd': 'success'})
        # A failed need skips a plain job; always() decides by its own expression.
        self.assertEqual(simulate(doc, {'github.ref': 'x'}, fail={'a'}),
                         {'a': 'failure', 'b': 'skipped', 'c': 'skipped', 'd': 'skipped'})
        # The implicit success() short-circuits: an unknown name behind it is never read.
        self.assertEqual(simulate(doc, {}, fail={'b'})['d'], 'skipped')
        with self.assertRaises(UnknownName):
            simulate(doc, {})

    def test_reading_history_is_not_switching_refs(self):
        for run in ('git fetch --depth=50 origin main', 'git merge-base --is-ancestor "$PROD" HEAD',
                    'git rev-parse HEAD', 'git log -1 --format=%H', 'echo "the git context"'):
            with self.subTest(run=run):
                self.assertIsNone(_GIT_REF_SWITCH.search(run))
        for run in ('git checkout v2.37.0', 'git switch main', 'git -C repo reset --hard x',
                    'git pull origin main', 'git merge other', 'git restore --source=x .'):
            with self.subTest(run=run):
                self.assertIsNotNone(_GIT_REF_SWITCH.search(run))

    def test_the_review_mutations_turn_it_red(self):
        """Each edit the review (2026-11-15) and VD-03 found passing is caught,
        and so are the same edits on the W6 and Railway workflows."""
        read = {name: (_WF / name).read_text(encoding='utf-8') for name in _FILES}
        apply_needs = "    needs: [validate]\n    # SA-13 (2026-11-13): on a public repository or GitHub Enterprise the\n"
        supabase_if = "        !fromJSON(inputs.skip_supabase || 'false')\n"
        supabase_whole = "        github.event_name != 'workflow_dispatch' ||\n" + supabase_if
        w6_set = '      - name: Set GUI auto-update env vars on the cloud-api service\n'
        mutations = {
            # the review of 2026-11-15
            'an if on validate': ('supabase-migrate.yml', '  validate:\n    name: Lint + parse migrations\n',
                                  "  validate:\n    name: Lint + parse migrations\n    if: github.event_name == 'pull_request'\n"),
            'continue-on-error on apply-production': ('supabase-migrate.yml', apply_needs,
                                                      apply_needs + '    continue-on-error: true\n'),
            'another ref checked out in apply-production': ('supabase-migrate.yml', '      - name: Link to production\n',
                                                            '      - name: Use another ref\n        run: git checkout v2.37.0\n'
                                                            '      - name: Link to production\n'),
            'a checkout of another ref in apply-production': (
                'supabase-migrate.yml', '      - name: Link to production\n',
                '      - uses: actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd  # v6.0.2\n'
                '        with:\n          ref: v2.37.0\n      - name: Link to production\n'),
            'cancel-in-progress on W1': ('supabase-migrate.yml',
                                         '  group: supabase-migrate-production\n  cancel-in-progress: false\n',
                                         '  group: supabase-migrate-production\n  cancel-in-progress: true\n'),
            'W1 skipped by a needs output (whole if)': ('release.yml', supabase_whole,
                                                        '        (' + supabase_whole.strip() + ') &&\n'
                                                        "        needs.version-preflight.outputs.version == ''\n"),
            'W1 skipped by a needs output (last operand)': ('release.yml', supabase_if,
                                                            "        !fromJSON(inputs.skip_supabase || 'false') &&\n"
                                                            "        needs.version-preflight.outputs.version == ''\n"),
            # VD-03
            'apply-production needs the refuse job': ('supabase-migrate.yml', apply_needs,
                                                      apply_needs.replace('[validate]', '[validate, refuse-unsupported-target]')),
            'W2 tolerates a failed W1': ('release.yml', "needs.supabase.result == 'skipped')",
                                         "needs.supabase.result == 'skipped' || needs.supabase.result == 'failure')"),
            'continue-on-error on the refuse step': ('supabase-migrate.yml', '      - name: Refuse\n',
                                                     '      - name: Refuse\n        continue-on-error: true\n'),
            'a step if on the push step': ('supabase-migrate.yml', '      - name: Apply pending migrations\n',
                                           "      - name: Apply pending migrations\n        if: github.event_name == 'workflow_dispatch'\n"),
            'apply-production tolerates a failed validate': (
                'supabase-migrate.yml', "    if: >-\n      ${{\n        (github.event_name == 'push'",
                "    if: >-\n      ${{\n        always() && (github.event_name == 'push'"),
            # the same rules on W6 and the Railway deploys
            'W6: continue-on-error on the set step': ('publish-gui-version.yml', w6_set,
                                                      w6_set + '        continue-on-error: true\n'),
            'W6: a step if on the set step': ('publish-gui-version.yml', w6_set,
                                              w6_set + "        if: github.event_name == 'push'\n"),
            'W6: a checkout of another ref': ('publish-gui-version.yml', '    steps:\n      - name: Check the release',
                                              '    steps:\n      - uses: actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd  # v6.0.2\n'
                                              '        with:\n          ref: some-branch\n      - name: Check the release'),
            'W6: cancellable': ('publish-gui-version.yml', 'cancel-in-progress: false', 'cancel-in-progress: true'),
            'W6: a job if': ('publish-gui-version.yml', '    environment: production\n',
                             "    environment: production\n    if: github.event_name == 'push'\n"),
            'W2: continue-on-error on deploy': ('railway-deploy-cloud-api.yml', '    needs: [schema-probe]\n',
                                                '    needs: [schema-probe]\n    continue-on-error: true\n'),
            'W2: deploys after a failed probe': ('railway-deploy-cloud-api.yml', '    needs: [schema-probe]\n',
                                                 '    needs: [schema-probe]\n    if: always()\n'),
            'W3: deploy only from the button': ('railway-deploy-teacher-web.yml', '    needs: [prebuild-validate]\n',
                                                "    needs: [prebuild-validate]\n    if: github.event_name == 'workflow_dispatch'\n"),
        }
        for label, (name, old, new) in mutations.items():
            with self.subTest(label):
                self.assertEqual(read[name].count(old), 1, f'{label}: the anchor must be unique')
                mutated = read[name].replace(old, new, 1)
                _load(mutated)  # still valid YAML
                self.assertNotEqual(workflow_problems({name: mutated}), [], label)

# ── W6 is manual (launch item E6, owner decision 2026-11-15) ────────────────
#
# On a private repository under Free, Pro or Team the production environment
# approves nothing, so a W6 inside release.yml advertised every tag to every
# school PC and Pi before anyone had tried it. W6 now runs only when the owner
# starts it, after the tag's .exe and Pi tarball ran on real hardware.

_W6 = _WF / 'publish-gui-version.yml'
_UPDATE_VARS = ('GUI_RELEASE_REPO', 'GUI_DOWNLOAD_URL', 'GUI_INSTALLER_SHA256',
                'PI_AGENT_SHA256', 'GUI_VERSION')
_SET_RE = re.compile(r'railway variable set "([A-Z0-9_]+)=([^"]*)"((?:[^\n]*\\\n)*[^\n]*)')


def _steps(block: str) -> list:
    """(name, text) of each `- name:` step of one job block, in order."""
    body = block[block.index('\n    steps:\n'):]
    parts = re.split(r'(?m)^      - name: ', body)[1:]
    return [(p.split('\n', 1)[0].strip(), p) for p in parts]


_RAILWAY_CALL = re.compile(r'(^|[\s;&|(])railway\s+[a-z]', re.M)


def _code(text: str) -> str:
    """`text` without its comment lines."""
    return '\n'.join(line for line in text.splitlines() if not line.lstrip().startswith('#'))


def _railway_sets(text: str) -> list:
    """(variable, value, skips the deploy?) of every `railway variable set`."""
    return [(m.group(1), m.group(2), '--skip-deploys' in m.group(3)) for m in _SET_RE.finditer(text)]


def w6_problems(text: str) -> list:
    """Why publish-gui-version.yml would not be a manual, check-first W6."""
    problems = []
    triggers = re.findall(r'^  ([a-z_]+):', _on_block(text), re.M)
    if triggers != ['workflow_dispatch']:
        problems.append(f'triggers are {triggers!r}, not only workflow_dispatch')
    if not re.search(r'(?m)^      version:\n        description: .*\n        required: true\n', _on_block(text)):
        problems.append('no required `version` input')
    jobs = _jobs(text)
    if list(jobs) != ['publish-gui-version']:
        return problems + [f'jobs are {list(jobs)!r}, not exactly publish-gui-version']
    block = jobs['publish-gui-version']
    if 'cancel-in-progress: false' not in text:
        problems.append('a second dispatch could cancel one halfway through the variable sets')
    steps = _steps(block)
    names = [n for n, _ in steps]
    install = 'Install Railway CLI (pinned release binary, SHA-256 checked)'
    if install not in names:
        return problems + ['no pinned Railway CLI install step']
    first_change = names.index(install)
    checks = '\n'.join(s for _, s in steps[:first_change])
    for needle, why in (
        ('refs/heads/main', 'runs from main'),
        ('releases/tags/${TAG}', 'reads the release from the channel'),
        ('"private"', 'proves the channel public'),
        ('sha256:([0-9a-f]{64})', "reads GitHub's recorded digests"),
        ('EduBotics_Setup.exe', 'checks the installer'),
        ('edubotics-pi-agent.tar.gz', 'checks the Pi tarball'),
        ('"$tgz" pi_agent/VERSION ', "reads the tarball's version"),
        ('"$tgz" pi_agent/docker/versions.env ', "reads the tarball's IMAGE_TAG"),
        ('for repo in ', 'probes the -opi images'),
    ):
        if needle not in checks:
            problems.append(f'no check before the Railway CLI install: {why}')
    if _RAILWAY_CALL.search(_code(checks)):
        problems.append('a step before the Railway CLI install runs railway')
    downloads = _code(checks).split('DOWNLOAD_URL="https://github.com/', 1)[-1].split('sha256sum', 1)[0]
    if 'Authorization' in downloads or '-u ' in downloads:
        problems.append('the asset download sends a credential: the fleet downloads anonymously')
    sets = _railway_sets(block)
    if [v for v, _, _ in sets] != list(_UPDATE_VARS):
        problems.append(f'sets {[v for v, _, _ in sets]!r}, not {list(_UPDATE_VARS)!r} in that order')
    else:
        if [skip for _, _, skip in sets] != [True, True, True, True, False]:
            problems.append('every variable but GUI_VERSION (last, the one redeploy) must --skip-deploys')
    set_steps = [n for n, s in steps if 'railway variable set' in s]
    if len(set_steps) != 1 or names.index(set_steps[0]) <= first_change:
        problems.append('the variables must be set in one step after the CLI install')
    return problems


class W6IsManual(unittest.TestCase):
    def setUp(self):
        self.text = _W6.read_text(encoding='utf-8')

    def test_publish_gui_version_is_a_manual_check_first_w6(self):
        self.assertEqual(w6_problems(self.text), [])

    def test_the_check_has_teeth(self):
        mutations = {
            'a push trigger': self.text.replace('on:\n  workflow_dispatch:\n', 'on:\n  push:\n    tags: ["v*"]\n  workflow_dispatch:\n', 1),
            'a workflow_call trigger': self.text.replace('on:\n  workflow_dispatch:\n', 'on:\n  workflow_call:\n  workflow_dispatch:\n', 1),
            'GUI_VERSION before the hashes': self.text.replace(
                'railway variable set "GUI_INSTALLER_SHA256=', 'railway variable set "GUI_VERSION_X=', 1),
            'GUI_VERSION with --skip-deploys': self.text.replace(
                '"GUI_VERSION=${VERSION}" \\\n              --service scintillating-empathy --environment production >/dev/null',
                '"GUI_VERSION=${VERSION}" \\\n              --service scintillating-empathy --environment production --skip-deploys >/dev/null', 1),
            'no digest check': self.text.replace('sha256:([0-9a-f]{64})', 'sha256:(.*)', 1),
            'no tarball version check': self.text.replace('"$tgz" pi_agent/VERSION ', '"$tgz" pi_agent/README ', 1),
            'no tarball IMAGE_TAG check': self.text.replace('"$tgz" pi_agent/docker/versions.env ', '"$tgz" pi_agent/x ', 1),
            'a token on the download': self.text.replace('curl -fsSL --retry 3 --retry-connrefused --max-time 600 -o "$exe"',
                                                          'curl -fsSL -H "Authorization: Bearer ${GH_API_TOKEN}" --retry 3 --retry-connrefused --max-time 600 -o "$exe"', 1),
            'railway in a check step': self.text.replace('      - name: Gate the Pi update on the four -opi images\n',
                                                         '      - name: Early\n        run: railway whoami\n      - name: Gate the Pi update on the four -opi images\n', 1),
            'not tied to main': self.text.replace('refs/heads/main', 'refs/heads/', 1),
            'cancellable': self.text.replace('cancel-in-progress: false', 'cancel-in-progress: true', 1),
        }
        for label, text in mutations.items():
            with self.subTest(label):
                self.assertNotEqual(text, self.text, label)
                self.assertNotEqual(w6_problems(text), [], label)

    def test_no_other_workflow_sets_the_update_gate_or_starts_w6(self):
        for path in sorted(_WF.glob('*.yml')):
            if path.name == _W6.name:
                continue
            text = path.read_text(encoding='utf-8')
            with self.subTest(workflow=path.name):
                for var, _, _ in _railway_sets(text):
                    self.assertNotIn(var, _UPDATE_VARS, f'{path.name} sets {var}')
                # Only comments may name it: no `uses:`, no `gh workflow run`,
                # no dispatch through the API.
                self.assertNotIn('publish-gui-version.yml', _code(text), f'{path.name} starts W6')
                self.assertNotIn('workflow run publish-gui-version', _code(text))

    def test_release_yml_has_no_w6_job(self):
        jobs = _jobs((_WF / 'release.yml').read_text(encoding='utf-8'))
        self.assertNotIn('publish-gui-version', jobs)
        for name, block in jobs.items():
            with self.subTest(job=name):
                self.assertNotIn('uses: ./.github/workflows/publish-gui-version.yml', block)
        # The release ends in a job that changes nothing and says so.
        self.assertIn('next-step', jobs)
        self.assertIn('    permissions: {}\n', jobs['next-step'])
        self.assertNotIn('environment:', jobs['next-step'])
        self.assertNotIn('secrets.', jobs['next-step'])

    def test_the_runbooks_say_w6_is_run_by_hand(self):
        root = _WF.parents[1]
        for rel in ('docs/deploy/DEPLOY.md', 'docs/deploy/MERGE-CHECKLIST.md',
                    'docs/deploy/PRIVATE-REPOSITORY.md', 'CLAUDE.md'):
            with self.subTest(doc=rel):
                self.assertIn('publish-gui-version.yml', (root / rel).read_text(encoding='utf-8'))

if __name__ == '__main__':
    unittest.main()
