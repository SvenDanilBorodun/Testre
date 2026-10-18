"""What a managed ACT checkpoint and a trainable dataset must look like (spec §3.5).

Pure stdlib, no app import: the cloud API validates every checkpoint a worker
uploads (``ckpt-done``), every model it mirrors or publishes, and every
dataset before a managed training costs a credit; the lockstep tests load this
module by path and run it beside the robot's ``chunk_engine.validate_model_dir``
and the worker's ``training_handler`` constants.

Why these rules exist: a checkpoint is produced on a host we do not trust (a
RunPod Community machine). ``train_config.json`` is read back by LeRobot's
draccus parser on a resume, and draccus resolves ``policy.type`` against every
registered config (``groot`` loads remote code); ``peft``, ``env``,
``wandb``, ``use_rabc`` and ``output_dir`` are attacker-chosen paths into
code. A processor JSON that names a ``class`` makes LeRobot import it. So the
API accepts exactly what LeRobot 0.5.1 writes for an ACT training it started
(measured, spec Appendix A), and nothing else.

Errors are ``ArtifactRefused(reason)``: ``reason`` is an English detail for the
server log, never shown to a student (the student sees S7/S12/S20).
"""

from __future__ import annotations

import base64
import json
import re

# ---------------------------------------------------------------------------
# Constants shared with the worker (training_handler.py) and the robot
# (chunk_engine.py); robotis_ai_setup/tests/test_managed_training_lockstep.py
# compares them.
# ---------------------------------------------------------------------------

OUTPUT_DIR = "/tmp/training_output"

PM_FILES = (
    "config.json",
    "model.safetensors",
    "train_config.json",
    "policy_preprocessor.json",
    "policy_preprocessor_step_3_normalizer_processor.safetensors",
    "policy_postprocessor.json",
    "policy_postprocessor_step_0_unnormalizer_processor.safetensors",
)
TS_FILES = (
    "optimizer_param_groups.json",
    "optimizer_state.safetensors",
    "rng_state.safetensors",
    "training_step.json",
)
# The 7 files of a final checkpoint (step == total_steps) and the 11 of every other.
FINAL_RELPATHS = tuple(f"pretrained_model/{f}" for f in PM_FILES)
CHECKPOINT_RELPATHS = FINAL_RELPATHS + tuple(f"training_state/{f}" for f in TS_FILES)
LOG_RELPATH = "training_log.txt"

ACT_PRE_STEPS = ("rename_observations_processor", "to_batch_processor", "device_processor", "normalizer_processor")
ACT_POST_STEPS = ("unnormalizer_processor", "device_processor")
PRE_STATE_FILE = "policy_preprocessor_step_3_normalizer_processor.safetensors"
POST_STATE_FILE = "policy_postprocessor_step_0_unnormalizer_processor.safetensors"
PRE_PIPELINE_NAME = "policy_preprocessor"
POST_PIPELINE_NAME = "policy_postprocessor"
PRE_STATE_INDEX = 3
POST_STATE_INDEX = 0

# The keys LeRobot itself changes when it resumes (lerobot/configs/train.py:92-109);
# the worker drops them before comparing a host's train_config.json with its own.
IGNORED_RESUME_KEYS = (("resume",), ("num_workers",), ("checkpoint_path",), ("policy", "pretrained_path"))

MIB = 1024 * 1024
SIZE_CAPS = {
    "pretrained_model/config.json": MIB,
    "pretrained_model/model.safetensors": 512 * MIB,
    "pretrained_model/train_config.json": MIB,
    "pretrained_model/policy_preprocessor.json": MIB,
    "pretrained_model/policy_preprocessor_step_3_normalizer_processor.safetensors": MIB,
    "pretrained_model/policy_postprocessor.json": MIB,
    "pretrained_model/policy_postprocessor_step_0_unnormalizer_processor.safetensors": MIB,
    "training_state/optimizer_param_groups.json": MIB,
    "training_state/optimizer_state.safetensors": 1024 * MIB,
    "training_state/rng_state.safetensors": MIB,
    "training_state/training_step.json": 4 * 1024,
}
LOG_SIZE_CAP = 32 * MIB

# The small JSON files the API downloads and parses at ckpt-done.
PM_JSON_RELPATHS = (
    "pretrained_model/config.json",
    "pretrained_model/train_config.json",
    "pretrained_model/policy_preprocessor.json",
    "pretrained_model/policy_postprocessor.json",
)
TS_JSON_RELPATHS = ("training_state/training_step.json", "training_state/optimizer_param_groups.json")

