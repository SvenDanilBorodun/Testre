"""Cloud-Inferenz Modal app (ONE file; variant and snapshot baked at deploy). Deploy (Modal CLI >= 1.6):
    modal deploy inference_app.py                                    # prod
    EDUBOTICS_INFERENCE_VARIANT=dev modal deploy inference_app.py    # dev (static backend, 2 containers;
        # DEV_SESSION_TOKEN + the 16 tokens session_backend.dev_session_token derives from it, one session each)
    EDUBOTICS_INFERENCE_SNAPSHOT=0 modal deploy inference_app.py     # without a memory snapshot (§10.3(c))
A sessioned server: Modal refuses every request without a live session's edge token before any container
starts; the cloud API mints them (Server.from_name(app, "CloudInference").sessions.start()).
"""
import os
import subprocess
import threading
import time
from pathlib import Path

import modal

LEROBOT_VERSION = "0.5.1"            # Rule §5 — the SIXTH pin site (tests/test_lerobot_pin_lockstep.py)
SERVER_PORT = 8000
VARIANTS = {
    "prod": {"app": "edubotics-inference", "max_containers": 20, "secret": "edubotics-inference-secrets",
             "required": ["SUPABASE_URL", "SUPABASE_ANON_KEY", "HF_TOKEN"]},
    "dev": {"app": "edubotics-inference-dev", "max_containers": 2, "secret": "edubotics-inference-dev-secrets",
            "required": ["DEV_SESSION_TOKEN", "DEV_MODEL_REPO", "DEV_MODEL_REVISION"]},   # public model: no HF_TOKEN
}
VARIANT = os.environ.get("EDUBOTICS_INFERENCE_VARIANT", "prod")    # baked into the image env below, so the
if VARIANT not in VARIANTS:                                          # container's re-import agrees
    raise RuntimeError(f"EDUBOTICS_INFERENCE_VARIANT must be one of {sorted(VARIANTS)}")
CFG = VARIANTS[VARIANT]
# Memory snapshot of the imports (step 0: EU T4 typical cold start 4 s with it). Configuration only:
# EDUBOTICS_INFERENCE_SNAPSHOT=0 deploys without it (§10.3(c) decides; both forms import).
SNAPSHOT = os.environ.get("EDUBOTICS_INFERENCE_SNAPSHOT", "1") == "1"
HERE = Path(__file__).resolve().parent
# Modal re-imports this module in the container as /root/inference_app.py, where HERE.parents has one entry: an
# unguarded parents[1] raises IndexError there [proved]; add_local_file is lazy, so a placeholder root is harmless.
_ROOT = HERE.parents[1] if len(HERE.parents) > 1 else Path("/")
SHARED = _ROOT / "physical_ai_tools" / "physical_ai_server" / "physical_ai_server" / "inference"


def _server_sha() -> str:            # deploy host: git; container: the baked env var
    sha = os.environ.get("EDUBOTICS_INFERENCE_SHA")
    if sha:
        return sha
    try:
        return subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], cwd=HERE, capture_output=True,
                              text=True, timeout=5).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


SERVER_SHA = _server_sha()
app = modal.App(CFG["app"])
image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("build-essential", "git")                     # lerobot pulls evdev (C extension)
    .pip_install(f"lerobot=={LEROBOT_VERSION}", "scipy>=1.14.0,<1.18", "accelerate>=1.10.0,<2.0.0",
                 "huggingface_hub==1.23.0", "opencv-python-headless==4.10.0.84", "fastapi[standard]==0.115.12",
                 "httpx==0.28.1", "datasets==4.8.5")
    # The swap replaces torch 2.10.0's cu128 nvidia-* set and triton 3.6.0 (the first resolve) with torch 2.7.0's
    # cu126 set, so it keeps --force-reinstall (never --no-deps). It also re-resolves torch's shared deps from the
    # cu126 index (deploy log: numpy 2.5.2, fsspec 2026.7.0); the next step restores lerobot's numpy range and
    # datasets 4.8.5's fsspec cap from PyPI (V1-10).
    .pip_install("torch==2.7.0", "torchvision==0.22.0", index_url="https://download.pytorch.org/whl/cu126",
                 extra_options="--force-reinstall")
    .pip_install("numpy>=2.0.0,<2.3.0", "fsspec==2026.2.0")
    .run_commands("python -m pip uninstall -y torchcodec || true",
                  "python -c \"import torchvision; torchvision.models.resnet18(weights='IMAGENET1K_V1')\"")  # D11
    .env({"PYTHONUNBUFFERED": "1", "HF_HUB_DISABLE_TELEMETRY": "1",
          "OPENCV_IO_MAX_IMAGE_PIXELS": "16777216",                                               # D25 belt
          "EDUBOTICS_INFERENCE_SHA": SERVER_SHA, "EDUBOTICS_INFERENCE_VARIANT": VARIANT,
          "EDUBOTICS_INFERENCE_SNAPSHOT": "1" if SNAPSHOT else "0"})
    .add_local_file(SHARED / "cloud_protocol.py", "/root/cloud_protocol.py")
    .add_local_file(SHARED / "chunk_engine.py", "/root/chunk_engine.py")
    .add_local_file(HERE / "server_core.py", "/root/server_core.py")
    .add_local_file(HERE / "session_backend.py", "/root/session_backend.py")
)
SECRET = modal.Secret.from_name(CFG["secret"], required_keys=CFG["required"])


