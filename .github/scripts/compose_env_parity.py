#!/usr/bin/env python3
r"""Per-compose, per-SERVICE env-forwarding parity between the two student-facing
compose targets: `docker-compose.yml` (Windows/WSL2) and `docker-compose.opi.yml`
(Orange Pi 5 Pro).

WHY THIS EXISTS
---------------
`ci.yml::env-forwarding-guard` proves that every `EDUBOTICS_*` name a shipped
source reads appears in an `environment:` list — but it builds a UNION over all
four shipped compose files and reduces it with `comm`. A key present on ONE
target therefore satisfies it for EVERY target, and a key on the WRONG SERVICE
of the right file satisfies it too. Both failure modes shipped together:
`EDUBOTICS_ROBOT_TYPE` was on the opi compose but only under
`physical_ai_server`, so an Orange Pi's `open_manipulator` container never saw
the variable its entrypoint branches on and ran the OMX Dynamixel bringup
against a Feetech bus; and none of the five `EDUBOTICS_EDU6_*` rollback knobs
reached a Pi at all. Two verifiers MEASURED that the union guard stays green
with all six keys deleted from the opi compose.

This job closes exactly that gap. It compares the two files SERVICE BY SERVICE,
so neither "present somewhere else" nor "present on another service" can hide a
missing forward.

SCOPE — the Jetson compose is deliberately OUT
----------------------------------------------
`jetson_agent/docker-compose.jetson.yml` is NOT a target here, not even
advisorily. It is a legitimately different and much smaller product surface
(7 + 3 env keys against 23 + 47 + 1 here), the Jetson is a shared classroom
inference target rather than a full rig, and gating it would demand a Jetson
parity audit nobody has scoped. Adding it later means adding it to `TARGETS`
and doing that audit — not flipping a warning to an error.

SCOPE — the shipped pi_agent twin is deliberately NOT re-checked
---------------------------------------------------------------
`robotis_ai_setup/pi_agent/docker/docker-compose.opi.yml` is the copy that
rides the release tarball to a fielded Pi. It is byte-identical to the opi file
below, and that equality is already fenced twice: `ci.yml::pi-compose-twin-guard`
on the source tree and `release.yml::pi-agent-tarball` on the built artifact.
Byte-identity makes every per-service property proven here transitive to the
twin, so re-deriving it would be a second failure surface reporting the same
bug in worse words. The residual is named rather than hidden: if BOTH twin gates
were ever deleted, this job's conclusions would silently stop applying to what
actually ships. Do not delete them.

WHAT IS GATED, AND WHAT IS NOT
------------------------------
GATED: the set of services, and the set of environment KEYS per service.
NOT GATED (reported as `::notice`): differing VALUES for a shared key. A
legitimate value divergence already exists and must stay — the opi compose pins
`EDUBOTICS_CAMERA_SOURCE=usb_cam` as a literal where the student compose
interpolates `${EDUBOTICS_CAMERA_SOURCE:-}`, which is correct for a Pi (there is
no WSL2 native-bridge there). Notices are classified so the one dangerous
sub-class is visible in the log: a key the student compose leaves
operator-overridable (`${...}`) but the opi compose pins to a bare literal is a
knob the `.env` can no longer reach. Whether that sub-class should GATE rather
than notify is an open question for the owner; this script does not decide it.

Maintainer-facing, so English throughout (CLAUDE.md Rule §1).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

STUDENT_REL = "robotis_ai_setup/docker/docker-compose.yml"
OPI_REL = "robotis_ai_setup/docker/docker-compose.opi.yml"

# Services that MUST carry at least one env key in BOTH files. This is the
# "a guard that reads nothing is green" fence: without it, a repo-wide switch to
# compose's dict form (`environment:\n  KEY: value`), a renamed service, or a
# reindent would make this script parse zero entries on both sides and pass with
# nothing compared. `physical_ai_manager` is deliberately absent — it genuinely
# has no env block on the student side.
REQUIRED_ENV_SERVICES = ("open_manipulator", "physical_ai_server")

# Divergences that are intentional. Key: (side, service, env_key) where `side`
# names the file the key is present in and the OTHER file is missing it.
# Every entry needs a reason, and a STALE entry is a hard error (see
# `_check_allowlist_is_live`) — an allowlist nobody prunes silently
# pre-authorises the re-introduction of a divergence that was already fixed.
INTENTIONAL: "dict" = {
    ("opi", "physical_ai_manager", "EDUBOTICS_ROS_NET_GATEWAY"): (
        "Pi-only. The opi manager's nginx template proxies the Roboter-Studio "
        "control bridge at http://${EDUBOTICS_ROS_NET_GATEWAY}:8769/, i.e. back "
        "out of ros_net to the agent on the host. On Windows that bridge is "
        "reached at 127.0.0.1:8769 by the browser directly, so the student "
        "manager has no env block at all."
    ),
}

_ENTRY_RE = re.compile(r"^-\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_INTERPOLATED_RE = re.compile(r"\$\{[A-Za-z_]")

# Compose mechanisms that inject environment WITHOUT an `environment:` block.
# None is used by either file today (measured). Each is refused rather than
# ignored: this gate's whole premise is that it can see every forward, and a
# service pulling env from a file, a parent service or a YAML merge would make
# the comparison quietly incomplete. `logging: *opi_logging` is untouched — an
# alias only matters where it could carry env.
_HIDDEN_ENV_KEYS = ("env_file:", "extends:", "<<:")

_errors: "list" = []


def die(msg: str) -> "None":
    print(f"::error::{msg}", file=sys.stderr)
    raise SystemExit(1)


def error(msg: str, *, file: "str | None" = None, line: "int | None" = None) -> None:
    """Record a gate failure as a GitHub annotation. Never exits — every
    divergence is reported in one run, because fixing them one CI round at a
    time is how a six-key gap turns into six pushes."""
    loc = ""
    if file:
        loc = f" file={file}" + (f",line={line}" if line else "")
    print(f"::error{loc}::{msg}")
    _errors.append(msg)


def parse_compose(path: Path, rel: str) -> "dict":
    """Return {service: {ENV_KEY: (value, line_no)}} for one compose file.

    A deliberately small indentation-aware scanner rather than PyYAML: no CI job
    outside `python-tests` installs pyyaml, and every other `.github/scripts/*.py`
    is stdlib-only.

    The trade is that it reads ONE shape — a block sequence of `- KEY=value`
    under `environment:`, at or below that key's indent. Everything a compose
    file could otherwise use to carry environment is REFUSED rather than
    skipped, because a scanner that silently ignores what it does not recognise
    fails exactly the way the union guard this job supplements does: an
    unreadable `environment:` would make a service read as env-less and its
    missing keys as nothing to report. Refused: an inline/flow/aliased
    `environment:`, a second `environment:` in one service, `env_file:`,
    `extends:`, a `<<:` merge, an entry that is not `- KEY=value`, a duplicate
    key, and a file with no services. Aliases elsewhere are none of this gate's
    business — the opi compose really does use `logging: *opi_logging`.

    Two structural fences catch what a line scanner cannot reason about:
    `REQUIRED_ENV_SERVICES` (a required service that parses to zero env entries
    is a failure, not an empty comparison) and service-set equality (a renamed
    or extra service is reported, never skipped).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        die(f"cannot read {rel}: {e}")

    services: "dict" = {}
    seen_env_block: "set" = set()
    in_services = False
    service = None
    service_indent = None
    env_indent = None
    in_env = False

    for lineno, raw in enumerate(text.split("\n"), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        stripped = raw.strip()

        if indent == 0:
            in_services = stripped == "services:"
            service, in_env = None, False
            continue
        if not in_services:
            continue

        if service_indent is None:
            service_indent = indent
        if indent == service_indent:
            if not stripped.endswith(":"):
                die(f"{rel}:{lineno}: expected a service name at indent "
                    f"{service_indent}, got {stripped!r} — this parser "
                    "understands one compose shape and refuses to guess")
            service = stripped[:-1].strip()
            services.setdefault(service, {})
            in_env = False
            continue
        if service is None:
            continue

        # A block sequence may sit at the SAME indent as its key — `environment:`
        # followed by `- KEY=v` also at indent 4 is valid YAML and compose reads
        # it. Closing the block on `indent <= env_indent` alone dropped every
        # such entry silently, which is the exact failure class this gate
        # exists to prevent (found by adversarial review, 2026-07-28: a
        # flush-style key genuinely missing on the Pi came back green). Only a
        # non-sequence line at or above the block's indent ends it, and a
        # mapping key can never begin with `-`.
        if in_env and indent <= env_indent and not stripped.startswith("-"):
            in_env = False

        if stripped.startswith("environment:"):
            if stripped != "environment:":
                # Flow style (`environment: [A=1]`), an alias (`*env`), or an
                # inline empty list. All valid compose; none is the shape this
                # parser reads, and skipping the line would leave the service
                # looking env-less.
                die(f"{rel}:{lineno}: `{stripped}` in service {service!r} puts "
                    "the environment inline. This gate reads only a block "
                    "sequence of `- KEY=value`; teach it the new shape rather "
                    "than letting the service read as env-less.")
            if service in seen_env_block:
                die(f"{rel}:{lineno}: a second `environment:` inside "
                    f"{service!r} — refusing to merge them silently")
            seen_env_block.add(service)
            in_env, env_indent = True, indent
            continue

        if not in_env:
            if any(stripped.startswith(k) for k in _HIDDEN_ENV_KEYS):
                die(f"{rel}:{lineno}: `{stripped}` in service {service!r} can "
                    "inject environment this gate cannot see. Refusing rather "
                    "than comparing an incomplete picture — if the shape is "
                    "wanted, teach this parser to follow it.")
            continue

        match = _ENTRY_RE.match(stripped)
        if not match:
            # Covers `- KEY` (no value), `- KEY: value`, and compose's dict form
            # (whose entries do not start with `-`). Each would otherwise vanish
            # from the comparison while the job stayed green.
            die(f"{rel}:{lineno}: environment entry {stripped!r} in service "
                f"{service!r} is not the `- KEY=value` form this gate reads. "
                "Either restore that form or teach this parser the new one — "
                "an unparsed entry is an unchecked forward.")
        key, value = match.group(1), match.group(2).strip()
        if key in services[service]:
            die(f"{rel}:{lineno}: {key} is listed twice in service "
                f"{service!r} — compose keeps the last one, so this is a bug "
                "in the compose file, not in this gate")
        services[service][key] = (value, lineno)

    if not services:
        die(f"{rel}: no services parsed at all — the file shape changed, or "
            "the path is wrong. Refusing to pass having compared nothing.")
    return services


def _check_required_services(name: str, rel: str, parsed: "dict") -> None:
    for svc in REQUIRED_ENV_SERVICES:
        if svc not in parsed:
            die(f"{rel}: required service {svc!r} not found (found: "
                f"{sorted(parsed)}). If a service was renamed, update "
                "REQUIRED_ENV_SERVICES in this script — do not let the "
                "comparison silently skip it.")
        if not parsed[svc]:
            die(f"{rel}: service {svc!r} has no parsed environment entries. "
                "That is either a real deletion or a shape this parser no "
                f"longer reads; either way {name} is not being checked.")


def _check_allowlist_is_live(used: "set") -> None:
    """Fail on an allowlist entry that no longer describes a real divergence.

    Only meaningful for the SHIPPED pair — INTENTIONAL is a statement about
    those two files specifically, so it is not applied when explicit paths are
    passed (tests, acceptance runs). Applying it there would make every
    fixture comparison fail for a reason that has nothing to do with the
    fixture.
    """
    stale = sorted(set(INTENTIONAL) - used)
    for side, svc, key in stale:
        error(
            f"INTENTIONAL divergence ({side}/{svc}/{key}) no longer exists — "
            "the two composes now agree on that key. Delete the allowlist "
            "entry. A stale entry silently pre-authorises re-introducing the "
            "divergence, which is the opposite of what the allowlist is for.",
            file=".github/scripts/compose_env_parity.py",
        )


def main(argv: "list") -> int:
    if len(argv) == 1:
        # Resolved from this file's own location, not the CWD, so the job
        # cannot be made to compare the wrong tree by being invoked elsewhere.
        # Computed here rather than at import so a copy of this script placed
        # anywhere (the tests do exactly that) still imports cleanly.
        repo_root = Path(__file__).resolve().parents[2]
        student_path, opi_path = repo_root / STUDENT_REL, repo_root / OPI_REL
        student_rel, opi_rel = STUDENT_REL, OPI_REL
        shipped_pair = True
    elif len(argv) == 3:
        # Test-only override, so the acceptance test can run against a scratch
        # copy without mutating the working tree. CI always calls with no args.
        student_path, opi_path = Path(argv[1]), Path(argv[2])
        student_rel, opi_rel = argv[1], argv[2]
        shipped_pair = False
    else:
        die("usage: compose_env_parity.py [<student-compose> <opi-compose>]")

    student = parse_compose(student_path, student_rel)
    opi = parse_compose(opi_path, opi_rel)
    _check_required_services("student", student_rel, student)
    _check_required_services("opi", opi_rel, opi)

    # --- services -----------------------------------------------------------
    # Set equality, not intersection. Iterating the intersection is how a
    # renamed service gets silently dropped from the comparison.
    only_student = sorted(set(student) - set(opi))
    only_opi = sorted(set(opi) - set(student))
    for svc in only_student:
        error(f"service {svc!r} exists in {student_rel} but not in {opi_rel} — "
              f"its {len(student[svc])} env keys are unchecked on the Pi",
              file=opi_rel)
    for svc in only_opi:
        error(f"service {svc!r} exists in {opi_rel} but not in {student_rel} — "
              "nothing compares its env keys",
              file=opi_rel)

    used_allowlist: "set" = set()
    notices: "list" = []

    for svc in sorted(set(student) & set(opi)):
        s_env, o_env = student[svc], opi[svc]

        for key in sorted(set(s_env) - set(o_env)):
            entry = ("student", svc, key)
            if entry in INTENTIONAL:
                used_allowlist.add(entry)
                notices.append(f"{svc}/{key}: student-only by design — "
                               f"{INTENTIONAL[entry]}")
                continue
            error(
                f"service {svc!r} is MISSING env key {key} "
                f"(forwarded by {student_rel}:{s_env[key][1]}). Add it to that "
                f"service's environment: list, or add "
                f'("student", "{svc}", "{key}") to INTENTIONAL in '
                ".github/scripts/compose_env_parity.py with a reason.",
                file=opi_rel,
            )

        for key in sorted(set(o_env) - set(s_env)):
            entry = ("opi", svc, key)
            if entry in INTENTIONAL:
                used_allowlist.add(entry)
                notices.append(f"{svc}/{key}: opi-only by design — "
                               f"{INTENTIONAL[entry]}")
                continue
            error(
                f"service {svc!r} forwards env key {key}, which "
                f"{student_rel} does not. Add it there too, or add "
                f'("opi", "{svc}", "{key}") to INTENTIONAL in '
                ".github/scripts/compose_env_parity.py with a reason.",
                file=opi_rel,
                line=o_env[key][1],
            )

        for key in sorted(set(s_env) & set(o_env)):
            s_val, o_val = s_env[key][0], o_env[key][0]
            if s_val == o_val:
                continue
            # Informational only — see the module docstring. Classified so the
            # one sub-class that can silently disable an operator knob is
            # visible in the job log rather than buried in a value dump.
            pinned = bool(_INTERPOLATED_RE.search(s_val)) and not _INTERPOLATED_RE.search(o_val)
            kind = "PINNED-LITERAL" if pinned else "value"
            print(f"::notice file={opi_rel},line={o_env[key][1]}::{kind} diff "
                  f"{svc}/{key}: student={s_val!r} opi={o_val!r}"
                  + (" — the Pi cannot override this from its .env" if pinned else ""))

    if shipped_pair:
        _check_allowlist_is_live(used_allowlist)

    for note in notices:
        print(f"  allowlisted: {note}")

    if _errors:
        print(f"\n{len(_errors)} env-forwarding parity failure(s) between "
              f"{student_rel} and {opi_rel}.")
        print("A key on one target only is invisible to env-forwarding-guard, "
              "which unions every compose file — that is how the Orange Pi "
              "shipped without EDUBOTICS_ROBOT_TYPE on open_manipulator.")
        return 1

    total = sum(len(v) for v in student.values())
    print(f"OK: {student_rel} and {opi_rel} agree per service on all "
          f"{total} student env keys "
          f"({len(INTENTIONAL)} allowlisted divergence(s)).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
