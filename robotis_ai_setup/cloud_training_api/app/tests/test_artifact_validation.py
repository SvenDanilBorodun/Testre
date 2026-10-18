"""artifact_validation: what a managed ACT checkpoint and dataset must look like (spec §3.5, FR-5).

The fixtures under ``fixtures/managed_training/`` are REAL files written by
``lerobot==0.5.1`` (the architect's CPU proof runs, spec Appendix A):

* ``omx_000010/`` — a fresh run on the OMX-shaped synthetic dataset
  (``observation.images.gripper`` + ``scene``, 6 joints), checkpoint 10 of 20.
  The proof ran with ``--dataset.root=/work/ds`` (an offline harness); a
  managed training never sets it, so the builder below sets ``dataset.root``
  to null — the one substitution, everything else is byte-for-byte LeRobot.
* ``pusht_resumed_000020/`` — the E17 run: ``lerobot/pusht`` pinned to its
  ``v3.0`` commit, resumed from 000010 (``resume: true``, ``checkpoint_path``
  and ``policy.pretrained_path`` set by LeRobot).
* ``datasets/`` — the two datasets' ``meta/info.json`` and the ``config.json``
  LeRobot built from each (the FR-5 feature-map equality).

Every §3.5 rule is mutated once and must refuse. This module is imported
before ``test_dataset_identity_routes`` alphabetically, so it imports nothing
but the stdlib module under test.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import unittest

from app.services import artifact_validation as AV

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures", "managed_training")
SHA = "b1c3ecbae7f244acc039a3dbc255a00dad1372b9"
OMX_DATASET = "edubotics-e2e/synthetic"
OMX_MODEL = "EduBotics-Solutions/e2e-act"
OMX_PARAMS = {"steps": 20, "save_freq": 10, "log_freq": 1, "seed": 1000, "batch_size": 8}


def _load(*parts):
    with open(os.path.join(FIX, *parts), encoding="utf-8") as fh:
        return json.load(fh)


def omx_docs(step=10) -> dict:
    """The six JSON files of the real 000010 checkpoint, dataset.root nulled."""
    docs = {}
    for rel in AV.PM_JSON_RELPATHS + AV.TS_JSON_RELPATHS:
        docs[rel] = _load("omx_000010", *rel.split("/"))
    docs["pretrained_model/train_config.json"]["dataset"]["root"] = None
    return docs


def _validate(docs, *, step=10, total=20, params=None, dataset=OMX_DATASET, model=OMX_MODEL):
    return AV.validate_checkpoint_jsons(
        docs, step=step, total_steps=total, dataset_name=dataset,
        training_params=dict(OMX_PARAMS if params is None else params), output_dir=AV.output_dir_for(model))


def _md5(b: bytes) -> str:
    return base64.b64encode(hashlib.md5(b).digest()).decode()


def manifest(relpaths, size=10) -> dict:
    return {rel: {"size": size, "sha256": hashlib.sha256(rel.encode()).hexdigest(), "md5": _md5(rel.encode())}
            for rel in relpaths}


class TheConstantsAreLeRobots(unittest.TestCase):
    def test_eleven_files_and_seven_for_a_final(self):
        self.assertEqual(len(AV.CHECKPOINT_RELPATHS), 11)
        self.assertEqual(len(AV.FINAL_RELPATHS), 7)
        self.assertTrue(all(r.startswith("pretrained_model/") for r in AV.FINAL_RELPATHS))
        self.assertNotIn("training_state/scheduler_state.json", AV.CHECKPOINT_RELPATHS)  # ACT has no scheduler
        self.assertEqual(set(AV.SIZE_CAPS), set(AV.CHECKPOINT_RELPATHS))
        self.assertEqual(AV.SIZE_CAPS["pretrained_model/model.safetensors"], 512 * AV.MIB)
        self.assertEqual(AV.SIZE_CAPS["training_state/optimizer_state.safetensors"], 1024 * AV.MIB)
        self.assertEqual(AV.SIZE_CAPS["training_state/training_step.json"], 4096)
        self.assertEqual(AV.LOG_SIZE_CAP, 32 * AV.MIB)

    def test_the_step_id_rule(self):
        # lerobot/utils/train_utils.py:37-39
        self.assertEqual(AV.step_id(2500, 50000), "002500")
        self.assertEqual(AV.step_id(10, 20), "000010")
        self.assertEqual(AV.step_id(2500, 1234567), "0002500")

    def test_the_29_train_config_keys_are_the_fixtures(self):
        self.assertEqual(set(_load("omx_000010", "pretrained_model", "train_config.json")), AV.TRAIN_CONFIG_KEYS)
        self.assertEqual(len(AV.TRAIN_CONFIG_KEYS), 29)

    def test_output_dir(self):
        self.assertEqual(AV.output_dir_for("EduBotics-Solutions/e2e-act"),
                         "/tmp/training_output/EduBotics-Solutions_e2e-act")


class Manifests(unittest.TestCase):
    def _ok(self, step=2500, total=50000, files=None, resume=0):
        files = manifest(AV.CHECKPOINT_RELPATHS) if files is None else files
        return AV.validate_ckpt_manifest(step, files, total_steps=total, save_freq=2500, resume_step=resume)

    def _refused(self, **kw):
        with self.assertRaises(AV.ManifestError):
            self._ok(**kw)

    def test_a_good_manifest_is_normalised(self):
        out = self._ok()
        self.assertEqual(set(out), set(AV.CHECKPOINT_RELPATHS))

    def test_the_final_step_carries_exactly_the_seven(self):
        self.assertEqual(set(self._ok(step=50000, files=manifest(AV.FINAL_RELPATHS))), set(AV.FINAL_RELPATHS))
        self._refused(step=50000, files=manifest(AV.CHECKPOINT_RELPATHS))  # 11 at the final: refused
        self._refused(step=2500, files=manifest(AV.FINAL_RELPATHS))       # 7 mid-run: refused

    def test_a_final_step_off_the_cadence_is_accepted(self):
        self.assertTrue(AV.validate_ckpt_manifest(50001, manifest(AV.FINAL_RELPATHS), total_steps=50001,
                                                  save_freq=2500, resume_step=0))

    def test_missing_and_extra_files(self):
        files = manifest(AV.CHECKPOINT_RELPATHS)
        files.pop("training_state/rng_state.safetensors")
        self._refused(files=files)
        files = manifest(AV.CHECKPOINT_RELPATHS + ("training_state/scheduler_state.json",))
        self._refused(files=files)
        files = manifest(AV.CHECKPOINT_RELPATHS + ("../../etc/passwd",))
        self._refused(files=files)

    def test_size_caps_and_types(self):
        for bad in (AV.SIZE_CAPS["training_state/training_step.json"] + 1, -1, True, 1.0, "18"):
            files = manifest(AV.CHECKPOINT_RELPATHS)
            files["training_state/training_step.json"]["size"] = bad
            self._refused(files=files)

    def test_bad_hashes(self):
        for field, bad in (("sha256", "A" * 64), ("sha256", "a" * 63), ("sha256", None),
                           ("md5", "x" * 24), ("md5", base64.b64encode(b"x" * 15).decode()),
                           ("md5", base64.b64encode(b"x" * 16).decode().rstrip("="))):
            files = manifest(AV.CHECKPOINT_RELPATHS)
            files["pretrained_model/config.json"][field] = bad
            with self.subTest(field=field, bad=bad):
                self._refused(files=files)

    def test_an_extra_key_in_an_entry(self):
        files = manifest(AV.CHECKPOINT_RELPATHS)
        files["pretrained_model/config.json"]["url"] = "https://evil.example"
        self._refused(files=files)

    def test_step_rules(self):
        self._refused(step=2499)          # not a multiple of save_freq
        self._refused(step=0)
        self._refused(step=50001)         # beyond total
        self._refused(step=True)
        self._refused(step="2500")
        self._refused(step=2500, resume=2500)   # not above the resume step
        self.assertTrue(self._ok(step=5000, resume=2500))

    def test_the_log_manifest(self):
        good = {"training_log.txt": {"size": 100, "sha256": "a" * 64, "md5": _md5(b"x")}}
        self.assertTrue(AV.validate_log_manifest(good))
        too_big = copy.deepcopy(good)
        too_big["training_log.txt"]["size"] = AV.LOG_SIZE_CAP + 1
        for bad in (too_big, {"log.txt": good["training_log.txt"]}, {}, None):
            with self.assertRaises(AV.ManifestError):
                AV.validate_log_manifest(bad)


class TheRealCheckpointPasses(unittest.TestCase):
    def test_fresh_checkpoint(self):
        summary = _validate(omx_docs())
        self.assertEqual(summary["expected_cameras"], ["gripper", "scene"])
        self.assertEqual(summary["state_dim"], 6)
        self.assertEqual(summary["action_dim"], 6)
        self.assertEqual(summary["n_action_steps"], 15)

    def test_the_final_checkpoint_needs_no_training_state(self):
        docs = omx_docs()
        for rel in AV.TS_JSON_RELPATHS:
            docs.pop(rel)
        docs["pretrained_model/train_config.json"]["steps"] = 10
        _validate(docs, step=10, total=10, params=dict(OMX_PARAMS, steps=10))

    def test_a_cuda_device_is_accepted(self):
        docs = omx_docs()
        docs["pretrained_model/config.json"]["device"] = "cuda"
        docs["pretrained_model/train_config.json"]["policy"]["device"] = "cuda"
        _validate(docs)

    def test_a_pinned_revision_must_be_the_rows(self):
        docs = omx_docs()
        docs["pretrained_model/train_config.json"]["dataset"]["revision"] = SHA
        _validate(docs, params=dict(OMX_PARAMS, dataset_revision=SHA))
        with self.assertRaises(AV.ArtifactRefused):
            _validate(docs)  # the row is unpinned, the checkpoint is not
        with self.assertRaises(AV.ArtifactRefused):
            _validate(docs, params=dict(OMX_PARAMS, dataset_revision="0" * 40))

    def test_the_resumed_e17_train_config_passes_the_structure_check(self):
        tc = _load("pusht_resumed_000020", "pretrained_model", "train_config.json")
        self.assertTrue(tc["resume"])
        params = {"steps": 20, "save_freq": 10, "log_freq": 1, "seed": 1000, "dataset_revision": SHA}
        AV.check_train_config(tc, dataset_name="lerobot/pusht", training_params=params,
                              output_dir=AV.output_dir_for(OMX_MODEL))
        self.assertEqual(_load("pusht_resumed_000020", "pretrained_model", "config.json"), tc["policy"])
        for key, value in (("checkpoint_path", "/tmp/elsewhere/checkpoints/000010"),
                           ("checkpoint_path", "/tmp/training_output/EduBotics-Solutions_e2e-act/checkpoints/00010"),
                           ("checkpoint_path", "/tmp/training_output/EduBotics-Solutions_e2e-act/checkpoints/000010/x")):
            bad = copy.deepcopy(tc)
            bad[key] = value
            with self.subTest(value=value), self.assertRaises(AV.ArtifactRefused):
                AV.check_train_config(bad, dataset_name="lerobot/pusht", training_params=params,
                                      output_dir=AV.output_dir_for(OMX_MODEL))
        bad = copy.deepcopy(tc)
        bad["policy"]["pretrained_path"] = "lerobot/pi0_base"
        with self.assertRaises(AV.ArtifactRefused):
            AV.check_train_config(bad, dataset_name="lerobot/pusht", training_params=params,
                                  output_dir=AV.output_dir_for(OMX_MODEL))

    def test_the_pusht_model_is_refused_by_the_robot_rules(self):
        # observation.image is not observation.images.<name>: a camera the robot cannot name.
        with self.assertRaises(AV.ArtifactRefused):
            AV.act_config_summary(_load("pusht_resumed_000020", "pretrained_model", "config.json"))


def _both(docs, fn):
    """Apply a policy mutation to config.json AND train_config.policy, so the
    specific rule (not config != policy) is what refuses."""
    fn(docs["pretrained_model/config.json"])
    fn(docs["pretrained_model/train_config.json"]["policy"])


def _tc(fn):
    return lambda d: fn(d["pretrained_model/train_config.json"])


def _set(path, value):
    def f(doc):
        node = doc
        for k in path[:-1]:
            node = node[k]
        node[path[-1]] = value
    return f


def _del(path):
    def f(doc):
        node = doc
        for k in path[:-1]:
            node = node[k]
        node.pop(path[-1])
    return f


def _pre(fn):
    return lambda d: fn(d["pretrained_model/policy_preprocessor.json"])


def _post(fn):
    return lambda d: fn(d["pretrained_model/policy_postprocessor.json"])


OUT = AV.output_dir_for(OMX_MODEL)

MUTATIONS = {
    # config.json (act_config_summary; applied to both copies)
    "config type groot": lambda d: _both(d, _set(("type",), "groot")),
    "config temporal ensembling": lambda d: _both(d, _set(("temporal_ensemble_coeff",), 0.01)),
    "config n_obs_steps 2": lambda d: _both(d, _set(("n_obs_steps",), 2)),
    "config use_peft": lambda d: _both(d, _set(("use_peft",), True)),
    "config env-state input": lambda d: _both(d, lambda c: c["input_features"].update(
        {"observation.environment_state": {"type": "ENV", "shape": [4]}})),
    "config camera name": lambda d: _both(d, lambda c: c["input_features"].update(
        {"observation.images.a b": {"type": "VISUAL", "shape": [3, 48, 64]}})),
    "config 4-channel camera": lambda d: _both(d, _set(("input_features", "observation.images.scene", "shape"), [4, 48, 64])),
    "config five cameras": lambda d: _both(d, lambda c: c["input_features"].update(
        {f"observation.images.c{i}": {"type": "VISUAL", "shape": [3, 48, 64]} for i in range(3)})),
    "config no state": lambda d: _both(d, lambda c: c["input_features"].pop("observation.state")),
    "config 2-D action": lambda d: _both(d, _set(("output_features", "action", "shape"), [6, 2])),
    "config extra output": lambda d: _both(d, lambda c: c["output_features"].update(
        {"next.reward": {"type": "REWARD", "shape": [1]}})),
    "config chunk_size 0": lambda d: _both(d, _set(("chunk_size",), 0)),
    "config n_action_steps 1001": lambda d: _both(d, _set(("n_action_steps",), 1001)),
    "config != train_config.policy": lambda d: d["pretrained_model/config.json"].update({"dim_model": 256}),
    # train_config.json structure
    "tc extra key": _tc(_set(("discover_packages_path",), "evil")),
    "tc missing key": _tc(_del(("rabc_head_mode",))),
    "tc dataset.repo_id": _tc(_set(("dataset", "repo_id"), "other/ds")),
    "tc dataset.revision": _tc(_set(("dataset", "revision"), "main")),
    "tc dataset.root": _tc(_set(("dataset", "root"), "/etc")),
    "tc dataset.episodes": _tc(_set(("dataset", "episodes"), [0])),
    "tc dataset.streaming": _tc(_set(("dataset", "streaming"), True)),
    "tc image_transforms.enable": _tc(_set(("dataset", "image_transforms", "enable"), True)),
    "tc video_backend": _tc(_set(("dataset", "video_backend"), "torchcodec")),
    "tc env": _tc(_set(("env",), {"type": "aloha"})),
    "tc peft": _tc(_set(("peft",), {"method_type": "LORA"})),
    "tc rabc_progress_path": _tc(_set(("rabc_progress_path",), "/tmp/x.parquet")),
    "tc scheduler": _tc(_set(("scheduler",), {"type": "cosine"})),
    "tc use_rabc": _tc(_set(("use_rabc",), True)),
    "tc wandb.enable": _tc(_set(("wandb", "enable"), True)),
    "tc rename_map": _tc(_set(("rename_map",), {"a": "b"})),
    "tc policy.device": lambda d: _both(d, _set(("device",), "mps")),
    "tc policy.push_to_hub": lambda d: _both(d, _set(("push_to_hub",), True)),
    "tc policy.vision_backbone": lambda d: _both(d, _set(("vision_backbone",), "resnet34")),
    "tc policy.pretrained_backbone_weights": lambda d: _both(d, _set(("pretrained_backbone_weights",), None)),
    "tc policy.use_amp": lambda d: _both(d, _set(("use_amp",), True)),
    "tc policy.n_action_steps": lambda d: _both(d, _set(("n_action_steps",), 20)),
    "tc output_dir": _tc(_set(("output_dir",), "/tmp/elsewhere")),
    "tc job_name": _tc(_set(("job_name",), "groot")),
    "tc seed": _tc(_set(("seed",), 7)),
    "tc batch_size": _tc(_set(("batch_size",), 16)),
    "tc steps": _tc(_set(("steps",), 21)),
    "tc eval_freq": _tc(_set(("eval_freq",), 100)),
    "tc log_freq": _tc(_set(("log_freq",), 50)),
    "tc save_freq": _tc(_set(("save_freq",), 5)),
    "tc save_checkpoint": _tc(_set(("save_checkpoint",), False)),
    "tc use_policy_training_preset": _tc(_set(("use_policy_training_preset",), False)),
    "tc resume not bool": _tc(_set(("resume",), "true")),
    "tc num_workers 65": _tc(_set(("num_workers",), 65)),
    "tc num_workers bool": _tc(_set(("num_workers",), True)),
    "tc checkpoint_path elsewhere": _tc(_set(("checkpoint_path",), "/tmp/x")),
    "tc policy.pretrained_path elsewhere": lambda d: _both(d, _set(("pretrained_path",), "lerobot/pi0_base")),
    # processors
    "pre class key": _pre(lambda p: p["steps"][0].update({"class": "os.system"})),
    "pre unknown step": _pre(lambda p: p["steps"].append({"registry_name": "tokenizer_processor", "config": {}})),
    "pre reordered": _pre(lambda p: p["steps"].reverse()),
    "pre state_file missing": _pre(lambda p: p["steps"][3].pop("state_file")),
    "pre state_file renamed": _pre(lambda p: p["steps"][3].update({"state_file": "../model.safetensors"})),
    "pre state_file on another step": _pre(lambda p: p["steps"][1].update({"state_file": AV.PRE_STATE_FILE})),
    "pre name": _pre(lambda p: p.update({"name": "evil"})),
    "pre steps not a list": _pre(lambda p: p.update({"steps": {}})),
    "post class key": _post(lambda p: p["steps"][1].update({"class": "x.Y"})),
    "post reordered": _post(lambda p: p["steps"].reverse()),
    "post state_file": _post(lambda p: p["steps"][0].update({"state_file": "x.safetensors"})),
    # training_state
    "training_step other": lambda d: d.update({"training_state/training_step.json": {"step": 11}}),
    "training_step bool": lambda d: d.update({"training_state/training_step.json": {"step": True}}),
    "training_step extra key": lambda d: d.update({"training_state/training_step.json": {"step": 10, "x": 1}}),
    "optimizer groups dict": lambda d: d.update({"training_state/optimizer_param_groups.json": {"lr": 1}}),
    "optimizer groups empty": lambda d: d.update({"training_state/optimizer_param_groups.json": []}),
    "optimizer groups non-dict": lambda d: d.update({"training_state/optimizer_param_groups.json": [1]}),
}


class EveryRuleRefusesItsMutation(unittest.TestCase):
    def test_each_mutation_is_a_content_refusal(self):
        self.assertGreaterEqual(len(MUTATIONS), 60)
        for name, mutate in MUTATIONS.items():
            docs = omx_docs()
            mutate(docs)
            with self.subTest(rule=name):
                with self.assertRaises(AV.ArtifactRefused) as ctx:
                    _validate(docs)
                self.assertTrue(ctx.exception.content, name)
                # ... and by ITS rule, not by a neighbour that happens to fire first.
                prefix = {"tc ": "train_config:", "pre ": "preprocessor:", "post ": "postprocessor:",
                          "training_step": "training_step.json", "optimizer": "optimizer_param_groups.json"}
                for head, want in prefix.items():
                    if name.startswith(head):
                        self.assertTrue(ctx.exception.reason.startswith(want), (name, ctx.exception.reason))
                if name.startswith("config "):
                    self.assertFalse(ctx.exception.reason.startswith(("train_config", "preprocessor")), name)

    def test_the_mutations_do_not_touch_the_fixture(self):
        _validate(omx_docs())  # still passes after every mutation above

    def test_strict_json(self):
        for raw in (b'{"a": NaN}', b'{"a": Infinity}', b"\xff\xfe", b"{", b""):
            with self.subTest(raw=raw), self.assertRaises(AV.ArtifactRefused) as ctx:
                AV.parse_json_bytes(raw, "x.json")
            self.assertTrue(ctx.exception.content)
        self.assertEqual(AV.parse_json_bytes(b'{"step": 10}', "x"), {"step": 10})


class DatasetFeatures(unittest.TestCase):
    """FR-5: the dataset's features through LeRobot's own map equal the
    config.json LeRobot wrote, and the robot's rules judge them before a credit."""

    def test_the_map_equals_lerobots_config_for_the_two_proven_datasets(self):
        for info_name, cfg_name in (("omx_synthetic_info.json", "omx_synthetic_config.json"),
                                    ("pusht_b1c3ecba_info.json", "pusht_config.json")):
            info = _load("datasets", info_name)
            cfg = _load("datasets", cfg_name)
            inputs, outputs = AV.dataset_policy_features(info["features"])
            with self.subTest(dataset=info_name):
                self.assertEqual(inputs, cfg["input_features"])
                self.assertEqual(outputs, cfg["output_features"])

    def test_the_omx_dataset_is_trainable_and_pusht_is_not(self):
        inputs, outputs = AV.dataset_policy_features(_load("datasets", "omx_synthetic_info.json")["features"])
        self.assertEqual(AV.act_feature_rules(inputs, outputs)["expected_cameras"], ["gripper", "scene"])
        inputs, outputs = AV.dataset_policy_features(_load("datasets", "pusht_b1c3ecba_info.json")["features"])
        with self.assertRaises(AV.ArtifactRefused):
            AV.act_feature_rules(inputs, outputs)

    def _features(self, extra=None, cameras=2):
        f = {
            "observation.state": {"dtype": "float32", "shape": [6], "names": ["j"] * 6},
            "action": {"dtype": "float32", "shape": [6], "names": ["j"] * 6},
            "timestamp": {"dtype": "float32", "shape": [1], "names": None},
            "frame_index": {"dtype": "int64", "shape": [1], "names": None},
        }
        for i in range(cameras):
            f[f"observation.images.cam{i}"] = {"dtype": "video", "shape": [480, 640, 3],
                                               "names": ["height", "width", "channels"]}
        f.update(extra or {})
        return f

    def test_feature_rules_on_datasets(self):
        ok = AV.act_feature_rules(*AV.dataset_policy_features(self._features(cameras=4)))
        self.assertEqual(ok["image_shapes"]["cam0"], [3, 480, 640])
        refused = {
            "fifth camera": self._features(cameras=5),
            "no camera": self._features(cameras=0),
            "environment state": self._features({"observation.environment_state": {"dtype": "float32", "shape": [4]}}),
            "velocity input": self._features({"observation.velocity": {"dtype": "float32", "shape": [6]}}),
            "second action": self._features({"action.extra": {"dtype": "float32", "shape": [1]}}),
            "2-D state": self._features({"observation.state": {"dtype": "float32", "shape": [6, 2]}}),
            "33 joints": self._features({"observation.state": {"dtype": "float32", "shape": [33]}}),
            "bad camera name": self._features({"observation.images.a.b": {"dtype": "video", "shape": [4, 4, 3],
                                                                           "names": ["h", "w", "channels"]}}),
        }
        for name, features in refused.items():
            with self.subTest(case=name), self.assertRaises(AV.ArtifactRefused):
                AV.act_feature_rules(*AV.dataset_policy_features(features))

    def test_malformed_features_are_refused_not_crashed(self):
        for features in (None, [], {"observation.state": None}, {"observation.state": {"dtype": "float32"}},
                         {"observation.images.x": {"dtype": "video", "shape": [4, 4], "names": ["h", "w"]}},
                         {"observation.images.x": {"dtype": "video", "shape": [4, 4, 3], "names": None}}):
            with self.subTest(features=features), self.assertRaises(AV.ArtifactRefused):
                AV.dataset_policy_features(features)

    def test_unrelated_keys_are_skipped_like_lerobot(self):
        inputs, outputs = AV.dataset_policy_features(self._features())
        self.assertNotIn("timestamp", inputs)
        self.assertNotIn("frame_index", inputs)
        self.assertEqual(set(outputs), {"action"})


if __name__ == "__main__":
    unittest.main()