# train_config.json, top level (spec §A.2), exactly these 29 keys.
TRAIN_CONFIG_KEYS = frozenset({
    "dataset", "env", "policy", "output_dir", "job_name", "resume", "seed", "cudnn_deterministic",
    "num_workers", "batch_size", "steps", "eval_freq", "log_freq", "tolerance_s", "save_checkpoint",
    "save_freq", "use_policy_training_preset", "optimizer", "scheduler", "eval", "wandb", "peft",
    "use_rabc", "rabc_progress_path", "rabc_kappa", "rabc_epsilon", "rabc_head_mode", "rename_map",
    "checkpoint_path",
})

ACT_BATCH = 8
DEFAULT_N_ACTION_STEPS = 15     # training_handler._build_training_command (F64)
DEFAULT_SEED = 1000             # LeRobot's TrainPipelineConfig.seed
DEFAULT_LOG_FREQ = 200          # LeRobot's TrainPipelineConfig.log_freq
NUM_WORKERS_MAX_CONFIG = 64

# Robot-side bounds (cloud_protocol.py / chunk_engine.py), restated here so this
# module stays import-free; the lockstep test compares them.
MAX_CAMERAS = 4
MAX_STATE_DIM = 32
MAX_ACTION_DIM = 32
MAX_CHUNK_SIZE = 1000
MAX_IMAGE_SIDE = 8192
NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")
IMAGE_PREFIX = "observation.images."
STATE_KEY = "observation.state"
ACTION_KEY = "action"
ENV_STATE_KEY = "observation.environment_state"

_HEX64 = re.compile(r"[0-9a-f]{64}")
_STEP_DIR = re.compile(r"\d{6,}")


class ArtifactRefused(Exception):
    """A checkpoint, model or dataset the API does not accept.

    ``reason`` is an English detail for the server log only. ``content`` is True
    for a rule on the CONTENT (it fails every checkpoint of that training the
    same way, FR-5), False for a transport or host fault (a size or a hash that
    does not match: the training goes on).
    """

    def __init__(self, reason: str, *, content: bool = True):
        super().__init__(reason)
        self.reason = reason
        self.content = content


class ManifestError(Exception):
    """An upload-url manifest the API refuses (422 bad_manifest, S11)."""


def step_id(step: int, total_steps: int) -> str:
    """LeRobot's checkpoint directory name (lerobot/utils/train_utils.py:37-39)."""
    return f"{step:0{max(6, len(str(total_steps)))}d}"


def output_dir_for(model_name: str) -> str:
    return OUTPUT_DIR + "/" + model_name.replace("/", "_")


def _is_int(v, lo, hi) -> bool:
    return type(v) is int and lo <= v <= hi


def _valid_md5_b64(v) -> bool:
    if type(v) is not str or len(v) != 24:
        return False
    try:
        raw = base64.b64decode(v, validate=True)
    except ValueError:
        return False
    return len(raw) == 16 and base64.b64encode(raw).decode("ascii") == v


def _manifest_entries(files, relpaths, caps) -> dict:
    if not isinstance(files, dict) or set(files) != set(relpaths):
        raise ManifestError("file list")
    out = {}
    for rel in relpaths:
        entry = files[rel]
        if not isinstance(entry, dict) or set(entry) != {"size", "sha256", "md5"}:
            raise ManifestError(f"entry shape {rel}")
        if not _is_int(entry["size"], 0, caps[rel]):
            raise ManifestError(f"size {rel}")
        if type(entry["sha256"]) is not str or not _HEX64.fullmatch(entry["sha256"]):
            raise ManifestError(f"sha256 {rel}")
        if not _valid_md5_b64(entry["md5"]):
            raise ManifestError(f"md5 {rel}")
        out[rel] = {"size": entry["size"], "sha256": entry["sha256"], "md5": entry["md5"]}
    return out


def validate_ckpt_manifest(step, files, *, total_steps: int, save_freq: int, resume_step: int) -> dict:
    """The manifest of one checkpoint upload (§3.5), normalised, or ManifestError."""
    if not _is_int(step, 1, total_steps):
        raise ManifestError("step range")
    if not (step % save_freq == 0 or step == total_steps):
        raise ManifestError("step cadence")
    if step <= (resume_step or 0):
        raise ManifestError("step not above the resume step")
    relpaths = FINAL_RELPATHS if step == total_steps else CHECKPOINT_RELPATHS
    return _manifest_entries(files, relpaths, SIZE_CAPS)


def validate_log_manifest(files) -> dict:
    return _manifest_entries(files, (LOG_RELPATH,), {LOG_RELPATH: LOG_SIZE_CAP})


