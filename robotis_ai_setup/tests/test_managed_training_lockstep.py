"""Managed (RunPod) training: the contracts held in lockstep across the packages (RunPod spec §6 WP-C).

A managed ACT training crosses three packages that were built apart and must agree byte for byte:

  * worker:  ``modal_training/training_handler.py`` (the trainer image and Modal's ``train_managed``)
             builds and uploads every checkpoint and validates the one it resumes from;
  * cloud:   ``cloud_training_api/app/services/artifact_validation.py`` judges every uploaded
             checkpoint and the final model, ``training_texts_de.py`` holds the German sentences,
             migrations 047 / 049 say which ``trainings`` columns a student may read;
  * robot:   ``physical_ai_server/inference/chunk_engine.py`` (``validate_model_dir``, the cloud
             inference's ACT check, and ``validate_processor_steps``, the robot's allowlist);
  * page:    ``physical_ai_manager/src/hooks/useSupabaseTrainings.js`` (the live column list).

If one side moved alone nothing would fail loudly: a worker whose file list lost a file uploads
checkpoints the API refuses (every training burns its four attempts), a step-id rule that pads
differently resumes from a path LeRobot never wrote, an API config rule looser than the robot's
publishes models the cloud inference then refuses, a column the page reads but no migration grants
is a 42501 that freezes every live chart. So the values are compared HERE, against the frozen
contract of spec §3 (§3.5 checkpoint content, §3.7 sentences, §3.9 constants).

How it reads the other packages:

  * The worker and the API are imported in a CHILD interpreter each (``sys.executable -B``), so
    neither their ``app`` package nor a stubbed ``supabase`` ever reaches this process's
    ``sys.modules`` (the GUI's tests own another package named ``app``). The child prints the
    module's plain constants and, for the API, the verdicts of the shared case tables as JSON.
  * Names are found by VALUE, not by name, where the spec fixes the value but not the name (the
    11 relpaths, the step lists, the four keys LeRobot sets on resume); the step-id rule is found
    by its SHAPE in the source and evaluated; ``OUTPUT_DIR``, ``act_config_summary``,
    ``dataset_policy_features`` and ``act_feature_rules`` are names the spec gives.
  * chunk_engine (stdlib + numpy) and german_detail_lint (stdlib) are loaded by path in-process.

Every input is REQUIRED: a missing file fails with its path and the package that owns it, it never
skips (a skipped lockstep is no lockstep). On WP-C's branch alone the worker and API halves fail
that way by design; they pass once WP-B and WP-A are merged (merge order B -> A -> C).
"""

import ast
import copy
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SETUP = REPO_ROOT / "robotis_ai_setup"
INFERENCE = REPO_ROOT / "physical_ai_tools" / "physical_ai_server" / "physical_ai_server" / "inference"
CHUNK_ENGINE_PY = INFERENCE / "chunk_engine.py"
CLOUD_PROTOCOL_PY = INFERENCE / "cloud_protocol.py"
API_ROOT = SETUP / "cloud_training_api"
SERVICES = API_ROOT / "app" / "services"
ARTIFACT_VALIDATION_PY = SERVICES / "artifact_validation.py"
TEXTS_PY = SERVICES / "training_texts_de.py"
MODAL_TRAINING = SETUP / "modal_training"
HANDLER_PY = MODAL_TRAINING / "training_handler.py"
MIGRATIONS = SETUP / "supabase" / "migrations"
MIGRATION_047 = MIGRATIONS / "20261020120000_047_hide_worker_token.sql"
MIGRATION_049 = MIGRATIONS / "20261020130000_049_managed_training.sql"
LIVE_JS = (REPO_ROOT / "physical_ai_tools" / "physical_ai_manager" / "src" / "hooks"
           / "useSupabaseTrainings.js")
MY_MODELS_JS = (REPO_ROOT / "physical_ai_tools" / "physical_ai_manager" / "src" / "components"
                / "MyModels.js")
GERMAN_LINT_PY = REPO_ROOT / ".github" / "scripts" / "german_detail_lint.py"

OWNERS = {
    ARTIFACT_VALIDATION_PY: "WP-A", TEXTS_PY: "WP-A", MIGRATION_047: "WP-A", MIGRATION_049: "WP-A",
    HANDLER_PY: "WP-B", CHUNK_ENGINE_PY: "WP-C", CLOUD_PROTOCOL_PY: "Cloud-Inferenz",
    LIVE_JS: "WP-C", MY_MODELS_JS: "WP-C", GERMAN_LINT_PY: "CI",
}

# ------------------------------------------------------------------ the frozen contract (spec §3)

# §3.5: the 11 files of a checkpoint (ACT has no scheduler, so no scheduler_state.json, §1 item 1).
PRETRAINED_RELPATHS = (
    "pretrained_model/config.json",
    "pretrained_model/model.safetensors",
    "pretrained_model/train_config.json",
    "pretrained_model/policy_preprocessor.json",
    "pretrained_model/policy_preprocessor_step_3_normalizer_processor.safetensors",
    "pretrained_model/policy_postprocessor.json",
    "pretrained_model/policy_postprocessor_step_0_unnormalizer_processor.safetensors",
)
CHECKPOINT_RELPATHS = PRETRAINED_RELPATHS + (
    "training_state/optimizer_param_groups.json",
    "training_state/optimizer_state.safetensors",
    "training_state/rng_state.safetensors",
    "training_state/training_step.json",
)
# §3.5: LeRobot 0.5.1's ACT processor pipelines, in order.
ACT_PRE_STEPS = ("rename_observations_processor", "to_batch_processor", "device_processor",
                 "normalizer_processor")
ACT_POST_STEPS = ("unnormalizer_processor", "device_processor")
PRE_STATE_FILE = "policy_preprocessor_step_3_normalizer_processor.safetensors"
POST_STATE_FILE = "policy_postprocessor_step_0_unnormalizer_processor.safetensors"
# §3.9 and §4.7 step 6.4: the four keys LeRobot itself sets in train_config.json on resume.
RESUME_KEYS = frozenset({"resume", "checkpoint_path", "policy.pretrained_path", "num_workers"})
OUTPUT_DIR = "/tmp/training_output"


def lerobot_step_id(step, total_steps):
    """``lerobot/utils/train_utils.py:37-39`` (spec §3.5)."""
    return f"{step:0{max(6, len(str(total_steps)))}d}"


