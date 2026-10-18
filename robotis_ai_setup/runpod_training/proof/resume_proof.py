"""Resume (R3) and dataset-revision (E17) proofs, run in the CPU proof image (Dockerfile.proof).

    python /app/proof/resume_proof.py resume --work /work            # R3, offline
    python /app/proof/resume_proof.py revision-fresh --work /work    # E17, public Hub, no token
    python /app/proof/resume_proof.py revision-resume --work /work   #      (a second container, empty cache)
    python /app/proof/resume_proof.py revision-negative --work /work
    python /app/proof/resume_proof.py revision-versions --work /work
    python /app/proof/resume_proof.py revision-verdict --work /work

Everything that matters is the PRODUCT's code from training_handler.py:
`_fresh_args` (= `_build_training_command` + `--dataset.revision` + `--num_workers`),
`_regenerate_train_config`, and `_prepare_resume`, which downloads the checkpoint
over HTTP (here from a local http.server instead of R2), checks every sha256,
validates the file list / processor steps / training_step, compares the host's
train_config.json with the regenerated one and overwrites it, moves it into
place and returns LeRobot's own resume command. Two harness substitutions only:
TRAIN_DEVICE = "cpu" and, for the offline R3 runs, `--dataset.root=<local>`.
`--steps`/`--save_freq` come from this file's own table (the proof calls LeRobot
directly, not through the API's 2,500 rule).

Each mode prints ONE JSON line; `resume` and `revision-verdict` exit 0 iff it holds.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, "/app")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import training_handler as th  # noqa: E402

import compare  # noqa: E402

th.TRAIN_DEVICE = "cpu"   # harness substitution 1

MODEL = "EduBotics-Solutions/e2e-act"
SYNTH_DATASET = "edubotics-e2e/synthetic"
PUSHT = "lerobot/pusht"
PUSHT_V30_SHA = "b1c3ecbae7f244acc039a3dbc255a00dad1372b9"    # the pusht v3.0 tag's commit
PUSHT_V21_SHA = "6e1d9c95aaf8abb19342e723935b6af7c54b5903"    # the pusht v2.1 tag's commit
VERSION_PROBES = {   # sha -> is_valid_version expected (§4.1 step 5's PEP 440 fallback)
    "b1c3ecbae7f244acc039a3dbc255a00dad1372b9": False,
    "6e1d9c95aaf8abb19342e723935b6af7c54b5903": False,
    "7628202a2180972f291ba1bc6723834921e72c19": False,
    "1234567a8901234567890123456789012345678f": False,
    "1234567890123456789a12345678901234567890": True,
    "v3.0": True,
}
OUT = th._output_path(MODEL)


def emit(obj: dict) -> None:
    print(json.dumps(obj, sort_keys=True), flush=True)


# ---------------------------------------------------------------- runs
def fresh_cmd(dataset: str, steps: int, save_freq: int, num_workers: int,
              revision: str | None = None, root: str | None = None) -> list[str]:
    spec = {
        "dataset_name": dataset,
        "model_name": MODEL,
        "training_params": {"seed": 1000, "batch_size": 8, "steps": steps, "log_freq": 1,
                            "save_freq": save_freq},
        "dataset_revision": revision,
    }
    cmd = th._fresh_args(spec, num_workers)                    # the product's fresh command
    if root:
        cmd.append(f"--dataset.root={root}")                  # harness substitution 2
    return cmd


def run_lerobot(cmd: list[str], log: Path, kill_after_ckpt: str | None = None) -> int:
    """Run LeRobot; with kill_after_ckpt, SIGKILL it once checkpoints/last names that id."""
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "w", encoding="utf-8") as f:
        proc = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT, env=th._lerobot_env())
        if kill_after_ckpt is None:
            return proc.wait()
        last = OUT / "checkpoints" / "last"
        while proc.poll() is None:
            try:
                if os.readlink(last) == kill_after_ckpt:
                    proc.kill()
                    break
            except OSError:
                pass
            time.sleep(0.05)
        return proc.wait()


def keep(work: Path, name: str) -> Path:
    dst = work / "runs" / name
    if dst.exists():
        shutil.rmtree(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(OUT, dst, symlinks=True)
    return dst


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def transport(src_ckpt: Path, step: int, total_steps: int, fresh: list[str],
              num_workers: int) -> list[str]:
    """Hand src_ckpt to the PRODUCT's _prepare_resume through HTTP, exactly as from R2."""
    files = {}
    for rel in th.CHECKPOINT_RELPATHS:
        meta = th._hash_file(src_ckpt / rel)
        files[rel] = {"size": meta["size"], "sha256": meta["sha256"]}
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(_Quiet, directory=str(src_ckpt)))
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        port = server.server_address[1]
        for rel in files:
            files[rel]["url"] = f"http://127.0.0.1:{port}/{rel}"
        resume = {"step": step, "step_id": th.step_id(step, total_steps), "attempt_no": 1,
                  "files": files}
        client = th._http_client()
        try:
            return th._prepare_resume(resume, fresh, num_workers, MODEL, total_steps, client)
        finally:
            client.close()
    finally:
        server.shutdown()


