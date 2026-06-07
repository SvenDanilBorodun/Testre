#!/usr/bin/env python3
"""enum-parity: the wire-int contract between ROS interfaces and the React mirror.

The TaskPhase / TaskCommand enums are hand-duplicated across FOUR sources:

  physical_ai_interfaces/srv/SendCommand.srv   <-> src/constants/taskCommand.js
  physical_ai_interfaces/msg/TaskStatus.msg    <-> src/constants/taskPhases.js

plus literal getattr() fallbacks in the server package (so a container running
pre-rebuild compiled interfaces still decodes the wire ints — see
collision_monitor.py / physical_ai_server.py) and hardcoded ints in the
contract tests. Nothing asserted they agree until this script: an enum added
to the .msg but not taskPhases.js (or a fallback literal that drifts from the
real constant) ships a silent mis-decode. rosbridge serializes raw ints, so
ONLY the int matters on the wire — names may legitimately differ (documented
aliases below).

Checks:
  1. INT-SET equality per pair (srv<->taskCommand.js, msg<->taskPhases.js).
  2. Same-name constants must carry the same int (catches renumbering).
     Known aliases (same int, different name, fine): IDLE<->NONE,
     MOVE_TO_NEXT<->NEXT.
  3. Every getattr(TaskStatus, 'X', n) / getattr(SendCommand.Request, 'X', n)
     literal in the server package equals the real .msg/.srv constant.
  4. The hardcoded phase ints in test_collision_monitor_contract.py equal the
     .msg constants.
"""

from __future__ import annotations

import glob
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]

SRV = ROOT / 'physical_ai_tools/physical_ai_interfaces/srv/SendCommand.srv'
MSG = ROOT / 'physical_ai_tools/physical_ai_interfaces/msg/TaskStatus.msg'
JS_COMMAND = ROOT / 'physical_ai_tools/physical_ai_manager/src/constants/taskCommand.js'
JS_PHASE = ROOT / 'physical_ai_tools/physical_ai_manager/src/constants/taskPhases.js'
SERVER_PKG = ROOT / 'physical_ai_tools/physical_ai_server/physical_ai_server'
CONTRACT_TEST = ROOT / 'robotis_ai_setup/tests/test_collision_monitor_contract.py'

# Same int, different name on the two sides of the wire — accepted as-is.
NAME_ALIASES = {
    ('IDLE', 'NONE'),
    ('MOVE_TO_NEXT', 'NEXT'),
}

errors: list[str] = []


def parse_ros_constants(path: pathlib.Path) -> dict[str, int]:
    """`uint8 NAME = N` lines (constants only — field lines have no `=`)."""
    out: dict[str, int] = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        m = re.match(r'^\s*uint\d+\s+([A-Z][A-Z0-9_]*)\s*=\s*(\d+)', line)
        if m:
            out[m.group(1)] = int(m.group(2))
    if not out:
        errors.append(f'{path}: parsed ZERO constants — parser or file shape broke')
    return out


def parse_js_constants(path: pathlib.Path) -> dict[str, int]:
    """`NAME: N,` entries inside the enum-like object literal."""
    out: dict[str, int] = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        m = re.match(r'^\s*([A-Z][A-Z0-9_]*)\s*:\s*(\d+)\s*,?', line)
        if m:
            out[m.group(1)] = int(m.group(2))
    if not out:
        errors.append(f'{path}: parsed ZERO constants — parser or file shape broke')
    return out