# §3.7, verbatim (templates keep their {placeholders}).
SENTENCES = {
    "S1": "GPU-Server ausgefallen – das Training wird ab Schritt {step} auf einem anderen Server fortgesetzt.",
    "S2": "GPU-Server ausgefallen – das Training startet auf einem anderen Server neu.",
    "S3": "Training fertig – das Modell wird geprüft und veröffentlicht …",
    "S4": "Das Training konnte auch nach 4 Versuchen auf GPU-Servern nicht abgeschlossen werden und wurde "
          "abgebrochen. Das Trainingsguthaben wurde erstattet – bitte starte das Training neu.",
    "S5": "Das Training hat das Limit von {hours} Stunden Trainingszeit erreicht und wurde abgebrochen. "
          "Das Trainingsguthaben wurde erstattet.",
    "S6": "Das Training wurde nicht innerhalb von 12 Stunden fertig und wurde abgebrochen. "
          "Das Trainingsguthaben wurde erstattet.",
    "S7": "Das Modell hat die Sicherheitsprüfung nicht bestanden und wurde nicht veröffentlicht. "
          "Das Trainingsguthaben wurde erstattet – bitte starte das Training neu.",
    "S8": "Das Modell konnte nicht auf Hugging Face veröffentlicht werden. Das Trainingsguthaben wurde "
          "erstattet – bitte starte das Training neu.",
    "S9": "Das Training ist fertig und wird gerade veröffentlicht – Abbrechen ist nicht mehr möglich.",
    "S10": "Der Trainings-Worker ist für dieses Training nicht mehr berechtigt.",
    "S11": "Die Dateiliste des Checkpoints ist ungültig.",
    "S12": "Der Checkpoint hat die Prüfung nicht bestanden und wurde verworfen.",
    "S13": "Der letzte Checkpoint fehlt – das Training kann noch nicht abgeschlossen werden.",
    "S14": "Dieser Checkpoint wurde bereits übernommen.",
    "S15": "Für diesen Checkpoint wurde kein Upload angefordert.",
    "S16": "Der Cloud-GPU-Server ist nicht nutzbar (keine passende GPU). Das Training wurde nicht "
           "gestartet – das Trainingsguthaben wurde erstattet.",
    "S17": "Das Modell enthält nicht erlaubte Verarbeitungsschritte und wird aus Sicherheitsgründen nicht "
           "geladen.",
    "S18": "Zurzeit ist keine Cloud-GPU verfügbar. Das Trainingsguthaben wurde erstattet – bitte starte das "
           "Training später neu.",
    "S19": "Das Training ist auf dem GPU-Server abgebrochen. Details stehen im Trainingsprotokoll.",
    "S20": "Dieser Datensatz kann nicht mit ACT trainiert werden: Kameras, Gelenkdaten oder Aktionen passen "
           "nicht zu dem, was der Roboter senden kann.",
    "S21": "Der Abbruch konnte nicht an den GPU-Server übermittelt werden. Das Trainingsguthaben wurde "
           "erstattet – bitte starte das Training neu.",
    "S22": "Zeitlimit erreicht – der letzte Zwischenstand wird geprüft und veröffentlicht …",
    "S23": "Nach {hours} Stunden bei Schritt {step} von {total} gestoppt – du kannst das Training fortsetzen.",
    "S24": "Der gespeicherte Zwischenstand ist abgelaufen oder nicht mehr nutzbar – das Training kann nicht "
           "mehr fortgesetzt werden. Bitte starte ein neues Training.",
    "S25": "Dieses Training wird bereits fortgesetzt.",
    "S26": "Dieses Training kann zurzeit nicht fortgesetzt werden.",
    "S27": "Der gespeicherte Zwischenstand ist nicht mehr nutzbar. Das Trainingsguthaben wurde erstattet – "
           "bitte starte das Training neu.",
    "S28": "Fortsetzung ab Schritt {step} – ein GPU-Server wird dafür gesucht …",
    "S29": "Das Zeitlimit von 12 Stunden seit dem Start wurde bei Schritt {step} von {total} erreicht – "
           "du kannst das Training fortsetzen.",
}
# The two sentences with neither an umlaut nor a word of german_detail_lint's GERMAN_WORDS. The lint
# itself classifies such a string as neither language and accepts it (ENGLISH is judged only on
# strings that do not classify as German); they are pinned verbatim above. The marker rule below
# requires this set to be EXACTLY the marker-less sentences, so a new one without a marker fails.
MARKERLESS_SENTENCES = frozenset({"S1", "S2"})

# ------------------------------------------------------------------ shared case tables

_NORM = {"VISUAL": "MEAN_STD", "STATE": "MEAN_STD", "ACTION": "MEAN_STD"}
# A real EduBotics ACT config.json (the one test_chunk_engine_validate.py pins).
BASE_CONFIG = {
    "type": "act", "n_obs_steps": 1,
    "input_features": {
        "observation.images.gripper": {"type": "VISUAL", "shape": [3, 480, 640]},
        "observation.images.scene": {"type": "VISUAL", "shape": [3, 480, 640]},
        "observation.state": {"type": "STATE", "shape": [6]},
    },
    "output_features": {"action": {"type": "ACTION", "shape": [6]}},
    "device": "cuda", "use_amp": False, "use_peft": False, "push_to_hub": False, "repo_id": None,
    "private": None, "tags": None, "license": None, "pretrained_path": None,
    "chunk_size": 100, "n_action_steps": 15, "normalization_mapping": _NORM,
    "vision_backbone": "resnet18", "pretrained_backbone_weights": "ResNet18_Weights.IMAGENET1K_V1",
    "replace_final_stride_with_dilation": False, "pre_norm": False, "dim_model": 512, "n_heads": 8,
    "dim_feedforward": 3200, "feedforward_activation": "relu", "n_encoder_layers": 4,
    "n_decoder_layers": 1, "use_vae": True, "latent_dim": 32, "n_vae_encoder_layers": 4,
    "temporal_ensemble_coeff": None, "dropout": 0.1, "kl_weight": 10.0, "optimizer_lr": 1e-05,
    "optimizer_weight_decay": 0.0001, "optimizer_lr_backbone": 1e-05,
}
ACT_PRE_JSON = {"name": "policy_preprocessor", "steps": [
    {"registry_name": "rename_observations_processor", "config": {"rename_map": {}}},
    {"registry_name": "to_batch_processor", "config": {}},
    {"registry_name": "device_processor", "config": {"device": "cuda", "float_dtype": None}},
    {"registry_name": "normalizer_processor", "config": {"eps": 1e-08, "features": {}, "norm_map": _NORM},
     "state_file": PRE_STATE_FILE},
]}
ACT_POST_JSON = {"name": "policy_postprocessor", "steps": [
    {"registry_name": "unnormalizer_processor", "config": {"eps": 1e-08, "features": {}, "norm_map": _NORM},
     "state_file": POST_STATE_FILE},
    {"registry_name": "device_processor", "config": {"device": "cpu", "float_dtype": None}},
]}


def _vis(h=480, w=640, c=3):
    return {"type": "VISUAL", "shape": [c, h, w]}


def _inputs(**over):
    f = copy.deepcopy(BASE_CONFIG["input_features"])
    for k, v in over.items():
        key = k.replace("__", ".")
        if v is None:
            f.pop(key, None)
        else:
            f[key] = v
    return f


def _cfg(**over):
    c = copy.deepcopy(BASE_CONFIG)
    c.update(over)
    return c