def json_load(p: Path):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def config_equals_policy(pm: Path) -> bool:
    return json_load(pm / "config.json") == json_load(pm / "train_config.json")["policy"]


def regenerate_now(fresh: list[str]) -> dict:
    if OUT.exists():
        shutil.rmtree(OUT)
    return th._regenerate_train_config(fresh[3:])


def tensors_equal(m: dict) -> bool:
    return bool(m["keys_equal"] and m["differing_tensors"] == 0 and m["file_sha_equal"])


# ---------------------------------------------------------------- R3
def mode_resume(work: Path) -> int:
    ds = work / "ds"
    if not (ds / "meta" / "info.json").is_file():
        if ds.exists():
            shutil.rmtree(ds)
        subprocess.run([sys.executable, str(Path(__file__).with_name("make_dataset.py")), str(ds)],
                       check=True, capture_output=True)
    logs = work / "logs"
    rcs, checks, facts = {}, {}, {}

    def fresh(steps, sf, nw):
        return fresh_cmd(SYNTH_DATASET, steps, sf, nw, root=str(ds))

    def full_run(name, steps, sf, nw):
        rcs[name] = run_lerobot(fresh(steps, sf, nw), logs / f"{name}.log")
        return keep(work, name)

    a1 = full_run("A1", 20, 10, 0)
    a2 = full_run("A2", 20, 10, 0)
    anw2 = full_run("Anw2", 20, 10, 2)
    asf = full_run("Asf", 20, 20, 0)

    # B: SIGKILL once checkpoint 000010 is complete, resume through the product (mid-epoch).
    rcs["Bfresh"] = run_lerobot(fresh(20, 10, 0), logs / "Bfresh.log", kill_after_ckpt="000010")
    bfresh = keep(work, "Bfresh")
    b_cmd = transport(bfresh / "checkpoints" / "000010", 10, 20, fresh(20, 10, 0), 0)
    rcs["B"] = run_lerobot(b_cmd, logs / "B.log")
    b = keep(work, "B")

    # E / EB: an epoch boundary (120 frames / batch 8 = 15 steps), resumed with 0 and 2 workers.
    e = full_run("E", 30, 15, 0)
    eb = {}
    for nw in (0, 2):
        cmd = transport(e / "checkpoints" / "000015", 15, 30, fresh(30, 15, 0), nw)
        rcs[f"EB{nw}"] = run_lerobot(cmd, logs / f"EB{nw}.log")
        eb[nw] = keep(work, f"EB{nw}")

    checks["every_run_exited_0"] = all(rc == 0 for name, rc in rcs.items() if name != "Bfresh")
    checks["bfresh_was_killed"] = rcs["Bfresh"] != 0 and (bfresh / "checkpoints" / "000010").is_dir()
    pm = lambda run, sid: run / "checkpoints" / sid / "pretrained_model"   # noqa: E731
    la1 = compare.losses(logs / "A1.log")

    def same(x, y, name, lx, ly):
        m = compare.compare_models(pm(x, "000020"), pm(y, "000020"))
        lo = compare.compare_losses(lx, ly)
        facts[name] = {**m, **lo}
        return tensors_equal(m) and not lo["differing_loss_steps"] and lo["logged_steps_compared"] >= 20

    checks["A1_equals_A2"] = same(a1, a2, "A1_vs_A2", la1, compare.losses(logs / "A2.log"))
    checks["A1_equals_A_num_workers_2"] = same(a1, anw2, "A1_vs_Anw2", la1, compare.losses(logs / "Anw2.log"))
    checks["A1_equals_A_save_freq_20"] = same(a1, asf, "A1_vs_Asf", la1, compare.losses(logs / "Asf.log"))

    lb = compare.losses(logs / "Bfresh.log")
    lb = {s: v for s, v in lb.items() if s <= 10}
    lb.update(compare.losses(logs / "B.log"))
    mb = compare.compare_models(pm(a1, "000020"), pm(b, "000020"))
    lob = compare.compare_losses(la1, lb)
    facts["A1_vs_B_midepoch"] = {**mb, **lob}
    checks["B_resumed_to_20"] = (pm(b, "000020") / "model.safetensors").is_file()
    checks["B_losses_before_the_kill_equal"] = all(s > 10 for s in lob["differing_loss_steps"])
    facts["B_differs_from_A1_as_documented"] = not tensors_equal(mb)   # E10: expected, not required

    le = compare.losses(logs / "E.log")
    for nw in (0, 2):
        m = compare.compare_models(pm(e, "000030"), pm(eb[nw], "000030"))
        lo = compare.compare_losses(le, compare.losses(logs / f"EB{nw}.log"))
        facts[f"E_vs_EB{nw}"] = {**m, **lo}
        checks[f"E_equals_EB_num_workers_{nw}"] = (
            tensors_equal(m) and not lo["differing_loss_steps"] and lo["logged_steps_compared"] >= 15)

    finals = [pm(a1, "000020"), pm(a2, "000020"), pm(anw2, "000020"), pm(asf, "000020"),
              pm(b, "000020"), pm(e, "000030"), pm(eb[0], "000030"), pm(eb[2], "000030")]
    checks["config_json_equals_train_config_policy"] = all(config_equals_policy(p) for p in finals)

    # The regenerated config equals LeRobot's own exactly (fresh) / after the 4 keys (resumed).
    ref20 = regenerate_now(fresh(20, 10, 0))
    checks["regenerate_equals_fresh_000010"] = ref20 == json_load(pm(a1, "000010") / "train_config.json")
    checks["regenerate_equals_fresh_000020"] = ref20 == json_load(pm(a1, "000020") / "train_config.json")
    checks["regenerate_equals_resumed_000020_normalised"] = (
        th._normalise_train_config(ref20)
        == th._normalise_train_config(json_load(pm(b, "000020") / "train_config.json")))

    checks.update(negatives(work, a1))
    ok = all(checks.values())
    emit({"proof": "resume", "verdict": "pass" if ok else "fail", "checks": checks,
          "returncodes": rcs, "facts": facts})
    return 0 if ok else 1