def parse_json_bytes(data: bytes, rel: str):
    """Strict JSON (no NaN/Infinity), or a CONTENT refusal: a file the worker
    uploaded as JSON that does not parse fails every checkpoint the same way."""
    def _no_constant(name):
        raise ValueError(f"constant {name}")

    try:
        return json.loads(data.decode("utf-8"), parse_constant=_no_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ArtifactRefused(f"{rel}: not JSON") from None


# ---------------------------------------------------------------------------
# Features: the dataset's, and a config.json's (one rule set for both, FR-5).
# ---------------------------------------------------------------------------


def _shape(feature):
    if not isinstance(feature, dict):
        return None
    shape = feature.get("shape")
    if type(shape) is not list or not all(type(x) is int for x in shape):
        return None
    return shape


def act_feature_rules(inputs, outputs) -> dict:
    """The robot's feature rules (chunk_engine.validate_model_dir, verbatim in
    substance): state 1-D, cameras ``observation.images.<name>`` 3xHxW,
    1..MAX_CAMERAS, action 1-D, no other input. Returns the summary part."""
    if not isinstance(inputs, dict):
        raise ArtifactRefused("input_features")
    cameras, shapes, state_dim = [], {}, None
    for key, feature in inputs.items():
        shape = _shape(feature)
        if key == STATE_KEY:
            if shape is None or len(shape) != 1 or not _is_int(shape[0], 1, MAX_STATE_DIM):
                raise ArtifactRefused("state shape")
            state_dim = shape[0]
        elif type(key) is str and key.startswith(IMAGE_PREFIX):
            name = key[len(IMAGE_PREFIX):]
            if not NAME_RE.fullmatch(name) or not isinstance(feature, dict) or feature.get("type") != "VISUAL":
                raise ArtifactRefused("camera feature")
            if (shape is None or len(shape) != 3 or shape[0] != 3
                    or not all(_is_int(x, 1, MAX_IMAGE_SIDE) for x in shape[1:])):
                raise ArtifactRefused("visual shape")
            cameras.append(name)
            shapes[name] = list(shape)
        else:
            raise ArtifactRefused("unsupported input feature")
    if state_dim is None:
        raise ArtifactRefused("no observation.state")
    if not 1 <= len(cameras) <= MAX_CAMERAS:
        raise ArtifactRefused("camera count")
    if not isinstance(outputs, dict) or set(outputs) != {ACTION_KEY}:
        raise ArtifactRefused("output_features")
    action_shape = _shape(outputs[ACTION_KEY])
    if action_shape is None or len(action_shape) != 1 or not _is_int(action_shape[0], 1, MAX_ACTION_DIM):
        raise ArtifactRefused("action shape")
    return {"expected_cameras": cameras, "image_shapes": shapes, "state_dim": state_dim,
            "action_dim": action_shape[0]}


def dataset_policy_features(features) -> tuple[dict, dict]:
    """``(inputs, outputs)`` exactly as LeRobot 0.5.1 builds an ACT config from a
    dataset's ``meta/info.json`` ``features`` (``dataset_to_policy_features``,
    lerobot/datasets/feature_utils.py:169-209, then make_policy's split: outputs
    are the ACTION features, inputs every other one). Proven equal to LeRobot's
    own config.json for the proof dataset and lerobot/pusht (spec §B.7).

    A feature LeRobot would crash on (no dict, no shape, a visual feature whose
    shape is not 3-D or whose names cannot be indexed) is refused here.
    """
    if not isinstance(features, dict):
        raise ArtifactRefused("dataset features")
    out: dict = {}
    for key, ft in features.items():
        if type(key) is not str or not isinstance(ft, dict):
            raise ArtifactRefused("dataset feature shape")
        shape = ft.get("shape")
        if type(shape) not in (list, tuple) or not all(type(x) is int for x in shape):
            raise ArtifactRefused("dataset feature shape")
        shape = list(shape)
        dtype = ft.get("dtype")
        if dtype in ("image", "video"):
            if len(shape) != 3:
                raise ArtifactRefused("visual rank")
            names = ft.get("names")
            if type(names) not in (list, tuple) or len(names) < 3:
                raise ArtifactRefused("visual names")
            if names[2] in ("channel", "channels"):
                shape = [shape[2], shape[0], shape[1]]
            out[key] = {"type": "VISUAL", "shape": shape}
        elif key == ENV_STATE_KEY:
            out[key] = {"type": "ENV", "shape": shape}
        elif key.startswith("observation"):
            out[key] = {"type": "STATE", "shape": shape}
        elif key.startswith("action"):
            out[key] = {"type": "ACTION", "shape": shape}
    outputs = {k: v for k, v in out.items() if v["type"] == "ACTION"}
    inputs = {k: v for k, v in out.items() if k not in outputs}
    return inputs, outputs


def act_config_summary(config) -> dict:
    """The config.json rules of ``chunk_engine.validate_model_dir`` (its API
    twin; one shared case table runs through both, WP-C lockstep)."""
    if not isinstance(config, dict):
        raise ArtifactRefused("config.json is not an object")
    policy_type = config.get("type") or config.get("model_type")
    if policy_type != "act":
        raise ArtifactRefused("not act")
    if config.get("temporal_ensemble_coeff") is not None:
        raise ArtifactRefused("temporal ensembling")
    n_obs = config.get("n_obs_steps", 1)
    if type(n_obs) is not int or n_obs != 1:
        raise ArtifactRefused("n_obs_steps != 1")
    if config.get("use_peft") not in (None, False):
        raise ArtifactRefused("peft")
    summary = act_feature_rules(config.get("input_features"), config.get("output_features"))
    chunk_size = config.get("chunk_size")
    n_action_steps = config.get("n_action_steps")
    if not _is_int(chunk_size, 1, MAX_CHUNK_SIZE) or not _is_int(n_action_steps, 1, MAX_CHUNK_SIZE):
        raise ArtifactRefused("chunk_size / n_action_steps")
    summary.update({"type": "act", "chunk_size": chunk_size, "n_action_steps": n_action_steps})
    return summary


# ---------------------------------------------------------------------------
# Processor pipelines and train_config.json
# ---------------------------------------------------------------------------


def _check_pipeline(doc, name, steps_want, state_index, state_file, label) -> None:
    if not isinstance(doc, dict) or doc.get("name") != name:
        raise ArtifactRefused(f"{label}: name")
    steps = doc.get("steps")
    if type(steps) is not list or not all(isinstance(s, dict) for s in steps):
        raise ArtifactRefused(f"{label}: steps")
    if tuple(s.get("registry_name") for s in steps) != steps_want:
        raise ArtifactRefused(f"{label}: step list")
    for i, s in enumerate(steps):
        if "class" in s:
            raise ArtifactRefused(f"{label}: a step names a class")
        if i == state_index:
            if s.get("state_file") != state_file:
                raise ArtifactRefused(f"{label}: state_file")
        elif "state_file" in s:
            raise ArtifactRefused(f"{label}: unexpected state_file")


def check_processor_jsons(pre, post) -> None:
    """The ACT processor files exactly as LeRobot 0.5.1 writes them (§3.5)."""
    _check_pipeline(pre, PRE_PIPELINE_NAME, ACT_PRE_STEPS, PRE_STATE_INDEX, PRE_STATE_FILE, "preprocessor")
    _check_pipeline(post, POST_PIPELINE_NAME, ACT_POST_STEPS, POST_STATE_INDEX, POST_STATE_FILE, "postprocessor")


def expected_n_action_steps(training_params: dict) -> int:
    """training_handler._build_training_command's rule (F64/F66), for act."""
    n = training_params.get("n_action_steps")
    if isinstance(n, int) and n > 0:
        return n
    return DEFAULT_N_ACTION_STEPS


def _resume_path_ok(value, output_dir: str, suffix: str) -> bool:
    if value is None:
        return True
    if type(value) is not str:
        return False
    head = output_dir + "/checkpoints/"
    if not value.startswith(head) or not value.endswith(suffix):
        return False
    middle = value[len(head):len(value) - len(suffix)] if suffix else value[len(head):]
    return bool(_STEP_DIR.fullmatch(middle))


def check_train_config(tc, *, dataset_name: str, training_params: dict, output_dir: str) -> None:
    """The API's structural check of a train_config.json (§3.5)."""
    def need(cond, what):
        if not cond:
            raise ArtifactRefused(f"train_config: {what}")

    need(isinstance(tc, dict), "not an object")
    need(set(tc) == TRAIN_CONFIG_KEYS, "top-level keys")
    ds = tc["dataset"]
    need(isinstance(ds, dict), "dataset")
    need(ds.get("repo_id") == dataset_name, "dataset.repo_id")
    need(ds.get("revision") == training_params.get("dataset_revision"), "dataset.revision")
    need("root" in ds and ds["root"] is None, "dataset.root")
    need("episodes" in ds and ds["episodes"] is None, "dataset.episodes")
    need(ds.get("streaming") is False, "dataset.streaming")
    it = ds.get("image_transforms")
    need(isinstance(it, dict) and it.get("enable") is False, "dataset.image_transforms.enable")
    need(ds.get("video_backend") == "pyav", "dataset.video_backend")
    for key in ("env", "peft", "rabc_progress_path", "scheduler"):
        need(tc[key] is None, key)
    need(tc["use_rabc"] is False, "use_rabc")
    wandb = tc["wandb"]
    need(isinstance(wandb, dict) and wandb.get("enable") is False, "wandb.enable")
    need(tc["rename_map"] == {} and isinstance(tc["rename_map"], dict), "rename_map")
    policy = tc["policy"]
    need(isinstance(policy, dict), "policy")
    need(policy.get("type") == "act", "policy.type")
    need(policy.get("device") in ("cuda", "cpu"), "policy.device")
    need(policy.get("use_peft") is False, "policy.use_peft")
    need(policy.get("push_to_hub") is False, "policy.push_to_hub")
    need("temporal_ensemble_coeff" in policy and policy["temporal_ensemble_coeff"] is None,
         "policy.temporal_ensemble_coeff")
    need(type(policy.get("n_obs_steps")) is int and policy.get("n_obs_steps") == 1, "policy.n_obs_steps")
    need(policy.get("vision_backbone") == "resnet18", "policy.vision_backbone")
    need(policy.get("pretrained_backbone_weights") == "ResNet18_Weights.IMAGENET1K_V1",
         "policy.pretrained_backbone_weights")
    need(policy.get("use_amp") is False, "policy.use_amp")
    want_nas = expected_n_action_steps(training_params)
    need(type(policy.get("n_action_steps")) is int and policy.get("n_action_steps") == want_nas,
         "policy.n_action_steps")
    need(tc["output_dir"] == output_dir, "output_dir")
    need(tc["job_name"] == "act", "job_name")
    want_seed = training_params.get("seed", DEFAULT_SEED)
    need(type(tc["seed"]) is int and tc["seed"] == want_seed, "seed")
    need(type(tc["batch_size"]) is int and tc["batch_size"] == ACT_BATCH, "batch_size")
    need(type(tc["steps"]) is int and tc["steps"] == training_params.get("steps"), "steps")
    need(type(tc["eval_freq"]) is int and tc["eval_freq"] == 0, "eval_freq")
    want_log = training_params.get("log_freq", DEFAULT_LOG_FREQ)
    need(type(tc["log_freq"]) is int and tc["log_freq"] == want_log, "log_freq")
    need(type(tc["save_freq"]) is int and tc["save_freq"] == training_params.get("save_freq"), "save_freq")
    need(tc["save_checkpoint"] is True, "save_checkpoint")
    need(tc["use_policy_training_preset"] is True, "use_policy_training_preset")
    need(type(tc["resume"]) is bool, "resume")
    need(_is_int(tc["num_workers"], 0, NUM_WORKERS_MAX_CONFIG), "num_workers")
    need(_resume_path_ok(policy.get("pretrained_path"), output_dir, "/pretrained_model"),
         "policy.pretrained_path")
    need(_resume_path_ok(tc["checkpoint_path"], output_dir, ""), "checkpoint_path")


def validate_model_jsons(docs: dict, *, dataset_name: str, training_params: dict, output_dir: str) -> dict:
    """The four pretrained_model/ JSON files of a checkpoint or a final model:
    config.json's ACT rules, train_config.json's structure, config.json ==
    train_config["policy"] exactly (SR-33), the processor pipelines."""
    config = docs.get("pretrained_model/config.json")
    tc = docs.get("pretrained_model/train_config.json")
    summary = act_config_summary(config)
    check_train_config(tc, dataset_name=dataset_name, training_params=training_params, output_dir=output_dir)
    if config != tc["policy"]:
        raise ArtifactRefused("config.json != train_config.policy")
    check_processor_jsons(docs.get("pretrained_model/policy_preprocessor.json"),
                          docs.get("pretrained_model/policy_postprocessor.json"))
    return summary


def validate_checkpoint_jsons(docs: dict, *, step: int, total_steps: int, dataset_name: str,
                              training_params: dict, output_dir: str) -> dict:
    """Every JSON file of one uploaded checkpoint (ckpt-done, §4.5)."""
    summary = validate_model_jsons(docs, dataset_name=dataset_name, training_params=training_params,
                                   output_dir=output_dir)
    if step < total_steps:
        ts = docs.get("training_state/training_step.json")
        if not (isinstance(ts, dict) and set(ts) == {"step"} and type(ts["step"]) is int and ts["step"] == step):
            raise ArtifactRefused("training_step.json")
        groups = docs.get("training_state/optimizer_param_groups.json")
        if type(groups) is not list or not groups or not all(isinstance(g, dict) for g in groups):
            raise ArtifactRefused("optimizer_param_groups.json")
    return summary