# (name, config.json, chunk_engine's verdict). Only configs a host can write: LeRobot's own shape,
# mutated where a hostile or broken host would. Every row runs through BOTH chunk_engine and the API.
CONFIG_CASES = [
    ("real_two_cameras", _cfg(), "ok"),
    ("one_camera", _cfg(input_features=_inputs(observation__images__gripper=None)), "ok"),
    ("four_cameras", _cfg(input_features=_inputs(**{f"observation__images__c{i}": _vis(48, 64) for i in range(2)})),
     "ok"),
    ("camera_name_alphabet", _cfg(input_features={"observation.state": {"type": "STATE", "shape": [6]},
                                                  "observation.images.Wrist_cam-2": _vis()}), "ok"),
    ("small_and_large_images", _cfg(input_features=_inputs(observation__images__gripper=_vis(1, 1),
                                                           observation__images__scene=_vis(8192, 8192))), "ok"),
    ("state_and_action_bounds", _cfg(input_features=_inputs(observation__state={"type": "STATE", "shape": [32]}),
                                     output_features={"action": {"type": "ACTION", "shape": [1]}}), "ok"),
    ("chunk_bounds", _cfg(chunk_size=1000, n_action_steps=1000), "ok"),
    ("chunk_one", _cfg(chunk_size=1, n_action_steps=1), "ok"),
    ("type_diffusion", _cfg(type="diffusion"), "refused"),
    ("type_pi0", _cfg(type="pi0"), "refused"),
    ("type_groot", _cfg(type="groot"), "refused"),
    ("type_none", _cfg(type=None), "refused"),
    ("temporal_ensemble", _cfg(temporal_ensemble_coeff=0.01), "refused"),
    ("temporal_ensemble_zero", _cfg(temporal_ensemble_coeff=0), "refused"),
    ("n_obs_steps_two", _cfg(n_obs_steps=2), "refused"),
    ("n_obs_steps_str", _cfg(n_obs_steps="1"), "refused"),
    ("peft", _cfg(use_peft=True), "refused"),
    ("no_state", _cfg(input_features=_inputs(observation__state=None)), "refused"),
    ("state_2d", _cfg(input_features=_inputs(observation__state={"type": "STATE", "shape": [2, 3]})), "refused"),
    ("state_too_wide", _cfg(input_features=_inputs(observation__state={"type": "STATE", "shape": [33]})), "refused"),
    ("state_zero", _cfg(input_features=_inputs(observation__state={"type": "STATE", "shape": [0]})), "refused"),
    ("state_bool_dim", _cfg(input_features=_inputs(observation__state={"type": "STATE", "shape": [True]})),
     "refused"),
    ("camera_one_channel", _cfg(input_features=_inputs(observation__images__gripper=_vis(c=1))), "refused"),
    ("camera_four_channels", _cfg(input_features=_inputs(observation__images__gripper=_vis(c=4))), "refused"),
    ("camera_2d", _cfg(input_features=_inputs(observation__images__gripper={"type": "VISUAL", "shape": [3, 480]})),
     "refused"),
    ("camera_zero_side", _cfg(input_features=_inputs(observation__images__gripper=_vis(0, 640))), "refused"),
    ("camera_side_too_large", _cfg(input_features=_inputs(observation__images__gripper=_vis(480, 9000))),
     "refused"),
    ("camera_shape_str", _cfg(input_features=_inputs(observation__images__gripper={"type": "VISUAL", "shape": "x"})),
     "refused"),
    ("camera_not_visual", _cfg(input_features=_inputs(observation__images__gripper={"type": "STATE",
                                                                                    "shape": [3, 480, 640]})),
     "refused"),
    ("camera_name_space", _cfg(input_features=_inputs(**{"observation__images__bad name": _vis()})), "refused"),
    ("camera_name_dot", _cfg(input_features=_inputs(**{"observation__images__a.b": _vis()})), "refused"),
    ("camera_name_33", _cfg(input_features=_inputs(**{"observation__images__" + "c" * 33: _vis()})), "refused"),
    ("no_camera", _cfg(input_features={"observation.state": {"type": "STATE", "shape": [6]}}), "refused"),
    ("five_cameras", _cfg(input_features=_inputs(**{f"observation__images__c{i}": _vis(48, 64) for i in range(3)})),
     "refused"),
    ("environment_state", _cfg(input_features=_inputs(observation__environment_state={"type": "ENV",
                                                                                      "shape": [4]})), "refused"),
    ("extra_state_key", _cfg(input_features=_inputs(observation__velocity={"type": "STATE", "shape": [6]})),
     "refused"),
    ("pusht_image_key", _cfg(input_features=_inputs(observation__image=_vis(96, 96))), "refused"),
    ("inputs_not_dict", _cfg(input_features=["observation.state"]), "refused"),
    ("no_outputs", _cfg(output_features={}), "refused"),
    ("two_outputs", _cfg(output_features={"action": {"type": "ACTION", "shape": [6]},
                                          "action.extra": {"type": "ACTION", "shape": [1]}}), "refused"),
    ("action_2d", _cfg(output_features={"action": {"type": "ACTION", "shape": [2, 3]}}), "refused"),
    ("action_too_wide", _cfg(output_features={"action": {"type": "ACTION", "shape": [33]}}), "refused"),
    ("action_empty_shape", _cfg(output_features={"action": {"type": "ACTION", "shape": []}}), "refused"),
    ("outputs_not_dict", _cfg(output_features=None), "refused"),
    ("chunk_zero", _cfg(chunk_size=0), "refused"),
    ("chunk_too_large", _cfg(chunk_size=1001), "refused"),
    ("chunk_bool", _cfg(chunk_size=True), "refused"),
    ("n_action_steps_zero", _cfg(n_action_steps=0), "refused"),
    ("n_action_steps_str", _cfg(n_action_steps="15"), "refused"),
]


def _video(h=480, w=640, c=3, names=("height", "width", "channels"), dtype="video"):
    shape = [h, w, c] if names[-1] in ("channel", "channels") else [c, h, w]
    return {"dtype": dtype, "shape": shape, "names": list(names)}


def _float(n, dims=None):
    return {"dtype": "float32", "shape": dims or [n], "names": [f"j{i}" for i in range(n)]}


_BOOKKEEPING = {
    "timestamp": {"dtype": "float32", "shape": [1], "names": None},
    "frame_index": {"dtype": "int64", "shape": [1], "names": None},
    "episode_index": {"dtype": "int64", "shape": [1], "names": None},
    "index": {"dtype": "int64", "shape": [1], "names": None},
    "task_index": {"dtype": "int64", "shape": [1], "names": None},
}


def _ds(**over):
    """A recorded dataset's meta/info.json ``features`` (LeRobot v3.0, what the recorder writes)."""
    f = {"action": _float(6), "observation.state": _float(6),
         "observation.images.gripper": _video(), "observation.images.scene": _video(), **_BOOKKEEPING}
    for k, v in over.items():
        key = k.replace("__", ".")
        if v is None:
            f.pop(key, None)
        else:
            f[key] = v
    return f


def lerobot_policy_features(features):
    """Spec §4.1 step 5 / FR-5: LeRobot 0.5.1's ``dataset_to_policy_features``
    (``lerobot/datasets/feature_utils.py:169-209``) plus ``make_policy``'s ACT split
    (outputs = the ACTION features, inputs = the rest), in config.json's JSON form."""
    out = {}
    for key, ft in features.items():
        shape = list(ft["shape"])
        if ft["dtype"] in ("image", "video"):
            kind = "VISUAL"
            assert len(shape) == 3, key
            if ft["names"][2] in ("channel", "channels"):
                shape = [shape[2], shape[0], shape[1]]
        elif key == "observation.environment_state":
            kind = "ENV"
        elif key.startswith("observation"):
            kind = "STATE"
        elif key.startswith("action"):
            kind = "ACTION"
        else:
            continue
        out[key] = {"type": kind, "shape": shape}
    outputs = {k: v for k, v in out.items() if v["type"] == "ACTION"}
    inputs = {k: v for k, v in out.items() if k not in outputs}
    return inputs, outputs


# (name, dataset features, verdict). Each runs through the API's dataset_policy_features +
# act_feature_rules and, as the equivalent config.json, through chunk_engine.validate_model_dir.
FEATURE_CASES = [
    ("recorded_two_cameras", _ds(), "ok"),
    ("image_dtype", _ds(observation__images__gripper=_video(dtype="image")), "ok"),
    ("channels_first_names", _ds(observation__images__scene=_video(names=("channels", "height", "width"))), "ok"),
    ("singular_channel_name", _ds(observation__images__scene=_video(names=("height", "width", "channel"))), "ok"),
    ("one_camera", _ds(observation__images__gripper=None), "ok"),
    ("four_cameras", _ds(**{f"observation__images__c{i}": _video(48, 64) for i in range(2)}), "ok"),
    ("reward_is_ignored", _ds(**{"next__reward": {"dtype": "float32", "shape": [1], "names": None}}), "ok"),
    ("five_cameras", _ds(**{f"observation__images__c{i}": _video(48, 64) for i in range(3)}), "refused"),
    ("environment_state", _ds(observation__environment_state=_float(4)), "refused"),
    ("extra_observation", _ds(observation__velocity=_float(6)), "refused"),
    ("pusht_image_key", _ds(observation__image=_video(96, 96)), "refused"),
    ("rgba_camera", _ds(observation__images__gripper=_video(c=4)), "refused"),
    ("gray_camera", _ds(observation__images__gripper=_video(c=1)), "refused"),
    ("state_2d", _ds(observation__state=_float(6, dims=[2, 3])), "refused"),
    ("state_too_wide", _ds(observation__state=_float(33)), "refused"),
    ("action_too_wide", _ds(action=_float(33)), "refused"),
    ("second_action", _ds(action__gripper=_float(1)), "refused"),
    ("no_state", _ds(observation__state=None), "refused"),
    ("no_camera", _ds(observation__images__gripper=None, observation__images__scene=None), "refused"),
    ("camera_name_dot", _ds(**{"observation__images__a.b": _video()}), "refused"),
]

