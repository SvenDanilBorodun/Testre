#!/usr/bin/env python3
r"""Print `pi_agent/constants.py::SHIPPED_COMPOSE_RELPATH` — the ONE declaration
of where the agent looks for the opi compose twin it repairs a fielded Pi onto.

Two CI/release gates fence that twin, and both need the path:

  * `ci.yml::pi-compose-twin-guard` — `cmp -s` the twin against the source of
    truth (`robotis_ai_setup/docker/docker-compose.opi.yml`), on every push.
  * `release.yml::pi-agent-tarball` — the same equality, on the built ARTIFACT,
    which is what actually ships.

Both used to restate the path as a shell literal. `release.yml`'s own comment
already says why that is wrong, about the two constants beside this one: "never
restated: a second hardcoded list is a second thing to keep in sync, and a name
drifting out of sync is precisely the failure this gate exists to catch." The
agent RESOLVES the twin off this constant (`agent.py::_shipped_compose_path`),
so a gate checking some other path is green and useless — it would fence a file
nothing reads while the file the agent does read ships unchecked.

Reads the module SOURCE, never imports it: no VERSION walk, no env lookup, no
way for the runner's environment to change the answer. Accepts a path argument
or `-` for stdin, so the same parser serves the source tree (ci.yml) and the
packaged copy inside the tarball (release.yml, via `tar -xzO … | … -`).

Prints the relpath in POSIX form on stdout — tar members are always `/`-joined,
and so is every use here. Diagnostics go to stderr as `::error::`, so stdout
stays clean enough to capture.
"""

from __future__ import annotations

import ast
import posixpath
import sys

CONST = "SHIPPED_COMPOSE_RELPATH"


def die(msg: str) -> "None":
    print(f"::error::{msg}", file=sys.stderr)
    raise SystemExit(1)


def _literal_parts(node: ast.AST) -> "list":
    """The string parts of `SHIPPED_COMPOSE_RELPATH`'s right-hand side.

    Two forms are accepted, and no others. A plain string literal
    (`"docker/docker-compose.opi.yml"`), and the `os.path.join("docker",
    "docker-compose.opi.yml")` form the constant actually uses today — kept
    parseable rather than flattened in the source, because flattening it would
    be a (tiny) runtime change to a file that ships to every Pi, made only to
    please a CI gate.

    Anything else — an f-string, a name, a concatenation with a variable — is
    refused rather than guessed at. This gate exists because a silently wrong
    path is worse than no path: it would fence a file nothing reads.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if (isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "join"
            and not node.keywords):
        parts = []
        for arg in node.args:
            if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
                return []
            parts.append(arg.value)
        return parts
    return []


def relpath_from_source(source: str, origin: str) -> str:
    for node in ast.parse(source).body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == CONST:
                parts = _literal_parts(node.value)
                if not parts:
                    die(f"{CONST} in {origin} is neither a string literal nor an "
                        "os.path.join() of string literals — this gate reads it "
                        "statically, so keep it one of those two forms")
                # posixpath.join, not "/".join: it is what `os.path.join`
                # actually does on the Pi, and its absolute-resets-the-path rule
                # is load-bearing here. A naive per-part strip("/") would turn
                # "/etc/x.yml" into the innocent-looking relative "etc/x.yml" and
                # pass — caught by test_it_refuses_rather_than_guesses while this
                # script was being written.
                relpath = posixpath.join(*parts)
                if (not relpath or posixpath.isabs(relpath)
                        or ".." in relpath.split("/")):
                    die(f"{CONST} in {origin} is {relpath!r} — it must be a "
                        "relative path inside the pi_agent package")
                return relpath
    die(f"{CONST} not found in {origin} — the agent resolves the compose twin "
        "off that constant (agent.py::_shipped_compose_path), so without it "
        "this gate cannot know which file to check and the twin would ship "
        "unfenced")
    raise AssertionError("unreachable")  # die() exits; keeps linters honest


def main(argv: "list") -> int:
    if len(argv) != 2:
        die("usage: pi_shipped_compose_relpath.py <path/to/constants.py>|-")
    origin = argv[1]
    if origin == "-":
        source, origin = sys.stdin.read(), "the packaged pi_agent/constants.py"
    else:
        try:
            with open(origin, "r", encoding="utf-8") as f:
                source = f.read()
        except OSError as e:
            die(f"cannot read {origin}: {e}")
    try:
        relpath = relpath_from_source(source, origin)
    except SyntaxError as e:
        die(f"{origin} does not parse as Python: {e}")
        raise AssertionError("unreachable")
    print(relpath)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
