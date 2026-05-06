# 11 — Modal Training Worker

> **Layer:** Cloud GPU compute
> **Location:** `Testre/robotis_ai_setup/modal_training/`
> **Owner:** Our code
> **Read this before:** editing `modal_app.py` or `training_handler.py`, bumping LeRobot, changing the image build, or debugging stuck training.

---

## 1. Files

```
modal_training/
├── modal_app.py              # Modal app definition + image + train function entry
└── training_handler.py       # The actual training pipeline (preflight → subprocess → upload)
```

Plus the deploy command: `modal deploy modal_app.py`.

---

## 2. modal_app.py — image + function

### Image build chain (lines 23–50)

```python
image = (
    modal.Image.from_registry("nvidia/cuda:12.1.1-devel-ubuntu22.04", add_python="3.11")
    .apt_install("git", "ffmpeg", "clang", "build-essential")  # clang+build-essential for evdev native deps
    .pip_install(
        f"lerobot[pi0] @ git+https://github.com/huggingface/lerobot.git@{LEROBOT_COMMIT}",
        "huggingface_hub",
        "supabase",
    )
    .pip_install(
        "torch",
        "torchvision",
        index_url="https://download.pytorch.org/whl/cu121",   # CRITICAL: not cu130
        extra_options="--force-reinstall",                     # overwrite anything lerobot pulled
    )
    .run_commands("python -m pip uninstall -y torchcodec || true")  # pyav fallback
    .env({"PYTHONUNBUFFERED": "1"})
    .add_local_python_source("training_handler")
)
```

**Critical pinning:**
- `LEROBOT_COMMIT = "989f3d05ba47f872d75c587e76838e9cc574857a"` (line 19) — must match the snapshot in `physical_ai_tools/lerobot/` and the base `physical-ai-server` image. Verified against `huggingface/lerobot` upstream: this is "[Async Inference] Merge Protos & refactoring (#1480)" from 2025-07-23.
- `index_url="https://download.pytorch.org/whl/cu121"` + `--force-reinstall` — without this, default pip pulls `cu130` wheels which are incompatible with the cu121 base image runtime
- `pip uninstall torchcodec` — LeRobot pulls torchcodec but it binds to libs that fail on this image; pyav fallback works

### Function definition (lines 55–61)

```python
@app.function(
    image=image,
    gpu="L4",
    timeout=7*3600,                  # hard 7h outer bound
    secrets=[modal.Secret.from_name("edubotics-training-secrets")],
    min_containers=0,                # scale to zero when idle (cold-start on demand)
)
def train(dataset_name, model_name, model_type, training_params, training_id, worker_token):
    return run_training(...)
```

**Secret `edubotics-training-secrets`** injects `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `HF_TOKEN` as env vars. **Anon key** (not service-role) — worker auth happens via per-row `worker_token`.

### Smoke test (lines 83–96)

`@app.function` `verify` checks: torch importable, CUDA available, required secrets present.

---

## 3. training_handler.run_training — lifecycle

Function: `training_handler.py:run_training` (lines 470–669).

### Phase 1: Setup (lines 483–499)

- Build `_current_job` global dict with refs to creds, worker_token, training_id, model_name, proc=None
- HF login if HF_TOKEN present (`login(token=hf_token)`, line 499)

### Phase 2: Preflight Dataset (lines 503–511)

`_preflight_dataset(dataset_name, hf_token)` (60 s timeout). Validates:
- `meta/info.json` exists and parses
- `codebase_version == "v2.1"` (German error otherwise)
- `fps` is a valid number
- `observation.state` and `action` features exist with matching joint names
- Joint count is 4–20
- ≥1 camera in `observation.images.*`

On `ValueError` → mark Supabase `failed` with German message, return early.

### Phase 3: Mark Running (lines 513–516)

`_update_supabase_status(..., "running")` via RPC.

### Phase 4: Build &amp; Spawn Subprocess (lines 518–541)

```python
total_steps = training_params.get("steps", 100000)
cmd = _build_training_command(dataset_name, model_type, model_name, training_params)
# ["python", "-m", "lerobot.scripts.train", "--policy.type=...", "--policy.device=cuda",
#  "--dataset.repo_id=...", "--output_dir=...", "--policy.push_to_hub=false", "--eval_freq=0",
#  optional --seed, --num_workers, --batch_size, --steps, --log_freq, --save_freq]

env = {**os.environ, "PYTHONUNBUFFERED": "1"}
if hf_token: env["HF_TOKEN"] = hf_token