def negatives(work: Path, a1: Path) -> dict:
    """The product refuses a tampered hand-out with the right ckpt-bad reason."""
    out = {}
    src = a1 / "checkpoints" / "000010"
    fresh = fresh_cmd(SYNTH_DATASET, 20, 10, 0, root=str(work / "ds"))

    def expect(name, mutate, reason):
        tmp = work / "neg" / name
        if tmp.exists():
            shutil.rmtree(tmp)
        shutil.copytree(src, tmp)
        mutate(tmp)
        try:
            transport(tmp, 10, 20, fresh, 0)
            out[f"refuses_{name}"] = False
        except th._CkptBad as bad:
            out[f"refuses_{name}"] = bad.reason == reason
        except Exception:  # noqa: BLE001
            out[f"refuses_{name}"] = False

    def class_step(d):
        p = d / "pretrained_model" / "policy_preprocessor.json"
        doc = json_load(p)
        doc["steps"][0]["class"] = "os.system"
        p.write_text(json.dumps(doc))

    def groot(d):
        p = d / "pretrained_model" / "train_config.json"
        doc = json_load(p)
        doc["policy"]["type"] = "groot"
        p.write_text(json.dumps(doc))

    def wrong_step(d):
        (d / "training_state" / "training_step.json").write_text('{"step": 11}')

    expect("class_step", class_step, "steps")
    expect("train_config_policy_type", groot, "train_config")
    expect("training_step", wrong_step, "training_step")

    # The hand-out lists the 11 files, but the served bytes of one differ: sha256.
    tmp = work / "neg" / "sha"
    if tmp.exists():
        shutil.rmtree(tmp)
    shutil.copytree(src, tmp)
    files = {rel: th._hash_file(tmp / rel) for rel in th.CHECKPOINT_RELPATHS}
    with open(tmp / "pretrained_model" / "model.safetensors", "r+b") as f:
        f.seek(100)
        byte = f.read(1)
        f.seek(100)
        f.write(bytes([byte[0] ^ 0xFF]))
    out["refuses_sha256_mismatch"] = _expect_sha_refusal(tmp, files, fresh)
    return out