def check_pair(ros: dict[str, int], js: dict[str, int], ros_name: str, js_name: str) -> None:
    ros_ints, js_ints = set(ros.values()), set(js.values())
    if ros_ints != js_ints:
        only_ros = sorted(ros_ints - js_ints)
        only_js = sorted(js_ints - ros_ints)
        errors.append(
            f'{ros_name} <-> {js_name}: int sets differ '
            f'(only in {ros_name}: {only_ros}; only in {js_name}: {only_js}). '
            f'An enum added on one side never reached the other — silent wire mis-decode.'
        )
    for name in ros.keys() & js.keys():
        if ros[name] != js[name]:
            errors.append(
                f'{ros_name}.{name}={ros[name]} but {js_name}.{name}={js[name]} '
                f'— same name, different int: renumbering on one side only.'
            )
    # Names that differ across the wire must be the KNOWN aliases, nothing new.
    ros_by_int = {v: k for k, v in ros.items()}
    for js_key, v in js.items():
        ros_key = ros_by_int.get(v)
        if ros_key and ros_key != js_key and (ros_key, js_key) not in NAME_ALIASES:
            errors.append(
                f'{ros_name}.{ros_key} and {js_name}.{js_key} share int {v} under '
                f'different names — add to NAME_ALIASES if intentional, else align the names.'
            )


def check_getattr_fallbacks(srv: dict[str, int], msg: dict[str, int]) -> int:
    """Every literal fallback in the server package must equal the real constant."""
    pat = re.compile(
        r"getattr\(\s*(TaskStatus|SendCommand\.Request)\s*,\s*'([A-Z0-9_]+)'\s*,\s*(\d+)\s*\)"
    )
    seen = 0
    for py in glob.glob(str(SERVER_PKG / '**' / '*.py'), recursive=True):
        text = pathlib.Path(py).read_text(encoding='utf-8')
        for owner, name, lit in pat.findall(text):
            seen += 1
            table = msg if owner == 'TaskStatus' else srv
            real = table.get(name)
            rel = pathlib.Path(py).relative_to(ROOT)
            if real is None:
                errors.append(
                    f"{rel}: getattr fallback for {owner}.{name} but no such constant "
                    f"in the interface file — typo or the constant was removed."
                )
            elif real != int(lit):
                errors.append(
                    f"{rel}: getattr({owner}, '{name}', {lit}) but the interface "
                    f"defines {name} = {real} — the stale-interface fallback would "
                    f"mis-decode on pre-rebuild containers."
                )
    return seen


def check_contract_test_literals(msg: dict[str, int]) -> int:
    """assertEqual(CM.PHASE_<NAME>, N) hardcodes in the e-stop contract test."""
    if not CONTRACT_TEST.exists():
        errors.append(f'{CONTRACT_TEST}: missing — contract test moved? Update enum_parity.py')
        return 0
    text = CONTRACT_TEST.read_text(encoding='utf-8')
    seen = 0
    for name, lit in re.findall(r'assertEqual\(\s*CM\.PHASE_([A-Z0-9_]+)\s*,\s*(\d+)\s*\)', text):
        seen += 1
        real = msg.get(name)
        if real is None:
            errors.append(
                f'{CONTRACT_TEST.relative_to(ROOT)}: asserts CM.PHASE_{name} '
                f'but TaskStatus.msg has no {name} constant.'
            )
        elif real != int(lit):
            errors.append(
                f'{CONTRACT_TEST.relative_to(ROOT)}: asserts CM.PHASE_{name} == {lit} '
                f'but TaskStatus.msg defines {name} = {real}.'
            )
    return seen


def main() -> int:
    srv = parse_ros_constants(SRV)
    msg = parse_ros_constants(MSG)
    js_cmd = parse_js_constants(JS_COMMAND)
    js_phase = parse_js_constants(JS_PHASE)

    check_pair(srv, js_cmd, 'SendCommand.srv', 'taskCommand.js')
    check_pair(msg, js_phase, 'TaskStatus.msg', 'taskPhases.js')
    n_fallbacks = check_getattr_fallbacks(srv, msg)
    n_test = check_contract_test_literals(msg)

    if errors:
        for e in errors:
            print(f'::error::{e}')
        return 1
    print(
        f'enum-parity OK: {len(srv)} commands + {len(msg)} phases in sync across '
        f'srv/msg/js, {n_fallbacks} getattr fallbacks verified, '
        f'{n_test} contract-test literals verified.'
    )
    return 0


if __name__ == '__main__':
    sys.exit(main())
