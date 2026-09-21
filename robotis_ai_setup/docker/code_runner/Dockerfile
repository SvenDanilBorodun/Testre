# code-runner — the sandbox that runs a student's Python or Java program
# beside physical_ai_server (Roboter Studio text programs). One image per
# platform: `code-runner` (amd64, the student PC) and `code-runner-opi`
# (arm64, the Orange Pi); no Jetson flavour (the Roboter-Studio tab is
# jetsonIncompatible).
#
# A PUBLIC base, deliberately: no self-built base, so nothing here joins
# Rule §6's manual base-rebuild set. python3 comes from noble's apt (3.12.3,
# the PEP 669 sys.monitoring floor for the debugger); there is no pip
# install and no network fetch besides that one apt-get.
#
# The sandbox is the COMPOSE SERVICE's (docker-compose.yml::code_runner):
# `user: "0:0"`, `cap_drop: ALL` + SETUID/SETGID/KILL, `read_only`,
# `network_mode: none`, `init: true`, the /work + /tmp tmpfs, the ipc volume.
# There is no USER instruction here on purpose — the supervisor is root with
# those three capabilities and every student process runs as uid 10001
# (created below). The tree ships to /opt/edubotics/runner exactly as it is
# in the repo (image_source_parity's third kind diffs it), plus the compiled
# java-classes/ that the build adds.
#
# Build-time gates, each of which fails the build: python >= 3.12 and
# hasattr(sys, 'monitoring') — an attribute, never `import sys.monitoring`;
# the Java tree compiles with -Xlint:all -Werror and its SmokeMain runs; every
# shipped .py compiles on the shipped interpreter; the Python stub and the
# debugger hook import under -I; the self-test's build rungs pass (uid/caps
# with the three EPERMs and the /proc sweep, the mid-run breakpoint, the
# rate floor, the Java smoke).
#
# The full self-test (the other:runner-sandbox gate) needs the sandbox flags:
#   docker run --rm --init --user 0:0 --cap-drop ALL --cap-add SETUID \
#     --cap-add SETGID --cap-add KILL --security-opt no-new-privileges:true \
#     --read-only --network none --tmpfs /work:size=64m,mode=1777 \
#     --tmpfs /tmp:size=16m,mode=1777 --tmpfs /run/edubotics/code \
#     --pids-limit 128 code-runner:probe python3 /opt/edubotics/runner/selftest.py
FROM eclipse-temurin:21-jdk-noble

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 \
 && rm -rf /var/lib/apt/lists/* \
 && python3 -c "import sys; assert sys.version_info >= (3, 12), sys.version; assert hasattr(sys, 'monitoring'), 'no sys.monitoring'" \
 && groupadd -g 10001 runner \
 && useradd -u 10001 -g 10001 -M -s /usr/sbin/nologin runner

COPY runner/ /opt/edubotics/runner/

RUN javac -Xlint:all -Werror -encoding UTF-8 -d /opt/edubotics/runner/java-classes \
        $(find /opt/edubotics/runner/java -name '*.java') \
 && java -cp /opt/edubotics/runner/java-classes edubotics.SmokeMain | grep -q 'JAVA SMOKE OK' \
 && python3 -m compileall -q /opt/edubotics/runner \
 && find /opt/edubotics/runner -name __pycache__ -type d -exec rm -rf {} + \
 && (cd /opt/edubotics/runner/lib && python3 -I -c "import sys; sys.path.insert(0, '.'); import robot, edubotics_debug") \
 && python3 /opt/edubotics/runner/selftest.py --build

CMD ["python3", "/opt/edubotics/runner/supervisor.py"]