proc = subprocess.Popen(cmd, stdout=PIPE, stderr=STDOUT, text=True, bufsize=1, env=env)
```

`stderr=subprocess.STDOUT` → Python logging (which goes to stderr) is captured for progress parsing.

### Phase 5: Streaming Output &amp; Progress Parsing (lines 543–605)

- Bounded deque (`maxlen=4000`) of stdout lines for error reporting
- Reader thread (lines 584–585) loops `for line in proc.stdout`:
  - Echo to console
  - Append to deque
  - Parse step + loss

#### Regexes (lines 547–548)

```python
step_re = re.compile(r"step[:\s]+(\d+\.?\d*[KMBkmb]?)")
loss_re = re.compile(r"loss[:\s]+([\d.]+(?:e[+-]?\d+)?)")
```

#### Abbreviated number parser (`_parse_abbreviated_number`, lines 64–82)

`"50K"` → 50000, `"1.5M"` → 1500000, `"1B"` → 1_000_000_000. Returns int or None.

#### Step deduplication (line 564)

```python
if step <= last_progress_step: continue
```

Skip duplicates and backward steps.

#### Supabase RPC retry (lines 567–580)

3 attempts, exponential backoff `0.5 * 2^attempt` seconds. German warning on failure. Total budget ~1.5 s of sleep.

### Phase 6: Subprocess Wait + Timeout (lines 587–602)

```python
timeout_hours = training_params.get("timeout_hours", 5)   # default 5h, capped to 7h Modal max
proc.wait(timeout=timeout_hours * 3600)
```

On `subprocess.TimeoutExpired`: kill, wait 10 s for death, mark Supabase failed (German: "Training Zeitlimit überschritten ({timeout_hours}h Limit)").

Reader thread joined with 10 s timeout.

### Phase 7: Check Exit Code (lines 605–616)

If `returncode != 0`: truncate output to 1000 head + `...[truncated]...` + 1000 tail. Mark Supabase failed with German error context.

### Phase 8: Mark 100% Progress (lines 618–622)

Before upload, push `(current_step=total_steps, total_steps=total_steps)` so UI shows 100%.

### Phase 9: Upload to HuggingFace (lines 624–641)

`_upload_model_to_hf(model_name, hf_token)`:
1. `HfApi(token=hf_token).create_repo(model_name, repo_type="model", exist_ok=True)`
2. Locate checkpoint: `{output_path}/checkpoints/last/pretrained_model/` (else `output_path.rglob("pretrained_model")` fallback)
3. `hf_api.upload_large_folder(repo_id=model_name, folder_path=checkpoint_dir, repo_type="model")`
4. Verify: `hf_api.repo_info(repo_id=model_name, repo_type="model")` (raises if missing)
5. Return `f"https://huggingface.co/{model_name}"`

On exception: German error ("Training erfolgreich, aber Model-Upload zu HuggingFace fehlgeschlagen…"), mark failed, return.

### Phase 10: Final Status &amp; Cleanup (lines 643–668)

Mark `succeeded` with `model_url`. Finally block: kill proc if still running, cleanup `/tmp/training_output/`, null `_current_job`.

---

## 4. Worker token + RPC contract

### Storage

In `_current_job` global dict under `"worker_token"`. Passed to every progress RPC.

### RPC call (`_call_progress_rpc`, lines 110–120)

```python
client.rpc("update_training_progress", {
    "p_training_id": training_id,
    "p_token": worker_token,
    "p_status": status,
    "p_current_step": current_step,
    "p_total_steps": total_steps,
    "p_current_loss": current_loss,
    "p_error_message": error_message,
}).execute()
```

Postgres function (in `migration.sql` rewritten by `006_loss_history.sql`):
- Validates `WHERE id = p_training_id AND worker_token = p_token`
- On terminal status (`succeeded`/`failed`/`canceled`): sets `terminated_at = NOW()`, **nulls worker_token** (one-way: token can't be reused)
- On non-terminal: appends to `loss_history` JSONB array, downsamples if &gt; 300 entries
- Raises `P0001` if no row updated (token mismatch)

**Worker has no direct table access** — only via this RPC. Tight scope.

### Token nulling (line 668)

After completion (success or failure), `_current_job = None` to release references. Prevents signal handler from attempting writes after cleanup.

---

## 5. Signal handlers (lines 403–464)

Registered on module import (lines 460–464). Triggered by Modal preemption (30 s grace), manual cancel, or 7h timeout.

### Actions

1. Retrieve `_current_job` (line 410)
2. Kill subprocess if running: `proc.kill(); proc.wait(timeout=5)` (lines 418–424)
3. Retry Supabase status update 3x with short backoff (lines 432–451):
   - Sleep 0.5 s, 1.0 s before attempts 2 and 3
   - Total ~1.5 s sleep budget — fits Modal's 30 s SIGINT grace
   - Catches all exceptions
4. Cleanup `/tmp/training_output/` (line 453)
5. `sys.exit(0)` (line 454)

### Status message

German: `"Worker wurde vom Cloud-Anbieter beendet. Bitte Training neu starten."`

---

## 6. Error message catalog (German)

| When | Message |
|---|---|
| Preflight timeout | `"Preflight hat das Zeitlimit (60s) überschritten — HuggingFace Hub erreichbar?..."` |
| Dataset not found | `"Dataset ... wurde auf HuggingFace nicht gefunden oder ist privat..."` |
| HF HTTP error | `"Dataset ... konnte nicht geladen werden: {err}"` |
| Bad info.json | `"Dataset ... hat ein ungueltiges meta/info.json: {e}"` |
| codebase_version mismatch | `"Dataset ... hat codebase_version='...', erwartet wird 'v2.1'..."` |
| Invalid FPS | `"Dataset ... hat keine gueltige 'fps' Angabe"` |
| Missing joint feature | `"Dataset ... hat kein '{feature_key}' Feature ({label})..."` |
| Joint count out of bounds | `"Dataset ... hat {N} {label}-Gelenke — erwartet werden 4-20..."` |
| Mismatched joint names | `"Dataset ... hat unterschiedliche Gelenk-Namen fuer observation.state und action..."` |
| No cameras | `"Dataset ... enthaelt keine Kamera-Features..."` |
| Training timeout | `"Training Zeitlimit ueberschritten ({timeout_hours}h Limit)"` |
| HF upload failed | `"Training erfolgreich, aber Model-Upload zu HuggingFace fehlgeschlagen..."` |
| Shutdown signal | `"Worker wurde vom Cloud-Anbieter beendet. Bitte Training neu starten."` |

These appear in Supabase `trainings.error_message` → React reads them → student sees German error.

---

## 7. Hidden assumptions / gotchas

1. **Force-reinstall torch+cu121 must run AFTER lerobot install.** lerobot pulls a torch — without `--force-reinstall`, you get whichever torch lerobot pulled (might be cu130).
2. **`pip uninstall torchcodec || true`** — `|| true` ensures script doesn't fail if torchcodec wasn't installed in the first place.
3. **`codebase_version: "v2.1"` is hardcoded** — bumping LeRobot probably bumps this string. Modal preflight enforces match. Need migration script for old datasets.
4. **`PYTHONUNBUFFERED=1` set in TWO places** — image env (line 48) AND subprocess env (line 526). Defensive belt-and-suspenders so progress lines flush immediately.
5. **Daemon reader thread killed on process exit** — `daemon=True`. Reader join uses 10 s timeout to give it time to drain stdout.
6. **`maxlen=4000` deque** — bounds memory on long failures; older lines drop silently when full.
7. **HF_TOKEN can be empty** — `os.environ.get("HF_TOKEN", "")`. Allows preflight on public datasets even if secret is missing. Training fails later if dataset is private.
8. **Step dedup also rejects backward steps** — defensive against rare LeRobot logging quirks.
9. **Supabase RPC retry uses `2^attempt`** — 0.5, 1, 2 — total ~3.5 s including a final attempt. German warning on each failure.

---

## 8. Configuration env vars (full list)

| Var | Required | Default | Notes |
|---|---|---|---|
| `SUPABASE_URL` | ✅ | — | from Modal Secret |
| `SUPABASE_ANON_KEY` | ✅ | — | anon key (not service-role); auth via worker_token |
| `HF_TOKEN` | ❌ | `""` | required for private datasets / model upload |
| `PYTHONUNBUFFERED` | (baked) | `"1"` | force stdout flush |

See [`04-env-vars.md`](04-env-vars.md) §3.

---

## 9. Observability

- **Console output**: every stdout/stderr line from the training subprocess is printed by the reader thread (line 554). View via `modal app logs edubotics-training`.
- **Last 4000 lines** ring-buffered in deque for inclusion in failure error_message (truncated to 2000 chars head/tail in error report).
- **Progress writes** to Supabase: every step increase, with 3x retry. View `trainings.last_progress_at` to detect stalls.
- **Loss history**: appended via RPC (006_loss_history.sql). Downsampled at Postgres level when > 300 points.

---

## 10. Deployment

```bash
cd robotis_ai_setup/modal_training
modal deploy modal_app.py
```

Or for dev:
```bash
modal serve modal_app.py    # auto-reload on save
```

Update Modal Secret:
```bash
modal secret create edubotics-training-secrets --from-dotenv .env
```

(`.env` should contain `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `HF_TOKEN`.)

---

## 11. Cross-references

- Cloud API dispatch (the `spawn` caller): [`10-cloud-api.md`](10-cloud-api.md) §5
- Supabase RPCs (`update_training_progress`, RLS, downsampling): [`12-supabase.md`](12-supabase.md)
- LeRobot version alignment across surfaces: [`01-architecture.md`](01-architecture.md) §5.4
- Operations (rotate HF_TOKEN, investigate stuck training): [`20-operations.md`](20-operations.md) §1, §2
- Known issues for this layer: [`21-known-issues.md`](21-known-issues.md) §3.7

---

**Last verified:** 2026-05-04.
