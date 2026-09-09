#!/usr/bin/env python3
"""A test harness may not set a ctx field that production never reads.

FIVE fixture lines set ``ctx.absent_since`` long after the field was deleted
from production, and one of them carried a four-line comment justifying it that
was false three separate times — including its claim that omitting the field
would ``AttributeError`` (``perception_blocks._claim_store`` lazily ``setattr``s
every position store). Two files written the SAME DAY documented that the field
was gone while a third set it and explained why.

A dead fixture line is not merely untidy: it is a false statement about the
contract between the harness and the code, and the next reader believes it.

HONEST LIMITS, stated so nobody over-trusts this:

* it scans classes whose NAME contains "ctx" — the harness shape this repo
  actually uses — not every fixture in the suite;
* "read by production" is a word-boundary search over the whole package TEXT, so
  a name that appears only in a comment counts as read. That is deliberate: a
  loose detector that flags real gaps beats an exact one that needs a parser for
  every access shape (``getattr(ctx, 'x', None)`` is the dominant one here, and
  it is a STRING, not an attribute node);
* the allowlist carries a written reason per entry and a STALE entry is a hard
  error, exactly like ``compose_env_parity.INTENTIONAL`` and
  ``sessionScope.IGNORED_STORAGE_KEYS``.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import physical_ai_server


_PKG = Path(physical_ai_server.__file__).parent
_TESTS = Path(__file__).parent

# name -> why production never reads it. Keep this SHORT; each entry is a claim.
ALLOWED_UNREAD = {
    'gripper_readback': (
        'read by the harness ITSELF — Ctx.get_follower_joints() builds the '
        'follower readback vector from it, so it is a knob the tests turn, not '
        'a field the ctx contract carries.'),
}


def _production_text() -> str:
    return '\n'.join(p.read_text(encoding='utf-8') for p in sorted(_PKG.rglob('*.py')))


def _harness_fields() -> dict[str, list[str]]:
    """``self.<name> = …`` inside every ctx-shaped test class."""
    found: dict[str, list[str]] = {}
    for tp in sorted(_TESTS.glob('*.py')):
        tree = ast.parse(tp.read_text(encoding='utf-8'), filename=str(tp))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or 'ctx' not in node.name.lower():
                continue
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Assign):
                    continue
                for target in sub.targets:
                    if (isinstance(target, ast.Attribute)
                            and isinstance(target.value, ast.Name)
                            and target.value.id == 'self'
                            and not target.attr.startswith('_')):
                        found.setdefault(target.attr, []).append(
                            f'{tp.name}:{sub.lineno}')
    return found


def test_no_test_harness_sets_a_field_production_never_reads():
    prod = _production_text()
    unread = {name: sites for name, sites in _harness_fields().items()
              if not re.search(rf'\b{re.escape(name)}\b', prod)}
    offenders = {n: s for n, s in unread.items() if n not in ALLOWED_UNREAD}
    assert offenders == {}, (
        'these ctx fields are set by a test harness and read NOWHERE in '
        'physical_ai_server/ — either production dropped the field and the '
        'fixture is now a false statement about the contract, or the field '
        'belongs in ALLOWED_UNREAD with a written reason:\n  '
        + '\n  '.join(f'{n}: {s}' for n, s in sorted(offenders.items())))


def test_the_allowlist_has_no_stale_entries():
    """A stale exemption is worse than none: it says a field is knowingly unread
    when production has since started reading it."""
    prod = _production_text()
    fields = _harness_fields()
    for name, reason in ALLOWED_UNREAD.items():
        assert reason.strip(), f'{name} is exempted with no reason'
        assert name in fields, (
            f'{name} is allowlisted but no harness sets it any more — '
            'delete the entry')
        assert not re.search(rf'\b{re.escape(name)}\b', prod), (
            f'{name} IS read by production now — delete the entry')


def test_the_deleted_reclaim_clock_stays_deleted():
    """The specific field this guard was written for. ``absent_since`` was the
    per-tag absence CLOCK; the reclaim became POSITION-based (an absence is
    sampled at loop-pass cadence, 8–12 s, so „absent for ONE poll" ALWAYS
    exceeded the 1.5 s clock and re-grasped a cube nobody had touched)."""
    prod = _production_text()
    assert 'absent_since' not in prod
    assert 'absent_since' not in _harness_fields()
