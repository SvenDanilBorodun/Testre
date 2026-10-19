"""Shape of the RunPod trainer image and its CI/CD (spec §4.8, §4.12, impl-notes WP-B 1). Text tests.

* runpod_training/Dockerfile: the pinned base, the lock install, the strict check, the baked
  ResNet18 weights, exactly four files in /app, the ENV, the entry point, no secret.
* runpod_training/proof/Dockerfile.proof: the per-architecture CPU lock (executed here for both
  architectures), the original lock kept at the hashed path, install and check from the
  derived lock (otherwise every fake pod is refused as wrong_image).
* trainer-image.yml, release.yml (W2a/W2b, --skip-deploys: FR-10) and ci.yml (boto3: FR-11).
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from unittest import mock

import yaml

_SETUP = Path(__file__).resolve().parents[1]
_REPO = _SETUP.parent
TRAINER = _SETUP / "runpod_training" / "Dockerfile"
PROOF = _SETUP / "runpod_training" / "proof" / "Dockerfile.proof"
LOCK = _SETUP / "modal_training" / "training-lock.txt"
WF = _REPO / ".github" / "workflows"
BASE = "python:3.12.14-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9"
CHECKOUT = "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd"
FREE_DISK = "jlumbroso/free-disk-space@54081f138730dfa15788a46383842cd2f914a1be"
LOGIN = "docker/login-action@4907a6ddec9925e35a0a9e82d7399ccc52663121"


def _instructions(path: Path) -> list[tuple[str, str]]:
    """(INSTRUCTION, argument) with continuations joined; heredoc bodies are skipped."""
    out, lines, i = [], path.read_text(encoding="utf-8").splitlines(), 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        while line.endswith("\\") and i + 1 < len(lines):
            i += 1
            line = line[:-1] + " " + lines[i].strip()
        word, _, arg = line.strip().partition(" ")
        if "<<'" in arg:
            end = arg.split("<<'", 1)[1].split("'", 1)[0]
            while i + 1 < len(lines) and lines[i + 1].strip() != end:
                i += 1
            i += 1
        out.append((word.upper(), re.sub(r"\s+", " ", arg).strip()))
        i += 1
    return out


def _stages(path: Path) -> list[list[tuple[str, str]]]:
    stages: list = []
    for ins in _instructions(path):
        if ins[0] == "FROM":
            stages.append([])
        if stages:
            stages[-1].append(ins)
    return stages


def _workflow(name: str) -> dict:
    return yaml.safe_load((WF / name).read_text(encoding="utf-8"))


def _on(doc: dict) -> dict:
    return doc.get("on", doc.get(True))   # PyYAML reads the bare key `on` as True


def _runs(job: dict) -> str:
    return "\n".join(step.get("run", "") for step in job.get("steps", []))


class TrainerDockerfile(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.builder, cls.runtime = _stages(TRAINER)

    def test_both_stages_use_the_pinned_base(self):
        self.assertEqual(self.builder[0], ("FROM", f"{BASE} AS builder"))
        self.assertEqual(self.runtime[0], ("FROM", BASE))

    def test_the_builder_compiles_a_venv_from_the_lock_and_bakes_the_backbone(self):
        runs = [a for w, a in self.builder if w == "RUN"]
        self.assertTrue(any("apt-get install -y --no-install-recommends gcc g++ build-essential" in r for r in runs))
        self.assertIn("python -m venv /opt/venv", runs)
        self.assertIn("python /build/check_training_lock.py /build/training-lock.txt --strict", runs)
        self.assertTrue(any("ResNet18_Weights.IMAGENET1K_V1.get_state_dict" in r for r in runs))
        self.assertIn(("ENV", "TORCH_HOME=/opt/torch"), self.builder)

    def test_the_runtime_has_exactly_four_files_in_app_and_no_build_tools(self):
        copies = [a for w, a in self.runtime if w == "COPY"]
        self.assertEqual(copies, [
            "--from=builder /opt/venv /opt/venv",
            "--from=builder /opt/torch /opt/torch",
            "robotis_ai_setup/modal_training/training_handler.py /app/training_handler.py",
            "robotis_ai_setup/modal_training/check_training_lock.py /app/check_training_lock.py",
            "robotis_ai_setup/modal_training/training-lock.txt /app/training-lock.txt",
            "robotis_ai_setup/runpod_training/entry.py /app/entry.py",
        ])
        self.assertFalse(any("apt-get" in a for w, a in self.runtime if w == "RUN"))
        self.assertNotIn("harness_entry", TRAINER.read_text(encoding="utf-8"),
                         "the CPU harness never enters the trainer image")

    def test_the_runtime_env_check_and_entrypoint(self):
        self.assertIn(("ARG", "BUILD_COMMIT=unknown"), self.runtime)
        env = next(a for w, a in self.runtime if w == "ENV")
        for item in ("PATH=/opt/venv/bin:$PATH", "PYTHONUNBUFFERED=1", "TORCH_HOME=/opt/torch",
                     "HF_HUB_DISABLE_TELEMETRY=1", "PYTHONPATH=/app", "EDUBOTICS_BUILD=${BUILD_COMMIT}"):
            self.assertIn(item, env.split())
        self.assertIn(("RUN", "python /app/check_training_lock.py --strict"), self.runtime)
        self.assertEqual(self.runtime[-1], ("ENTRYPOINT", '["python", "-u", "/app/entry.py"]'))

    def test_no_secret_is_baked_in(self):
        for word, arg in _instructions(TRAINER):
            if word in ("ENV", "ARG"):
                self.assertNotRegex(arg.upper(), r"TOKEN|SECRET|PASSWORD|API_KEY|ANON_KEY", arg)


class ProofDockerfile(unittest.TestCase):
    TEXT = PROOF.read_text(encoding="utf-8")

    def _derive(self, machine: str) -> str:
        body = self.TEXT.split("RUN python - <<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
        written = {}

        class _File:
            def __init__(self, path, mode="r", encoding=None):
                self.path, self.mode = path, mode

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def __iter__(self):
                assert self.path == "/app/training-lock.txt", self.path
                return iter(LOCK.read_text(encoding="utf-8").splitlines(keepends=True))

            def write(self, text):
                written[self.path] = text

        import builtins
        import platform
        with mock.patch.object(platform, "machine", return_value=machine), \
                mock.patch.object(builtins, "open", _File), mock.patch.object(builtins, "print"):
            exec(compile(body, "Dockerfile.proof", "exec"), {"__name__": "__main__"})
        return written["/app/training-lock.cpu.txt"]

    def test_the_cpu_lock_per_architecture(self):
        lock = dict(ln.split("==") for ln in LOCK.read_text().splitlines() if ln and not ln.startswith("#"))
        for machine, vision in (("x86_64", "0.22.0+cpu"), ("aarch64", "0.22.0")):
            with self.subTest(machine=machine):
                derived = dict(ln.split("==") for ln in self._derive(machine).splitlines()
                               if ln and not ln.startswith("#"))
                self.assertEqual(derived["torch"], "2.7.0+cpu")
                self.assertEqual(derived["torchvision"], vision)
                self.assertFalse([n for n in derived if n.startswith("nvidia-") or n == "triton"])
                rest = {n: v for n, v in lock.items()
                        if not n.startswith("nvidia-") and n not in ("triton", "torch", "torchvision")}
                self.assertEqual({n: v for n, v in derived.items() if n not in ("torch", "torchvision")}, rest,
                                 "every other package keeps its exact version")

    def test_the_original_lock_stays_at_the_hashed_path(self):
        self.assertIn("COPY robotis_ai_setup/modal_training/training-lock.txt /app/training-lock.txt", self.TEXT)
        runs = [a for w, a in _instructions(PROOF) if w == "RUN"]
        self.assertIn("python -m pip install --no-cache-dir --no-deps -r /app/training-lock.cpu.txt "
                      "--index-url https://pypi.org/simple --extra-index-url https://download.pytorch.org/whl/cpu",
                      runs)
        self.assertIn("python /app/check_training_lock.py /app/training-lock.cpu.txt --strict", runs)
        self.assertFalse(any("-r /app/training-lock.txt" in r for r in runs))
        self.assertEqual(_instructions(PROOF)[0], ("FROM", BASE))
        self.assertEqual(_instructions(PROOF)[-1], ("ENTRYPOINT", '["python", "-u", "/app/harness_entry.py"]'))


class TrainerImageWorkflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = _workflow("trainer-image.yml")
        cls.text = (WF / "trainer-image.yml").read_text(encoding="utf-8")
        cls.job = cls.doc["jobs"]["build"]

    def test_triggers_and_outputs(self):
        on = _on(self.doc)
        self.assertIn("workflow_dispatch", on)
        self.assertEqual(on["workflow_call"]["inputs"]["version"]["type"], "string")
        self.assertTrue(on["workflow_call"]["inputs"]["version"]["required"])
        self.assertEqual(set(on["workflow_call"]["outputs"]), {"digest", "image"})

    def test_permissions_runner_timeout(self):
        self.assertEqual(self.doc["permissions"], {})
        self.assertEqual(self.job["permissions"], {"contents": "read", "packages": "write"})
        self.assertEqual(self.job["runs-on"], "ubuntu-latest")
        self.assertEqual(self.job["timeout-minutes"], 60)

    def test_pinned_actions_and_no_persisted_credentials(self):
        uses = [s["uses"].split("#")[0].strip() for s in self.job["steps"] if "uses" in s]
        self.assertEqual(uses, [CHECKOUT, FREE_DISK, LOGIN])
        checkout = next(s for s in self.job["steps"] if s.get("uses", "").startswith("actions/checkout"))
        self.assertIs(checkout["with"]["persist-credentials"], False)

    def test_no_expression_inside_any_run_block(self):
        for step in self.job["steps"]:
            self.assertNotIn("${{", step.get("run", ""), step.get("name"))

    def test_build_smoke_size_push_probe(self):
        runs = _runs(self.job)
        self.assertIn('docker build --pull --no-cache --build-arg BUILD_COMMIT="$GITHUB_SHA"', runs)
        self.assertIn("-f robotis_ai_setup/runpod_training/Dockerfile -t \"$IMG\" .", runs)
        self.assertIn("/app/check_training_lock.py --strict", runs)
        self.assertIn("ci_host_refusal.py", runs)
        self.assertIn("import lerobot.scripts.lerobot_train, lerobot.policies.act.modeling_act, av, cv2", runs)
        self.assertIn("-m lerobot.scripts.lerobot_train --help", runs)
        self.assertIn("docker image history --human=false", runs)
        self.assertIn("CEIL=$((12 * 1000 * 1000 * 1000))", runs)
        self.assertIn('docker push "$IMG"', runs)
        self.assertIn('echo "digest=${DIGEST}" >> "$GITHUB_OUTPUT"', runs)
        self.assertRegex(runs, r'MODE" = "release" \]; then\s+echo "::error::')
        self.assertIn("::warning::", runs)
        names = [s.get("name", "") for s in self.job["steps"]]
        order = [next(i for i, n in enumerate(names) if n.startswith(p))
                 for p in ("Build", "Smoke", "Size", "Push", "Anonymous")]
        self.assertEqual(order, sorted(order))

    def test_the_mode_comes_from_the_input_not_the_event_name(self):
        self.assertNotIn("github.event_name", self.text.split("jobs:", 1)[1])
        self.assertIn('TAG="test-${GITHUB_SHA}"', self.text)


class ReleaseWorkflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.jobs = _workflow("release.yml")["jobs"]

    def test_w2a_builds_the_trainer_image_with_the_version_file(self):
        job = self.jobs["trainer-image"]
        self.assertEqual(job["needs"], ["version-preflight"])
        self.assertEqual(job["uses"], "./.github/workflows/trainer-image.yml")
        self.assertEqual(job["with"]["version"], "${{ needs.version-preflight.outputs.version }}")
        self.assertEqual(job["permissions"], {"contents": "read", "packages": "write"})
        self.assertNotIn("secrets", job)
        self.assertIn("version", self.jobs["version-preflight"]["outputs"])

    def test_w2b_sets_the_digest_pinned_image_always_with_skip_deploys(self):
        job = self.jobs["trainer-image-var"]
        self.assertEqual(job["needs"], ["version-preflight", "trainer-image"])
        runs = _runs(job)
        sets = [ln for ln in runs.replace("\\\n", " ").splitlines() if "railway variable set" in ln]
        self.assertEqual(len(sets), 1)
        self.assertIn("RUNPOD_TRAINER_IMAGE=${IMAGE}", sets[0])
        self.assertIn("--skip-deploys", sets[0], "FR-10: never a deploy from this variable")
        self.assertIn('IMAGE="ghcr.io/svendanilborodun/edubotics-trainer:${VERSION}@${DIGEST}"', runs)
        step_env = next(s for s in job["steps"] if "railway variable set" in s.get("run", ""))["env"]
        self.assertEqual(step_env["DIGEST"], "${{ needs.trainer-image.outputs.digest }}")
        for step in job["steps"]:
            self.assertNotIn("${{", step.get("run", ""))

    def test_the_cloud_api_waits_for_both(self):
        job = self.jobs["cloud-api"]
        self.assertTrue({"trainer-image", "trainer-image-var"} <= set(job["needs"]))
        self.assertIn("needs.trainer-image-var.result == 'success'", job["if"])
        self.assertIn("needs.trainer-image.result == 'success'", job["if"])


class CiWorkflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.jobs = _workflow("ci.yml")["jobs"]

    def test_python_tests_install_boto3_and_botocore(self):
        runs = _runs(self.jobs["python-tests"]).replace("\\\n", " ")
        install = next(ln for ln in runs.splitlines() if "pip install pyyaml pytest" in ln)
        self.assertIn("boto3==1.43.107", install)
        self.assertIn("botocore==1.43.107", install)

    def test_runpod_training_is_a_compileall_root(self):
        self.assertIn("robotis_ai_setup/runpod_training", _runs(self.jobs["python-tests"]))

    def test_modal_import_validate_imports_both_training_variants(self):
        runs = _runs(self.jobs["modal-import-validate"])
        self.assertIn("for variant in prod dev", runs)
        self.assertIn('want="smoke_test,train,train_managed,train_vla"', runs)
        self.assertIn('want="smoke_test,train_managed"', runs)
        self.assertNotIn("registered_functions", runs, "deprecated in modal 1.6, removed in 1.7")
        self.assertIn("from app.services import training_dispatch, runpod_client", runs)
        self.assertIn("'boto3==1.43.107' 'botocore==1.43.107'", runs)


class EntryDefaults(unittest.TestCase):
    def test_entry_constants(self):
        sys.path.insert(0, str(_SETUP / "runpod_training"))
        try:
            import entry
        finally:
            sys.path.pop(0)
        self.assertIs(entry.EXIT_AFTER_FAILED_SELF_STOP, False)
        self.assertEqual(entry.WATCHDOG_GRACE_S, 1200)
        self.assertEqual(entry.MANAGED_ACT_CAP_S_DEFAULT, 14400)
        self.assertEqual(entry.POST_RUN_SLEEP_S, 120)
        self.assertEqual(entry.SELF_STOP_TRIES, 3)


if __name__ == "__main__":
    unittest.main()
