"""Constants of the code-runner sandbox — the runner-side twin of the server's
policy numbers, plus the numbers that exist only here.

Two kinds of value live in this file, and a deps-free test tells them apart:

* TWINS of ``physical_ai_server/workflow/robot_api.py`` (``RpcLimits``, the
  code-project caps, ``RUN_TOKEN_RE``, ``PAUSED_MAX_LOCALS``) and of
  ``workflow/code_rpc.py`` (the two stdout bounds). The runner cannot import
  the server package (a different image, a different interpreter), so the
  numbers are re-spelled and ``tests/test_code_runner_limits_lockstep.py``
  AST-compares every one of them. Change the server, and that test names the
  line here that has to move.
* The sandbox's OWN numbers (uid, socket modes, rlimits, paths, the kill
  sweep), pinned by ``tests/test_code_runner_supervisor.py``.

Nothing in this tree reads an environment variable of the product: the
``code_runner`` compose service carries no ``environment:`` block at all, so
a read here would be dead code with a live-looking name.
"""

from __future__ import annotations

import resource

# ── RpcLimits twin (robot_api.RPC_LIMITS) ──────────────────────────────────
MAX_CALLS_PER_S = 200.0
BURST = 50
PERCEPTION_MAX_PER_S = 20.0
PERCEPTION_BURST = 5
MAX_FRAME_BYTES = 65536
CONTROL_MAX_FRAME_BYTES = 196608

# ── code-project caps twin (robot_api.CODE_PROJECT_LIMITS) ─────────────────
MAX_CODE_FILES = 32
MAX_CODE_FILE_BYTES = 65536
MAX_CODE_PROJECT_BYTES = 131072
CODE_PATH_RE = (
    r'^(?:[A-Za-z][A-Za-z0-9_]{0,39}/){0,3}[A-Za-z][A-Za-z0-9_]{0,39}\.(py|java)$'
)
RUN_TOKEN_RE = r'^[0-9a-f]{32}$'
PAUSED_MAX_LOCALS = 30

# ── stdout bounds twin (code_rpc.STDOUT_*) ─────────────────────────────────
STDOUT_MAX_LINE_BYTES = 2000
STDOUT_MAX_LINES_PER_S = 20

# ── the sandbox's own numbers ──────────────────────────────────────────────
# The student uid/gid. The supervisor is uid 0 with exactly SETUID, SETGID and
# KILL (the compose service drops everything else); every student process and
# every helper that touches the run directory is spawned as this uid.
STUDENT_UID = 10001
STUDENT_GID = 10001

# The ipc volume shared with physical_ai_server. ``rpc.sock`` (DATA) is
# created by the server and is 0666 so the student uid can connect;
# ``runner.sock`` (CONTROL) is created by the supervisor and is 0600 so only
# root — physical_ai_server, root in its container — can reach it.
IPC_DIR = '/run/edubotics/code'
CONTROL_SOCKET_NAME = 'runner.sock'
DATA_SOCKET_NAME = 'rpc.sock'
CONTROL_SOCKET_MODE = 0o600
DATA_SOCKET_MODE = 0o666

# Per-run project directories live on the /work tmpfs (compose:
# ``size=64m,mode=1777``). The supervisor never writes there itself: a
# uid-10001 helper stages and removes them (root without DAC_OVERRIDE cannot
# unlink inside a student-owned directory; the student uid can).
WORK_DIR = '/work'
RUN_DIR_MODE = 0o755

# The shipped tree and the two interpreters, by ABSOLUTE path: the student
# process receives exactly two environment variables (below) and therefore no
# PATH, so a bare ``java`` would never resolve.
RUNNER_ROOT = '/opt/edubotics/runner'
JAVA_CLASSES_DIR = RUNNER_ROOT + '/java-classes'
PYTHON_BIN = '/usr/bin/python3'
JAVA_HOME = '/opt/java/openjdk'
JAVA_BIN = JAVA_HOME + '/bin/java'
JAVAC_BIN = JAVA_HOME + '/bin/javac'

# The only two names in a student process's environment.
STUDENT_ENV_NAMES = ('CODE_RPC_SOCKET', 'CODE_RUN_TOKEN')

# JVM bounds. ``-Xmx256m`` is the heap; the three reservation caps are what
# make a bounded RLIMIT_AS possible at all — a stock JVM reserves 1 GiB of
# compressed-class space and 240 MiB of code cache up front and refuses to
# start under any address-space limit below ~1.3 GiB (measured 2026-09-21 on
# eclipse-temurin:21-jdk-noble, arm64). SerialGC + a single compiler thread
# keep the task count at 12, inside RLIMIT_NPROC.
JAVA_FLAGS = (
    '-Xmx256m', '-Xss1m',
    '-XX:+UseSerialGC', '-XX:TieredStopAtLevel=1', '-XX:CICompilerCount=1',
    '-XX:CompressedClassSpaceSize=64m', '-XX:ReservedCodeCacheSize=32m',
    '-XX:MaxMetaspaceSize=96m', '-XX:-UsePerfData',
)

# Resource limits applied by every student-side process to ITSELF before any
# student byte runs (``apply_rlimits``), never by a ``preexec_fn`` — the
# supervisor is multithreaded and CPython documents ``preexec_fn`` as unsafe
# in that case. Soft == hard, so an unprivileged process cannot raise them
# back. RLIMIT_AS differs per language: the spec's 512 MiB kills a JVM (the
# measurement above); 1 GiB leaves the capped JVM 256 MiB of headroom for
# glibc arenas.
_MIB = 1024 * 1024
RLIMIT_AS_BYTES = {'python': 512 * _MIB, 'java': 1024 * _MIB}
RLIMIT_NPROC = 32
RLIMIT_FSIZE_BYTES = 32 * _MIB
RLIMIT_NOFILE = 64
RLIMIT_CORE = 0

# Kill sequence: killpg → sweep /proc for the student uid, this many rounds
# this far apart → waitpid. A ``setsid()`` escapee is what the sweep is for.
KILL_SWEEP_ROUNDS = 3
KILL_SWEEP_INTERVAL_S = 0.1

# One start at a time; a start arriving while the previous run is still being
# torn down waits this long for the slot before answering ``busy``.
BUSY_WAIT_S = 1.0
# javac on a student project; the bound is generous, the tmpfs is small.
COMPILE_TIMEOUT_S = 60.0
# A helper (stage/remove) that has not finished in this long is broken.
HELPER_TIMEOUT_S = 15.0
# The first control frame must arrive this soon after connect.
CONTROL_FIRST_FRAME_S = 2.0

# The breakpoint channel into a Python student process: a pipe whose read end
# is passed to the launcher (its fd number rides argv, not the environment).
# Each line is one JSON object ``{"lines": {"main.py": [12, 40]}}``.
BREAKPOINT_LINE_MAX_BYTES = 8192


def apply_rlimits(language: str) -> None:
    """Lower this process's own resource limits to the sandbox table.

    Called by ``student_main.py`` (Python), ``sandbox_exec.py`` (javac/java)
    and ``work_helper.py`` as their first action; a limit lowered here binds
    every child they exec or fork because soft and hard are set together."""
    limits = (
        (resource.RLIMIT_AS, RLIMIT_AS_BYTES[language]),
        (resource.RLIMIT_NPROC, RLIMIT_NPROC),
        (resource.RLIMIT_FSIZE, RLIMIT_FSIZE_BYTES),
        (resource.RLIMIT_NOFILE, RLIMIT_NOFILE),
        (resource.RLIMIT_CORE, RLIMIT_CORE),
    )
    for res, value in limits:
        resource.setrlimit(res, (value, value))