# 5 robots per T4 (V1-2, owner): the real engine costs 26-28 ms per chunk under load, 6 put the GPU thread at
# ~97 %. server_core.LRU_MAX_MODELS = max_concurrency + 2 (D27, tests/test_inference_app_variants.py).
@app.server(image=image, gpu="T4", port=SERVER_PORT, routing_region="eu-west", compute_region="eu",
            max_concurrency=5, target_concurrency=4, min_containers=0, max_containers=CFG["max_containers"],
            scaledown_window=300, startup_timeout=300, enable_memory_snapshot=SNAPSHOT, cpu=2.0, secrets=[SECRET])
@modal.sessioned()
class CloudInference:                # no __init__, no @modal.method, no @modal.concurrent: refused on a server
    @modal.enter(snap=SNAPSHOT)
    def load_libraries(self):
        import cv2, fastapi, httpx, torch, uvicorn                          # noqa: F401
        import lerobot.policies.act.modeling_act, lerobot.policies.factory  # noqa: F401
        import lerobot.policies.utils                                       # noqa: F401

    @modal.enter(snap=False)
    def serve(self):                 # after a restore: CUDA once, then uvicorn on SERVER_PORT in a thread;
        # build_app warms every model it loads with one zero chunk on its GPU thread (V1-3), never in a snapshot
        import uvicorn
        import server_core
        import session_backend
        cache = server_core.EngineCache(device="cuda", max_models=server_core.LRU_MAX_MODELS)
        cache.init_device()
        backend = (session_backend.StaticSessionBackend.from_env() if VARIANT == "dev"
                   else session_backend.SupabaseSessionBackend.from_env())
        cfg = uvicorn.Config(server_core.build_app(backend, cache, SERVER_SHA), host="0.0.0.0", port=SERVER_PORT,
                             log_level="warning", access_log=False)      # HTTPS only (O7')
        self.server = uvicorn.Server(cfg)
        threading.Thread(target=self.server.run, name="uvicorn", daemon=True).start()
        deadline = time.monotonic() + 60
        while not getattr(self.server, "started", False):
            if time.monotonic() > deadline:
                raise RuntimeError("uvicorn did not start")
            time.sleep(0.05)

    @modal.exit()
    def stop(self):
        server = getattr(self, "server", None)
        if server is not None:
            server.should_exit = True


@app.function(image=image, gpu="T4", region="eu", secrets=[SECRET], timeout=600)
def smoke_test() -> dict:
    """Operator check (``modal run inference_app.py::smoke_test``): imports, CUDA, the variant's secrets, and
    one chunk of a random-init ACT of the trained shape through the real ChunkEngine. Plain types only, so a
    torch-less Mac can deserialize the answer."""
    import json
    import tempfile

    import numpy as np
    import torch
    from lerobot.configs.types import FeatureType, PolicyFeature
    from lerobot.policies.act.configuration_act import ACTConfig
    from lerobot.policies.act.modeling_act import ACTPolicy
    from lerobot.policies.factory import make_pre_post_processors

    import chunk_engine

    cuda = bool(torch.cuda.is_available())
    device = "cuda" if cuda else "cpu"
    cams = ("gripper", "scene")
    inputs = {f"observation.images.{c}": PolicyFeature(type=FeatureType.VISUAL, shape=(3, 480, 640)) for c in cams}
    inputs["observation.state"] = PolicyFeature(type=FeatureType.STATE, shape=(6,))
    outputs = {"action": PolicyFeature(type=FeatureType.ACTION, shape=(6,))}
    config = ACTConfig(input_features=inputs, output_features=outputs, chunk_size=100, n_action_steps=15,
                       device=device, pretrained_backbone_weights=None)
    rng = np.random.default_rng(0)
    stats = {k: {"mean": torch.tensor(rng.random(f.shape).astype(np.float32)),
                 "std": torch.tensor((rng.random(f.shape) + 0.5).astype(np.float32))}
             for k, f in {**inputs, **outputs}.items()}
    pre, post = make_pre_post_processors(policy_cfg=config, dataset_stats=stats)
    model_dir = tempfile.mkdtemp(prefix="act-smoke-")
    ACTPolicy(config).save_pretrained(model_dir)
    pre.save_pretrained(model_dir)
    post.save_pretrained(model_dir)
    with open(os.path.join(model_dir, "edubotics_model_meta.json"), "w") as f:
        json.dump({"dataset_fps": 30, "policy_type": "act"}, f)
    engine = chunk_engine.ChunkEngine.load(model_dir, device)
    images = {c: (rng.random((480, 640, 3)) * 255).astype(np.uint8) for c in cams}
    actions, gpu_ms = engine.chunk(images, rng.standard_normal(6).astype(np.float32), 50)
    return {"variant": VARIANT, "snapshot": SNAPSHOT, "torch": str(torch.__version__), "cuda": cuda,
            "missing_secrets": [k for k in CFG["required"] if not os.environ.get(k)],
            "chunk_shape": [int(x) for x in actions.shape], "gpu_ms": float(gpu_ms)}