def _expect_sha_refusal(tmp: Path, files: dict, fresh: list[str]) -> bool:
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(_Quiet, directory=str(tmp)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        handed = {rel: {"size": m["size"], "sha256": m["sha256"],
                        "url": f"http://127.0.0.1:{port}/{rel}"} for rel, m in files.items()}
        client = th._http_client()
        try:
            th._prepare_resume({"step": 10, "step_id": "000010", "attempt_no": 1, "files": handed},
                               fresh, 0, MODEL, 20, client)
            return False
        except th._CkptBad as bad:
            return bad.reason == "sha256"
        finally:
            client.close()
    finally:
        server.shutdown()


# ---------------------------------------------------------------- E17
def _snapshot_dirs(hf_home: Path, sha: str) -> list[str]:
    return sorted(str(p) for p in hf_home.rglob(sha) if p.is_dir() and p.parent.name == "snapshots")


def _hf_home() -> Path:
    return Path(os.environ.get("HF_HOME", str(Path.home() / ".cache" / "huggingface")))


def mode_revision_fresh(work: Path) -> int:
    e17 = work / "e17"
    e17.mkdir(parents=True, exist_ok=True)
    cmd = fresh_cmd(PUSHT, 20, 10, 0, revision=PUSHT_V30_SHA)
    checks = {"command_carries_the_revision": f"--dataset.revision={PUSHT_V30_SHA}" in cmd}
    rc = run_lerobot(cmd, e17 / "fresh.log")
    checks["fresh_exited_0"] = rc == 0
    out = keep(work, "e17_fresh")
    pm = lambda sid: out / "checkpoints" / sid / "pretrained_model"   # noqa: E731
    checks["fresh_reached_20"] = (pm("000020") / "model.safetensors").is_file()
    snaps = _snapshot_dirs(_hf_home(), PUSHT_V30_SHA)
    checks["downloaded_into_the_commit_snapshot"] = bool(snaps)
    if checks["fresh_reached_20"]:
        tc = json_load(pm("000020") / "train_config.json")
        checks["train_config_pins_the_commit"] = tc["dataset"]["revision"] == PUSHT_V30_SHA
        ref = regenerate_now(cmd)
        checks["regenerate_equals_000010"] = ref == json_load(pm("000010") / "train_config.json")
        checks["regenerate_equals_000020"] = ref == json_load(pm("000020") / "train_config.json")
        checks["config_equals_policy"] = config_equals_policy(pm("000010")) and config_equals_policy(pm("000020"))
    result = {"proof": "revision-fresh", "ok": all(checks.values()), "checks": checks,
              "snapshots": snaps, "returncode": rc}
    (e17 / "fresh.json").write_text(json.dumps(result, sort_keys=True))
    emit(result)
    return 0 if result["ok"] else 1


def mode_revision_resume(work: Path) -> int:
    e17 = work / "e17"
    src = work / "runs" / "e17_fresh" / "checkpoints" / "000010"
    fresh = fresh_cmd(PUSHT, 20, 10, 0, revision=PUSHT_V30_SHA)
    hf = _hf_home()
    checks = {"cache_started_empty": not any(hf.rglob("*")) if hf.exists() else True}
    cmd = transport(src, 10, 20, fresh, 0)
    rc = run_lerobot(cmd, e17 / "resume.log")
    checks["resume_exited_0"] = rc == 0
    out = keep(work, "e17_resume")
    pm = out / "checkpoints" / "000020" / "pretrained_model"
    checks["resume_reached_20"] = (pm / "model.safetensors").is_file()
    snaps = _snapshot_dirs(hf, PUSHT_V30_SHA)
    checks["downloaded_the_same_commit_again"] = bool(snaps)
    if checks["resume_reached_20"]:
        tc = json_load(pm / "train_config.json")
        checks["train_config_still_pins_the_commit"] = tc["dataset"]["revision"] == PUSHT_V30_SHA
        ref = regenerate_now(fresh)
        checks["regenerate_equals_resumed_000020_normalised"] = (
            th._normalise_train_config(ref) == th._normalise_train_config(tc))
        checks["config_equals_policy"] = config_equals_policy(pm)
    result = {"proof": "revision-resume", "ok": all(checks.values()), "checks": checks,
              "snapshots": snaps, "returncode": rc}
    (e17 / "resume.json").write_text(json.dumps(result, sort_keys=True))
    emit(result)
    return 0 if result["ok"] else 1


def mode_revision_negative(work: Path) -> int:
    e17 = work / "e17"
    e17.mkdir(parents=True, exist_ok=True)
    cmd = fresh_cmd(PUSHT, 20, 10, 0, revision=PUSHT_V21_SHA)
    rc = run_lerobot(cmd, e17 / "negative.log")
    text = (e17 / "negative.log").read_text(encoding="utf-8", errors="replace")
    checks = {"refused_nonzero_exit": rc != 0,
              "refused_with_BackwardCompatibilityError": "BackwardCompatibilityError" in text}
    result = {"proof": "revision-negative", "ok": all(checks.values()), "checks": checks,
              "returncode": rc}
    (e17 / "negative.json").write_text(json.dumps(result, sort_keys=True))
    emit(result)
    return 0 if result["ok"] else 1


def mode_revision_versions(work: Path) -> int:
    from lerobot.datasets.utils import is_valid_version
    e17 = work / "e17"
    e17.mkdir(parents=True, exist_ok=True)
    got = {s: bool(is_valid_version(s)) for s in VERSION_PROBES}
    checks = {f"is_valid_version({s})=={want}": got[s] == want for s, want in VERSION_PROBES.items()}
    result = {"proof": "revision-versions", "ok": all(checks.values()), "checks": checks}
    (e17 / "versions.json").write_text(json.dumps(result, sort_keys=True))
    emit(result)
    return 0 if result["ok"] else 1


def mode_revision_verdict(work: Path) -> int:
    parts, ok = {}, True
    for name in ("versions", "fresh", "resume", "negative"):
        p = work / "e17" / f"{name}.json"
        r = json_load(p) if p.is_file() else {"ok": False, "missing": True}
        parts[name] = r
        ok = ok and bool(r.get("ok"))
    emit({"proof": "revision", "verdict": "pass" if ok else "fail",
          "checks": {n: bool(r.get("ok")) for n, r in parts.items()},
          "details": {n: r.get("checks") for n, r in parts.items()}})
    return 0 if ok else 1


MODES = {
    "resume": mode_resume,
    "revision-fresh": mode_revision_fresh,
    "revision-resume": mode_revision_resume,
    "revision-negative": mode_revision_negative,
    "revision-versions": mode_revision_versions,
    "revision-verdict": mode_revision_verdict,
}


def main(argv) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=sorted(MODES))
    ap.add_argument("--work", default="/work")
    a = ap.parse_args(argv)
    work = Path(a.work)
    work.mkdir(parents=True, exist_ok=True)
    return MODES[a.mode](work)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