# ------------------------------------------------------------------ helpers


def _require(path):
    """The zero-file floor: a missing input fails with its path and owner, never skips."""
    if not path.is_file():
        owner = OWNERS.get(path, "another package")
        raise AssertionError(
            f"{path.relative_to(REPO_ROOT)} is missing ({owner}'s file): the managed-training lockstep has "
            "nothing to compare, and a skipped lockstep is no lockstep. On WP-C's branch alone this is "
            "expected until WP-B and WP-A are merged (merge order B -> A -> C).")
    return path


def _text(path):
    return _require(path).read_text(encoding="utf-8")


def _load_by_path(name, path):
    """Execute a file's SOURCE as a fresh module: no sys.modules entry, and no __pycache__ read or
    written (a cached .pyc keyed on mtime and size can outlive a same-size edit made within one
    second, and this test must judge the file as it is now)."""
    module = types.ModuleType(name)
    module.__file__ = str(_require(path))
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), module.__dict__)
    return module


def _chunk_engine():
    """chunk_engine with cloud_protocol loaded by path and bound on the module itself (no
    ``cloud_protocol`` entry in sys.modules)."""
    protocol = _load_by_path("_managed_lockstep_cloud_protocol", CLOUD_PROTOCOL_PY)
    engine = _load_by_path("_managed_lockstep_chunk_engine", CHUNK_ENGINE_PY)
    engine._protocol = lambda: protocol
    return engine


_GERMAN_LINT = None


def _german_lint():
    global _GERMAN_LINT
    if _GERMAN_LINT is None:
        _GERMAN_LINT = _load_by_path("_managed_lockstep_german_lint", GERMAN_LINT_PY)
    return _GERMAN_LINT


# The child's half: dump a module's plain constants as tagged JSON. Lists/tuples are JSON lists,
# sets {"__set__": [...]}, dicts {"__dict__": [[k, v], ...]}, paths their text; anything else
# (functions, classes, regexes, objects) is left out.
_CHILD_COMMON = r'''
import importlib, importlib.util, json, os, pathlib, sys, traceback, types
_SKIP = object()

def dump(v, depth=0):
    if depth > 8:
        return _SKIP
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, os.PathLike):
        return os.fspath(v)
    if isinstance(v, (list, tuple)):
        items = [dump(x, depth + 1) for x in v]
        return _SKIP if any(i is _SKIP for i in items) else items
    if isinstance(v, (set, frozenset)):
        items = [dump(x, depth + 1) for x in v]
        if any(i is _SKIP for i in items):
            return _SKIP
        return {"__set__": sorted(items, key=lambda i: json.dumps(i, sort_keys=True))}
    if isinstance(v, dict):
        pairs = [[dump(k, depth + 1), dump(x, depth + 1)] for k, x in v.items()]
        if any(p is _SKIP for pair in pairs for p in pair):
            return _SKIP
        return {"__dict__": pairs}
    return _SKIP

def constants(module):
    out = {}
    for name, value in vars(module).items():
        if name.startswith("__") or isinstance(value, types.ModuleType):
            continue
        d = dump(value)
        if d is not _SKIP:
            out[name] = d
    return out

def import_error(e):
    return "".join(traceback.format_exception_only(type(e), e)).strip()[-1500:]
'''

_API_CHILD = _CHILD_COMMON + r'''
payload = json.load(sys.stdin)
sys.path.insert(0, payload["api_root"])
out = {"errors": {}, "av": None, "texts": None, "config": [], "features": []}
try:
    av = importlib.import_module("app.services.artifact_validation")
    out["av"] = constants(av)
except BaseException as e:
    av = None
    out["errors"]["artifact_validation"] = import_error(e)
try:
    texts = importlib.import_module("app.services.training_texts_de")
    out["texts"] = constants(texts)
except BaseException as e:
    out["errors"]["training_texts_de"] = import_error(e)

def verdict(fn, *args):
    try:
        result = fn(*args)
    except Exception as e:
        cls = type(e)
        refused = isinstance(e, ValueError) or cls.__module__ == av.__name__
        return {"v": "refused" if refused else "crash", "type": cls.__name__, "msg": str(e)[:300]}
    return {"v": "ok", "result": repr(result)[:300]}

if av is not None:
    missing = [n for n in ("act_config_summary", "dataset_policy_features", "act_feature_rules")
               if not callable(getattr(av, n, None))]
    if missing:
        out["errors"]["functions"] = "artifact_validation lacks " + ", ".join(missing)
    else:
        for config in payload["config_cases"]:
            out["config"].append(verdict(av.act_config_summary, config))
        for features in payload["feature_cases"]:
            try:
                pair = av.dataset_policy_features(features)
                inputs, outputs = pair
            except Exception as e:
                out["features"].append({"stage": "dataset_policy_features", "v": "crash",
                                        "type": type(e).__name__, "msg": str(e)[:300]})
                continue
            row = verdict(av.act_feature_rules, inputs, outputs)
            d_in, d_out = dump(inputs), dump(outputs)
            row.update(stage="act_feature_rules",
                       inputs=None if d_in is _SKIP else d_in, outputs=None if d_out is _SKIP else d_out)
            out["features"].append(row)
print(json.dumps(out))
'''

_WORKER_CHILD = _CHILD_COMMON + r'''
payload = json.load(sys.stdin)
sys.path.insert(0, payload["modal_dir"])
out = {"errors": {}, "th": None}
if importlib.util.find_spec("supabase") is None:          # CI installs no supabase (the Modal image does)
    m = types.ModuleType("supabase")
    m.create_client = lambda *a, **kw: None
    sys.modules["supabase"] = m
if importlib.util.find_spec("huggingface_hub") is None:   # CI installs the real one; a bare dev box may not
    m = types.ModuleType("huggingface_hub")
    m.HfApi = type("HfApi", (), {"__init__": lambda self, *a, **kw: None})
    m.hf_hub_download = lambda *a, **kw: None
    m.login = lambda *a, **kw: None
    u = types.ModuleType("huggingface_hub.utils")
    for n in ("HfHubHTTPError", "RepositoryNotFoundError", "RevisionNotFoundError"):
        setattr(u, n, type(n, (Exception,), {}))
    m.utils = u
    sys.modules["huggingface_hub"] = m
    sys.modules["huggingface_hub.utils"] = u
try:
    import training_handler as th
    out["th"] = constants(th)
except BaseException as e:
    out["errors"]["training_handler"] = import_error(e)
print(json.dumps(out))
'''


def _run_child(script, payload):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    proc = subprocess.run([sys.executable, "-B", "-c", script], input=json.dumps(payload),
                          capture_output=True, text=True, encoding="utf-8", timeout=180, env=env,
                          cwd=str(REPO_ROOT))
    if proc.returncode != 0:
        raise AssertionError(f"lockstep child failed (exit {proc.returncode}):\n{proc.stderr[-3000:]}")
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")]
    if not lines:
        raise AssertionError(f"lockstep child printed no JSON:\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}")
    return json.loads(lines[-1])


def _decode(v):
    """Tagged JSON -> hashable Python (lists become tuples)."""
    if isinstance(v, list):
        return tuple(_decode(x) for x in v)
    if isinstance(v, dict):
        if "__set__" in v:
            return frozenset(_decode(x) for x in v["__set__"])
        if "__dict__" in v:
            return {_decode(k): _decode(x) for k, x in v["__dict__"]}
    return v


def _all_values(value, depth=0):
    """A constant and every collection nested inside it (dict values and sequence items)."""
    yield value
    if depth >= 4:
        return
    if isinstance(value, dict):
        for x in value.values():
            yield from _all_values(x, depth + 1)
    elif isinstance(value, (tuple, frozenset)):
        for x in value:
            yield from _all_values(x, depth + 1)


def _collections(constants):
    for name, raw in constants.items():
        for value in _all_values(_decode(raw)):
            yield name, value


def _strings(constants):
    for name, value in _collections(constants):
        if isinstance(value, str):
            yield name, value


def _as_key_set(value):
    """The four resume keys in any of the plain shapes: ``"policy.pretrained_path"`` or
    ``("policy", "pretrained_path")`` elements, or a dict ``{"policy": ("pretrained_path",), ...}``."""
    keys = set()
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                return None
            if v is None or v is True:
                keys.add(k)
            elif isinstance(v, (tuple, frozenset)) and v and all(isinstance(x, str) for x in v):
                keys.update(f"{k}.{x}" for x in v)
            elif isinstance(v, str):
                keys.add(f"{k}.{v}")
            else:
                return None
        return frozenset(keys)
    if not isinstance(value, (tuple, frozenset)) or not value:
        return None
    for item in value:
        if isinstance(item, str):
            keys.add(item)
        elif isinstance(item, tuple) and item and all(isinstance(x, str) for x in item):
            keys.add(".".join(item))
        else:
            return None
    return frozenset(keys)


# ------------------------------------------------------------------ the step-id rule, by its shape

def _is_width_call(node):
    """``max(6, len(str(<total>)))`` (either argument order) -> the ``<total>`` expression."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "max"
            and len(node.args) == 2 and not node.keywords):
        return None
    six = [a for a in node.args if isinstance(a, ast.Constant) and a.value == 6 and type(a.value) is int]
    other = [a for a in node.args if a not in six]
    if len(six) != 1 or len(other) != 1:
        return None
    ln = other[0]
    if not (isinstance(ln, ast.Call) and isinstance(ln.func, ast.Name) and ln.func.id == "len"
            and len(ln.args) == 1 and isinstance(ln.args[0], ast.Call)
            and isinstance(ln.args[0].func, ast.Name) and ln.args[0].func.id == "str"
            and len(ln.args[0].args) == 1):
        return None
    return ln.args[0].args[0]


def step_id_rules(source):
    """Every step-id expression in ``source``, as a callable ``(step, total_steps) -> str``.

    Recognised shapes (the width expression may also be a local name assigned from it in the same
    function):  ``f"{S:0{max(6, len(str(T)))}d}"``  and  ``str(S).zfill(max(6, len(str(T))))``.
    A shape is found by its width call; ``(rules, width_calls)`` lets a caller tell "no rule" from
    "a width call in a shape this test cannot evaluate".
    """
    tree = ast.parse(source)
    width_calls = sum(1 for n in ast.walk(tree) if _is_width_call(n) is not None)
    rules, seen = [], set()
    # Innermost scopes first, so a width name resolves in the function that assigns it.
    scopes = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))][::-1] + [tree]
    for scope in scopes:
        width_names = {}
        for n in ast.walk(scope):
            if (isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
                    and _is_width_call(n.value) is not None):
                width_names[n.targets[0].id] = n.value
        for n in ast.walk(scope):
            if id(n) in seen:
                continue
            width = step = None
            if (isinstance(n, ast.FormattedValue) and isinstance(n.format_spec, ast.JoinedStr)
                    and n.conversion == -1):
                parts = n.format_spec.values
                if (len(parts) == 3 and isinstance(parts[0], ast.Constant) and parts[0].value == "0"
                        and isinstance(parts[1], ast.FormattedValue) and parts[1].format_spec is None
                        and isinstance(parts[2], ast.Constant) and parts[2].value == "d"):
                    width, step = parts[1].value, n.value
            elif (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "zfill"
                  and len(n.args) == 1 and isinstance(n.func.value, ast.Call)
                  and isinstance(n.func.value.func, ast.Name) and n.func.value.func.id == "str"
                  and len(n.func.value.args) == 1):
                width, step = n.args[0], n.func.value.args[0]
            if width is None:
                continue
            resolved = width_names.get(width.id) if isinstance(width, ast.Name) else width
            total = _is_width_call(resolved) if resolved is not None else None
            # The padded value is the step itself, never arithmetic on it (`step + 1` pads another number).
            if total is None or any(isinstance(x, (ast.BinOp, ast.UnaryOp)) for x in ast.walk(step)):
                continue
            seen.add(id(n))
            rules.append(_compile_rule(n, step, total, width, resolved))
    return rules, width_calls


class _Substitute(ast.NodeTransformer):
    """Replace sub-expressions by their ``ast.dump``: ``{dump: factory() -> new node}``."""

    def __init__(self, replacements):
        self.replacements = replacements

    def visit(self, node):
        if isinstance(node, ast.expr):
            factory = self.replacements.get(ast.dump(node))
            if factory is not None:
                return ast.copy_location(factory(), node)
        return super().visit(node)


def _compile_rule(node, step, total, width, resolved):
    """The found expression with the step and total bound to names; a width NAME is replaced by the
    expression it was assigned from, so the evaluated text is exactly what the source computes."""
    def name(n):
        return lambda: ast.Name(id=n, ctx=ast.Load())

    inner = _Substitute({ast.dump(total): name("__total")})
    replacements = {ast.dump(step): name("__step"), ast.dump(total): name("__total")}
    if width is not resolved:
        replacements[ast.dump(width)] = lambda: inner.visit(copy.deepcopy(resolved))
    expr = _Substitute(replacements).visit(copy.deepcopy(node))
    if isinstance(expr, ast.FormattedValue):
        expr = ast.JoinedStr(values=[expr])
    code = compile(ast.fix_missing_locations(ast.Expression(body=expr)), "<step-id>", "eval")

    def rule(s, t):
        return eval(code, {"__builtins__": {"max": max, "len": len, "str": str}}, {"__step": s, "__total": t})
    rule.source = ast.unparse(node)
    return rule


STEP_ID_PROBES = [(1, 1), (10, 20), (2500, 50000), (50000, 50000), (7, 999999), (12, 1000000),
                  (2500, 1000000), (123456, 9999999), (5, 100)]


# ------------------------------------------------------------------ the managed path's German

def _docstring_nodes(tree):
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.body:
            first = n.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                    and isinstance(first.value.value, str):
                out.add(id(first.value))
    return out


def managed_path(tree, root="run_managed"):
    """The module-level functions and classes ``root`` reaches by name (calls, references, a
    ``target=`` argument), ``root`` included."""
    defs = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    seen, stack = set(), [root]
    while stack:
        name = stack.pop()
        if name in seen or name not in defs:
            continue
        seen.add(name)
        for n in ast.walk(defs[name]):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in defs:
                stack.append(n.id)
    return [defs[n] for n in sorted(seen)]


_ERROR_KEYS = frozenset({"p_error_message", "error_message", "p_log_url"})


def german_on_the_managed_path(source, root="run_managed"):
    """``(german, writes)``: German-looking text (by german_detail_lint's own regexes) and literal
    text handed to an error-message parameter, inside what ``root`` reaches. A module-level string
    constant used there by name counts as its text (``p_error_message=S16_TEXT`` is a write)."""
    gdl = _german_lint()
    tree = ast.parse(source)
    docstrings = _docstring_nodes(tree)
    module_texts = {}
    for stmt in tree.body:
        if (isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Constant)
                and isinstance(stmt.value.value, str)):
            for t in stmt.targets:
                if isinstance(t, ast.Name):
                    module_texts[t.id] = stmt.value.value

    def is_german(t):
        return bool(gdl.GERMAN_CHARS.search(t) or gdl.GERMAN_WORDS.search(t) or gdl.TRANSLITERATIONS.search(t))

    def is_text(v):
        if isinstance(v, ast.Constant):
            return isinstance(v.value, str)
        return isinstance(v, ast.JoinedStr) or (isinstance(v, ast.Name) and v.id in module_texts)

    german, writes = [], []
    for node in managed_path(tree, root):
        for n in ast.walk(node):
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings \
                    and is_german(n.value):
                german.append((node.name, n.lineno, n.value[:80]))
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in module_texts \
                    and is_german(module_texts[n.id]):
                german.append((node.name, n.lineno, n.id))
            if isinstance(n, ast.Call):
                for kw in n.keywords:
                    if kw.arg in _ERROR_KEYS and is_text(kw.value):
                        writes.append((node.name, n.lineno, kw.arg))
            if isinstance(n, ast.Dict):
                for k, v in zip(n.keys, n.values):
                    if isinstance(k, ast.Constant) and k.value in _ERROR_KEYS and is_text(v):
                        writes.append((node.name, n.lineno, k.value))
    return german, writes


def _string_literals(source):
    return [n.value for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def _transliterate(text):
    return text.translate(str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss",
                                         "Ä": "Ae", "Ö": "Oe", "Ü": "Ue"}))


# ------------------------------------------------------------------ the checks


class _Children:
    api = None
    worker = None

    @classmethod
    def get(cls):
        if cls.api is None:
            cls.api = _run_child(_API_CHILD, {
                "api_root": str(API_ROOT),
                "config_cases": [c for _, c, _ in CONFIG_CASES],
                "feature_cases": [f for _, f, _ in FEATURE_CASES],
            })
        if cls.worker is None:
            cls.worker = _run_child(_WORKER_CHILD, {"modal_dir": str(MODAL_TRAINING)})
        return cls.api, cls.worker


def _api_constants(test):
    _require(ARTIFACT_VALIDATION_PY)
    api, _ = _Children.get()
    test.assertIsNone(api["errors"].get("artifact_validation"),
                      "app.services.artifact_validation must import in a plain interpreter (it is a pure validator)")
    return api["av"]


def _worker_constants(test):
    _require(HANDLER_PY)
    _, worker = _Children.get()
    test.assertIsNone(worker["errors"].get("training_handler"),
                      "training_handler must import with only supabase stubbed (spec impl-notes WP-B 4)")
    return worker["th"]


def _texts(test):
    _require(TEXTS_PY)
    api, _ = _Children.get()
    test.assertIsNone(api["errors"].get("training_texts_de"), "app.services.training_texts_de must import")
    return {name: _decode(v) for name, v in api["texts"].items()}


class TheCheckpointContractIsOneOnBothSides(unittest.TestCase):
    """§3.5 / §3.9: worker (``training_handler.py``) and API (``artifact_validation.py``)."""

    def _sides(self):
        return (("training_handler.py", _worker_constants(self)), ("artifact_validation.py", _api_constants(self)))

    def test_the_eleven_relpaths(self):
        want = frozenset(CHECKPOINT_RELPATHS)
        for side, consts in self._sides():
            found = []
            for name, value in _collections(consts):
                keys = frozenset(value) if isinstance(value, dict) else value
                if isinstance(keys, (tuple, frozenset)) and keys and all(isinstance(x, str) for x in keys) \
                        and frozenset(keys) == want and len(keys) == 11:
                    found.append(name)
            self.assertTrue(found, f"{side}: no module constant holds exactly the 11 checkpoint relpaths of §3.5")

    def test_the_act_step_lists_and_the_subset_chain(self):
        engine = _chunk_engine()
        for side, consts in self._sides():
            seqs = [v for _, v in _collections(consts) if isinstance(v, tuple)]
            self.assertIn(ACT_PRE_STEPS, seqs, f"{side}: no constant is the ACT preprocessor step list, in order")
            self.assertIn(ACT_POST_STEPS, seqs, f"{side}: no constant is the ACT postprocessor step list, in order")
        # The API's ACT lists ⊆ the cloud inference's ⊆ the robot's.
        self.assertLessEqual(set(ACT_PRE_STEPS), set(engine.PRE_STEPS_ALLOWED))
        self.assertLessEqual(set(engine.PRE_STEPS_ALLOWED), set(engine.ROBOT_PRE_STEPS_ALLOWED))
        self.assertLessEqual(set(ACT_POST_STEPS), set(engine.POST_STEPS_ALLOWED))
        self.assertLessEqual(set(engine.POST_STEPS_ALLOWED), set(engine.ROBOT_POST_STEPS_ALLOWED))

    def test_the_state_file_names(self):
        want = {PRE_STATE_FILE, POST_STATE_FILE}
        for side, consts in self._sides():
            names = set()
            for _, text in _strings(consts):
                base = text.rsplit("/", 1)[-1]
                if base.endswith(".safetensors") and "_processor" in base:
                    names.add(base)
            self.assertEqual(names, want, f"{side}: the processor state files differ from §3.5")

    def test_the_output_dir(self):
        for side, consts in self._sides():
            self.assertIn("OUTPUT_DIR", consts, f"{side}: no OUTPUT_DIR constant (§3.9)")
            self.assertEqual(_decode(consts["OUTPUT_DIR"]), OUTPUT_DIR, side)

    def test_the_four_keys_lerobot_sets_on_resume(self):
        for side, consts in self._sides():
            found = [name for name, value in _collections(consts) if _as_key_set(value) == RESUME_KEYS]
            self.assertTrue(found, f"{side}: no constant holds exactly the four resume keys {sorted(RESUME_KEYS)}")

    def test_the_step_id_rule(self):
        for path in (HANDLER_PY, ARTIFACT_VALIDATION_PY):
            rules, width_calls = step_id_rules(_text(path))
            self.assertTrue(rules, f"{path.name}: no step-id rule f\"{{step:0{{max(6, len(str(total)))}}d}}\" "
                                   f"found (width calls seen: {width_calls}; accepted shapes in step_id_rules)")
            for rule in rules:
                for s, t in STEP_ID_PROBES:
                    self.assertEqual(rule(s, t), lerobot_step_id(s, t), f"{path.name}: {rule.source} at {(s, t)}")


class OneConfigRuleOnTheRobotAndInTheApi(unittest.TestCase):
    """SR-26 / FR-5: one table through ``chunk_engine.validate_model_dir`` and the API."""

    @classmethod
    def setUpClass(cls):
        cls.engine = _chunk_engine()
        cls.tmp = tempfile.TemporaryDirectory()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _robot_verdict(self, name, config):
        d = pathlib.Path(self.tmp.name) / name
        d.mkdir(parents=True, exist_ok=True)
        for fname, content in (("config.json", config), ("policy_preprocessor.json", ACT_PRE_JSON),
                               ("policy_postprocessor.json", ACT_POST_JSON)):
            (d / fname).write_text(json.dumps(content), encoding="utf-8")
        try:
            self.engine.validate_model_dir(str(d))
        except self.engine.ModelRefused:
            return "refused"
        return "ok"

    def test_the_tables_pin_chunk_engine(self):
        for name, config, want in CONFIG_CASES:
            self.assertEqual(self._robot_verdict("c_" + name, config), want, name)
        for name, features, want in FEATURE_CASES:
            inputs, outputs = lerobot_policy_features(features)
            self.assertEqual(self._robot_verdict("f_" + name, _cfg(input_features=inputs, output_features=outputs)),
                             want, name)

    def test_config_cases_have_equal_verdicts(self):
        _require(ARTIFACT_VALIDATION_PY)
        api, _ = _Children.get()
        self.assertIsNone(api["errors"].get("artifact_validation"))
        self.assertIsNone(api["errors"].get("functions"))
        self.assertEqual(len(api["config"]), len(CONFIG_CASES))
        for (name, config, _), got in zip(CONFIG_CASES, api["config"]):
            self.assertNotEqual(got["v"], "crash", f"{name}: act_config_summary crashed instead of refusing: {got}")
            self.assertEqual(got["v"], self._robot_verdict("c_" + name, config),
                             f"{name}: act_config_summary {got} vs chunk_engine.validate_model_dir")

    def test_feature_cases_have_equal_verdicts(self):
        _require(ARTIFACT_VALIDATION_PY)
        api, _ = _Children.get()
        self.assertIsNone(api["errors"].get("artifact_validation"))
        self.assertIsNone(api["errors"].get("functions"))
        self.assertEqual(len(api["features"]), len(FEATURE_CASES))
        for (name, features, _), got in zip(FEATURE_CASES, api["features"]):
            self.assertNotEqual(got["v"], "crash", f"{name}: {got['stage']} crashed: {got}")
            inputs, outputs = lerobot_policy_features(features)
            self.assertEqual((_decode(got["inputs"]), _decode(got["outputs"])),
                             (_decode(json.loads(json.dumps(_tag(inputs)))), _decode(json.loads(json.dumps(_tag(outputs))))),
                             f"{name}: dataset_policy_features differs from LeRobot's dataset_to_policy_features + "
                             "the ACT split, in config.json's JSON form")
            robot = self._robot_verdict("f_" + name, _cfg(input_features=inputs, output_features=outputs))
            self.assertEqual(got["v"], robot, f"{name}: act_feature_rules {got} vs chunk_engine on the equivalent config")


def _tag(v):
    """Python -> the child's tagged JSON form (for comparing like with like)."""
    if isinstance(v, dict):
        return {"__dict__": [[_tag(k), _tag(x)] for k, x in v.items()]}
    if isinstance(v, (list, tuple)):
        return [_tag(x) for x in v]
    return v


class TheSentencesAreTheContract(unittest.TestCase):
    """§3.7: defined once in training_texts_de.py, S16 also in the worker, S17 also in chunk_engine."""

    def test_s17_is_the_same_on_the_robot(self):
        self.assertEqual(_chunk_engine().PROCESSOR_STEPS_REFUSED_DE, SENTENCES["S17"])
        self.assertEqual(_texts(self).get("S17"), SENTENCES["S17"])

    def test_every_sentence_is_the_spec_text(self):
        texts = _texts(self)
        numbered = {n for n in texts if re.fullmatch(r"S\d+", n)}
        self.assertEqual(numbered, set(SENTENCES), "training_texts_de defines exactly S1 … S29")
        for name, want in SENTENCES.items():
            self.assertEqual(texts[name], want, name)

    def test_the_umlaut_rule(self):
        """No transliteration anywhere in the module (the lint's own TRANSLITERATIONS stems, never
        a naive ae/oe/ue/ss search: „abgeschlossen" and „passen" are correct German); every S*
        sentence and every other sentence-like constant carries an umlaut or a GERMAN_WORDS marker,
        except exactly MARKERLESS_SENTENCES."""
        gdl = _german_lint()
        texts = _texts(self)
        markerless = set()
        for name, value in texts.items():
            for text in (v for v in _all_values(value) if isinstance(v, str)):
                self.assertIsNone(gdl.TRANSLITERATIONS.search(text), f"{name}: transliteration in {text!r}")
                sentence_like = bool(re.fullmatch(r"S\d+", name)) or len(text.split()) >= 4
                if sentence_like and not (gdl.GERMAN_CHARS.search(text) or gdl.GERMAN_WORDS.search(text)):
                    markerless.add(name)
                if sentence_like:
                    self.assertFalse(gdl.ENGLISH_WORDS.search(text) and not (
                        gdl.GERMAN_CHARS.search(text) or gdl.GERMAN_WORDS.search(text)),
                        f"{name}: reads as English: {text!r}")
        self.assertEqual(markerless, set(MARKERLESS_SENTENCES))

    def test_the_models_card_shows_every_sentence_whole(self):
        """„Meine Modelle" cuts an error_message only past ERROR_MESSAGE_MAX_CHARS (a legacy
        worker's LeRobot blob): every S sentence, its placeholders at their widest, fits."""
        found = re.findall(r"export const ERROR_MESSAGE_MAX_CHARS = (\d+);", _text(MY_MODELS_JS))
        self.assertEqual(len(found), 1, "MyModels.js defines ERROR_MESSAGE_MAX_CHARS once, as a literal")
        limit = int(found[0])
        for name, text in SENTENCES.items():
            widest = (text.replace("{step}", "500.000").replace("{hours}", "999,5")
                      .replace("{total}", "500.000"))
            self.assertNotIn("{", widest, name)
            self.assertLessEqual(len(widest), limit, f"{name} would be cut on the models card")

    def test_the_rule_holds_for_the_contract_texts_themselves(self):
        gdl = _german_lint()
        for name, text in SENTENCES.items():
            self.assertIsNone(gdl.TRANSLITERATIONS.search(text), name)
            has_marker = bool(gdl.GERMAN_CHARS.search(text) or gdl.GERMAN_WORDS.search(text))
            self.assertEqual(has_marker, name not in MARKERLESS_SENTENCES, name)


class TheWorkerWritesNoGermanForTheDatabase(unittest.TestCase):
    """§3.7 / E11: a managed failure shows the API's fixed sentence; the worker's own text goes to
    the server log and the training log only. The legacy host check (§4.7) writes S16 verbatim."""

    def test_the_managed_path_carries_no_german_and_writes_no_error_text(self):
        source = _text(HANDLER_PY)
        tree = ast.parse(source)
        names = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        self.assertIn("run_managed", names, "training_handler.run_managed (§4.7) is missing")
        german, writes = german_on_the_managed_path(source)
        self.assertEqual(german, [], "German text on the managed path: its sentences belong to the API (E11)")
        self.assertEqual(writes, [], "the managed path hands literal text to an error-message parameter")

    def test_no_api_sentence_is_copied_into_the_worker_and_s16_is_verbatim(self):
        literals = _string_literals(_text(HANDLER_PY))
        self.assertTrue(SENTENCES["S16"] in literals,
                        "the legacy host check's S16 (§3.7, §4.7 Legacy mode) is not in training_handler.py verbatim")
        for name, text in SENTENCES.items():
            if name == "S16":
                continue
            fragment = max(re.split(r"\{[a-z_]+\}", text), key=len).strip()
            for candidate in {fragment, _transliterate(fragment)}:
                self.assertFalse(any(candidate in lit for lit in literals),
                                 f"{name} (or its transliteration) is in training_handler.py")

    def test_no_save_or_mirror_frequency_constant(self):
        source = _text(HANDLER_PY)
        tree = ast.parse(source)
        cadence = re.compile(r"(?i)(save|mirror|ckpt|checkpoint)_?(freq|frequency|every|interval|cadence)")
        for node in tree.body:
            targets = node.targets if isinstance(node, ast.Assign) else (
                [node.target] if isinstance(node, ast.AnnAssign) else [])
            for t in targets:
                for n in ast.walk(t):
                    if isinstance(n, ast.Name):
                        self.assertIsNone(cadence.search(n.id),
                                          f"training_handler defines {n.id}: the cadence comes from the spec (FR-12)")
        for text in _string_literals(source):
            self.assertIsNone(re.fullmatch(r"--save_freq=\d+", text), f"a hard-coded {text}")


class TheLiveColumnsAreGranted(unittest.TestCase):
    """047 revokes the table-wide SELECT and grants columns; 049 grants its own (§1 item 5). Every
    column the page's direct live read names must be granted, and the token never is."""

    @staticmethod
    def _live_columns():
        m = re.search(r"export const LIVE_COLUMNS =\s*((?:'[^']*'\s*\+?\s*)+);", _text(LIVE_JS))
        assert m, "useSupabaseTrainings.js: no `export const LIVE_COLUMNS = '…' + '…';`"
        return "".join(re.findall(r"'([^']*)'", m.group(1))).split(",")

    @staticmethod
    def _granted(path):
        sql = re.sub(r"--[^\n]*", "", _text(path))
        cols = set()
        for m in re.finditer(r"GRANT\s+SELECT\s*\(([^)]*)\)\s*ON\s+(?:public\.)?trainings\s+TO\s+anon\s*,"
                             r"\s*authenticated\s*;", sql, re.IGNORECASE):
            cols.update(c.strip() for c in m.group(1).split(",") if c.strip())
        return cols

    def test_every_live_column_is_granted_and_the_token_never(self):
        live = self._live_columns()
        self.assertNotIn("*", "".join(live))
        g047, g049 = self._granted(MIGRATION_047), self._granted(MIGRATION_049)
        self.assertTrue(g047, "047 grants no column list on trainings")
        self.assertIn("status_note", g049, "049 grants status_note")
        self.assertNotIn("worker_token", g047 | g049)
        self.assertEqual([c for c in live if c not in g047 | g049], [],
                         "a column the live read selects is granted by neither 047 nor 049 (42501 for students)")


class TheMatchersHaveTeeth(unittest.TestCase):
    """The helpers above judge other packages' code; these prove they can tell right from wrong
    on synthetic source, so a green run above is evidence and not a matcher that accepts anything."""

    def test_the_step_id_matcher_accepts_the_shapes_and_evaluates_them(self):
        good = textwrap.dedent('''
            def a(step, total_steps):
                return f"{step:0{max(6, len(str(total_steps)))}d}"
            def b(n, spec):
                width = max(len(str(spec["total_steps"])), 6)
                return f"checkpoints/{n:0{width}d}"
            def c(step, total):
                return str(step).zfill(max(6, len(str(total))))
        ''')
        rules, _ = step_id_rules(good)
        self.assertEqual(len(rules), 3)
        for rule in rules:
            for s, t in STEP_ID_PROBES:
                self.assertEqual(rule(s, t), lerobot_step_id(s, t), rule.source)

    def test_the_step_id_matcher_finds_no_rule_in_a_wrong_one(self):
        for bad in ('def f(s, t):\n    return f"{s:0{max(7, len(str(t)))}d}"\n',      # 7, not 6
                    'def f(s, t):\n    return f"{s:06d}"\n',                          # fixed width
                    'def f(s, t):\n    return str(s).zfill(6)\n',
                    'def f(s, t):\n    return f"{s:0{max(6, len(t))}d}"\n',           # len(t), not len(str(t))
                    'def f(s, t):\n    return f"{s:0{max(6, len(str(t)))}x}"\n',      # hex
                    'def f(s, t):\n    return f"{s + 1:0{max(6, len(str(t)))}d}"\n',  # another number
                    'def f(s, t):\n    w = max(6, len(str(t)))\n    return f"{s:0{w + 1}d}"\n'):
            rules, _ = step_id_rules(bad)
            self.assertEqual(rules, [], bad)
        # The width call alone is counted, so a caller can say "a width call in an unknown shape".
        self.assertEqual(step_id_rules('W = lambda t: max(6, len(str(t)))\n')[1], 1)

    def test_the_key_normaliser(self):
        self.assertEqual(_as_key_set(("resume", "checkpoint_path", ("policy", "pretrained_path"), "num_workers")),
                         RESUME_KEYS)
        self.assertEqual(_as_key_set(frozenset({"resume", "checkpoint_path", "policy.pretrained_path",
                                                "num_workers"})), RESUME_KEYS)
        self.assertEqual(_as_key_set({"resume": None, "checkpoint_path": None, "num_workers": None,
                                      "policy": ("pretrained_path",)}), RESUME_KEYS)
        self.assertNotEqual(_as_key_set(("resume", "checkpoint_path", "num_workers")), RESUME_KEYS)
        self.assertIsNone(_as_key_set((1, 2)))

    def test_the_managed_path_scanner(self):
        src = textwrap.dedent('''
            def run_managed(api):
                _report(api, "host check failed")
                worker = Uploader()
                return {"status": "ok"}
            def _report(api, detail):
                print("Training wurde abgebrochen")
                api.rpc(p_error_message=f"Fehler: {detail}")
            class Uploader:
                def tick(self):
                    send({"p_error_message": "Das Training ist abgebrochen."})
            def legacy():
                fail("Bitte Training neu starten.")
        ''')
        german, writes = german_on_the_managed_path(src)
        self.assertEqual({g[0] for g in german}, {"_report", "Uploader"})
        self.assertEqual({w[0] for w in writes}, {"_report", "Uploader"})
        self.assertNotIn("legacy", {g[0] for g in german})
        clean = 'def run_managed(api):\n    api.rpc(p_error_message=None, p_status="running")\n    _h(api)\n' \
                'def _h(api, detail=None):\n    api.rpc(p_error_message=detail)\n'
        self.assertEqual(german_on_the_managed_path(clean), ([], []))
        # A module constant used by name is its text; one used only by the legacy path is not scanned.
        by_name = ('S16_DE = "Der Cloud-GPU-Server ist nicht nutzbar."\n'
                   'def run_managed(api):\n    api.rpc(p_error_message=S16_DE)\n'
                   'def run_training(api):\n    api.rpc(p_error_message=S16_DE)\n')
        german, writes = german_on_the_managed_path(by_name)
        self.assertEqual([g[0] for g in german], ["run_managed"])
        self.assertEqual([w[0] for w in writes], ["run_managed"])
        legacy_only = by_name.replace('def run_managed(api):\n    api.rpc(p_error_message=S16_DE)',
                                      'def run_managed(api):\n    api.rpc(p_status="running")')
        self.assertEqual(german_on_the_managed_path(legacy_only), ([], []))

    def test_the_lerobot_feature_transcription(self):
        inputs, outputs = lerobot_policy_features(_ds(observation__environment_state=_float(4),
                                                      observation__images__scene=_video(names=("channels", "height",
                                                                                                "width"))))
        self.assertEqual(inputs["observation.images.gripper"], {"type": "VISUAL", "shape": [3, 480, 640]})
        self.assertEqual(inputs["observation.images.scene"], {"type": "VISUAL", "shape": [3, 480, 640]})
        self.assertEqual(inputs["observation.environment_state"], {"type": "ENV", "shape": [4]})
        self.assertEqual(outputs, {"action": {"type": "ACTION", "shape": [6]}})
        self.assertNotIn("timestamp", inputs)


if __name__ == "__main__":
    unittest.main()
