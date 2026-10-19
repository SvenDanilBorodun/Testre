"""A local stand-in for RunPod REST v2 (spec §3.6) whose pods are docker containers. E2E only.

    FAKE_RUNPOD_IMAGE=edubotics-trainer-proof:local \\
    FAKE_RUNPOD_API_KEY=<the key the API is configured with> \\
    FAKE_RUNPOD_ENV_OVERRIDES='{"EDUBOTICS_API_URL": "http://host.docker.internal:8000",
                                "SUPABASE_URL": "http://host.docker.internal:54321"}' \\
    python fake_runpod.py --port 8765          # then RUNPOD_API_BASE=http://127.0.0.1:8765 for the API

Exactly the calls the cloud API makes (§3.6: catalog gpus + datacenters, POST/GET/
DELETE pods, the paginated list) plus PATCH, POST /action and GET /logs, in the
pinned OpenAPI's shapes (errors are application/problem+json {title, status, detail}).
A created pod is `docker run -d` of FAKE_RUNPOD_IMAGE (the CPU proof image, whose
entrypoint is harness_entry.py) with the create body's env; FAKE_RUNPOD_ENV_OVERRIDES
(JSON) replaces values so a pod reaches the local stack. RunPod's own variables are
injected like RunPod does: RUNPOD_POD_ID, RUNPOD_CPU_COUNT, RUNPOD_API_KEY (a key
valid for that pod only), and RUNPOD_API_BASE (this fake, as a container sees it).

Admin routes (same admin key): POST /_fake/stock, POST /_fake/next_create_error,
POST /_fake/pods/{id}/kill, POST /_fake/pods/{id}/set {dataCenterId, cudaVersion},
GET /_fake/pods, POST /_fake/reset.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import secrets
import subprocess
import tempfile
import threading

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

ADMIN_KEY = os.environ.get("FAKE_RUNPOD_API_KEY", "fake-runpod-key")
POD_IMAGE = os.environ.get("FAKE_RUNPOD_IMAGE", "edubotics-trainer-proof:local")
ENV_OVERRIDES = json.loads(os.environ.get("FAKE_RUNPOD_ENV_OVERRIDES") or "{}")
CPU_COUNT = int(os.environ.get("FAKE_RUNPOD_CPU_COUNT", "4"))
CONTAINER_LABEL = "edubotics.fake-runpod=1"

# Region and country per data centre. The real catalog carries no country; the
# country here only drives the countryCodes filter of the gpus call.
DATACENTERS = [
    {"id": "EU-RO-1", "name": "Europe Romania 1", "region": "EUROPE", "country": "RO", "compliance": ["GDPR"]},
    {"id": "EU-SE-1", "name": "Europe Sweden 1", "region": "EUROPE", "country": "SE", "compliance": ["GDPR"]},
    {"id": "EU-NL-1", "name": "Europe Netherlands 1", "region": "EUROPE", "country": "NL", "compliance": ["GDPR"]},
    {"id": "EU-CZ-1", "name": "Europe Czechia 1", "region": "EUROPE", "country": "CZ", "compliance": []},
    {"id": "EUR-IS-1", "name": "Europe Iceland 1", "region": "EUROPE", "country": "IS", "compliance": []},
    {"id": "EU-GB-1", "name": "Europe United Kingdom 1", "region": "EUROPE", "country": "GB", "compliance": []},
    {"id": "US-KS-2", "name": "US Kansas 2", "region": "NORTH_AMERICA", "country": "US", "compliance": []},
    {"id": "XX-UNKNOWN-1", "name": "Unknown 1", "region": "UNKNOWN", "country": "", "compliance": []},
]
DEFAULT_STOCK = [
    {"id": "NVIDIA RTX A5000", "cloud": "BOTH", "dataCenters": ["EU-RO-1", "EU-SE-1"],
     "availability": "HIGH", "cudaVersions": ["12.8"], "price": 0.16, "memory": 24},
]

_lock = threading.Lock()
_pods: dict[str, dict] = {}
_stock: list[dict] = [dict(s) for s in DEFAULT_STOCK]
_create_errors: list[dict] = []

app = FastAPI(title="fake-runpod")


# ---------------------------------------------------------------- helpers
def problem(status: int, detail: str, headers: dict | None = None) -> JSONResponse:
    titles = {400: "Bad Request", 401: "Unauthorized", 402: "Payment Required", 403: "Forbidden",
              404: "Not Found", 409: "Conflict", 422: "Unprocessable Entity",
              429: "Too Many Requests", 500: "Internal Server Error", 503: "Service Unavailable"}
    return JSONResponse({"title": titles.get(status, "Error"), "status": status, "detail": detail},
                        status_code=status, media_type="application/problem+json", headers=headers)


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def bearer(request: Request) -> str | None:
    h = request.headers.get("authorization", "")
    return h[7:] if h.lower().startswith("bearer ") else None


def is_admin(request: Request) -> bool:
    return secrets.compare_digest(bearer(request) or "", ADMIN_KEY)


def may_touch(request: Request, pod_id: str) -> bool:
    if is_admin(request):
        return True
    pod = _pods.get(pod_id)
    return pod is not None and secrets.compare_digest(bearer(request) or "", pod["_pod_key"])


def docker(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check)


def container_state(pod: dict) -> tuple[str, int | None]:
    r = docker("inspect", "-f", "{{.State.Status}} {{.State.ExitCode}}", pod["_container"])
    if r.returncode != 0:
        return "gone", None
    status, code = r.stdout.split()
    return status, int(code)


def pod_status(pod: dict) -> str:
    if pod["_deleted"]:
        return "TERMINATED"
    state, _ = container_state(pod)
    return {"created": "STARTING", "running": "RUNNING", "restarting": "STARTING",
            "paused": "RUNNING", "exited": "EXITED", "dead": "ERROR", "removing": "EXITED",
            "gone": "ERROR"}.get(state, "ERROR")


def public(pod: dict) -> dict:
    status = pod_status(pod)
    return {
        "id": pod["id"], "name": pod["name"], "status": status,
        "actions": {"RUNNING": ["stop", "restart", "terminate"], "EXITED": ["start", "terminate"]}
        .get(status, ["terminate"]),
        "image": pod["image"], "args": "", "entrypoint": [], "cmd": [],
        "disk": pod["disk"], "mounts": {}, "ports": pod["ports"], "env": dict(pod["env"]),
        "registry": None, "cloud": pod["cloud"], "dataCenterId": pod["dataCenterId"],
        "cudaVersion": pod["cudaVersion"], "ssh": {"proxy": None, "direct": None},
        "template": None, "cost": pod["cost"] if status == "RUNNING" else 0.0,
        "locked": pod["locked"], "runtime": None, "createdAt": pod["createdAt"],
        "startedAt": pod["startedAt"], "globalNetworking": {"enabled": False},
        "gpu": {"id": pod["gpu"]["id"], "count": 1, "vcpuCount": CPU_COUNT, "memory": 24},
    }


def _dc(dc_id: str) -> dict | None:
    return next((d for d in DATACENTERS if d["id"] == dc_id), None)


def _cuda_tuple(v: str) -> tuple:
    try:
        return tuple(int(x) for x in v.split("."))
    except ValueError:
        return (0,)


def _stock_for(cloud: str) -> list[dict]:
    want = cloud.upper()
    return [s for s in _stock if s.get("cloud", "BOTH") in ("BOTH", want)]


# ---------------------------------------------------------------- catalog
@app.get("/v2/catalog/gpus")
def catalog_gpus(request: Request, cloud: str = "SECURE", countryCodes: str = "",
                 minCudaVersion: str = "", include: str = "", product: str = "POD"):
    if bearer(request) is None:
        return problem(401, "missing API key")
    countries = {c.strip().upper() for c in countryCodes.split(",") if c.strip()}
    floor = _cuda_tuple(minCudaVersion) if minCudaVersion else None
    gpus = []
    with _lock:
        for s in _stock_for(cloud):
            cudas = [v for v in s.get("cudaVersions", ["12.8"]) if floor is None or _cuda_tuple(v) >= floor]
            if floor is not None and not cudas:
                continue
            dcs = []
            for dc_id in s.get("dataCenters", []):
                dc = _dc(dc_id)
                if dc is None or (countries and dc["country"] not in countries):
                    continue
                dcs.append({"id": dc_id, "name": dc["name"], "availability": s.get("availability", "HIGH")})
            gpus.append({
                "id": s["id"], "name": s["id"], "memory": s.get("memory", 24),
                "secure": s.get("cloud", "BOTH") in ("BOTH", "SECURE"),
                "community": s.get("cloud", "BOTH") in ("BOTH", "COMMUNITY"),
                "price": {"secure": s.get("price", 0.2), "community": s.get("price", 0.2)},
                "availability": s.get("availability", "HIGH") if dcs else "NONE",
                "dataCenters": dcs,
                "cudaVersions": [{"version": v, "available": True} for v in cudas],
            })
    return {"gpus": gpus}


@app.get("/v2/catalog/datacenters")
def catalog_datacenters(request: Request, regions: str = "", compliance: str = ""):
    if bearer(request) is None:
        return problem(401, "missing API key")
    regs = {r.strip().upper() for r in regions.split(",") if r.strip()}
    comps = {c.strip().upper() for c in compliance.split(",") if c.strip()}
    out = []
    for dc in DATACENTERS:
        if regs and dc["region"] not in regs:
            continue
        if comps and not comps <= set(dc["compliance"]):
            continue
        out.append({"id": dc["id"], "name": dc["name"], "region": dc["region"],
                    "globalNetwork": False, "networkVolumeTypes": [], "compliance": dc["compliance"]})
    return {"dataCenters": out}


# ---------------------------------------------------------------- pods
@app.post("/v2/pods")
async def create_pod(request: Request):
    if not is_admin(request):
        return problem(401 if bearer(request) is None else 403, "this key may not create pods")
    with _lock:
        if _create_errors:
            err = _create_errors.pop(0)
            hdrs = {"Retry-After": str(err["retry_after"])} if err.get("retry_after") else None
            return problem(int(err["status"]), err.get("detail", "injected error"), hdrs)
    try:
        body = await request.json()
    except ValueError:
        return problem(400, "body is not JSON")
    required = {"name", "image", "cloud", "gpu", "dataCenterIds", "env"}
    if not isinstance(body, dict) or not required <= set(body):
        return problem(400, f"missing fields: {sorted(required - set(body or {}))}")
    gpu_id = (body.get("gpu") or {}).get("id")
    cloud = str(body.get("cloud", "SECURE")).upper()
    wanted = list(body.get("dataCenterIds") or [])
    with _lock:
        stock = next((s for s in _stock_for(cloud) if s["id"] == gpu_id
                      and s.get("availability", "HIGH") != "NONE"), None)
        dc_id = next((d for d in wanted if stock and d in stock.get("dataCenters", [])), None)
    if stock is None or dc_id is None:
        return problem(400, "There are no longer any instances available with the requested specifications.")
    pod_id = "fk" + secrets.token_hex(6)
    pod_key = "podkey-" + secrets.token_hex(16)
    env = dict(body["env"])
    run_env = {**env, **ENV_OVERRIDES, "RUNPOD_POD_ID": pod_id, "RUNPOD_CPU_COUNT": str(CPU_COUNT),
               "RUNPOD_API_KEY": pod_key,
               "RUNPOD_API_BASE": os.environ.get("FAKE_RUNPOD_PUBLIC_URL", "http://host.docker.internal:8765")}
    container = f"fake-runpod-{pod_id}"
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".env") as f:
        for k, v in run_env.items():
            f.write(f"{k}={v}\n")
        env_file = f.name
    try:
        r = docker("run", "-d", "--name", container, "--label", CONTAINER_LABEL,
                   "--add-host", "host.docker.internal:host-gateway", "--env-file", env_file, POD_IMAGE)
    finally:
        os.unlink(env_file)
    if r.returncode != 0:
        return problem(500, "docker run failed: " + r.stderr.strip()[-300:])
    pod = {
        "id": pod_id, "name": body["name"], "image": body["image"], "cloud": cloud,
        "gpu": {"id": gpu_id}, "env": env, "disk": body.get("disk", 50), "ports": body.get("ports", []),
        "dataCenterId": dc_id, "cudaVersion": (stock.get("cudaVersions") or ["12.8"])[-1],
        "cost": float(stock.get("price", 0.2)), "locked": False, "createdAt": now_iso(),
        "startedAt": now_iso(), "_container": container, "_pod_key": pod_key, "_deleted": False,
        "_create_body_keys": sorted(body), "_gpu_keys": sorted(body.get("gpu") or {}),
    }
    with _lock:
        _pods[pod_id] = pod
    return JSONResponse(public(pod), status_code=201)


@app.get("/v2/pods")
def list_pods(request: Request, limit: int = 100, cursor: str | None = None):
    if not is_admin(request):
        return problem(401 if bearer(request) is None else 403, "this key may not list pods")
    live = [p for p in list(_pods.values()) if not p["_deleted"]]
    start = int(cursor) if cursor and cursor.isdigit() else 0
    page = live[start:start + max(1, min(limit, 1000))]
    nxt = start + len(page)
    more = nxt < len(live)
    return {"pods": [public(p) for p in page],
            "pagination": {"nextCursor": str(nxt) if more else None, "hasNextPage": more}}


def _get(pod_id: str):
    pod = _pods.get(pod_id)
    if pod is None or pod["_deleted"]:
        return None
    return pod


@app.get("/v2/pods/{pod_id}")
def get_pod(pod_id: str, request: Request):
    if not may_touch(request, pod_id):
        return problem(401 if bearer(request) is None else 403, "forbidden")
    pod = _get(pod_id)
    return public(pod) if pod else problem(404, "pod not found")


@app.delete("/v2/pods/{pod_id}")
def delete_pod(pod_id: str, request: Request):
    if not is_admin(request):
        return problem(401 if bearer(request) is None else 403, "forbidden")
    pod = _get(pod_id)
    if pod is None:
        return problem(404, "pod not found")
    if pod["locked"]:
        return problem(409, "pod is locked")
    docker("rm", "-f", pod["_container"])
    pod["_deleted"] = True
    return Response(status_code=204)


@app.patch("/v2/pods/{pod_id}")
async def patch_pod(pod_id: str, request: Request):
    if not may_touch(request, pod_id):
        return problem(401 if bearer(request) is None else 403, "forbidden")
    pod = _get(pod_id)
    if pod is None:
        return problem(404, "pod not found")
    body = await request.json()
    for key in ("name", "image", "env", "locked"):
        if key in body:
            pod[key] = dict(body[key]) if key == "env" else body[key]
    return public(pod)


@app.post("/v2/pods/{pod_id}/action")
async def pod_action(pod_id: str, request: Request):
    if not may_touch(request, pod_id):
        return problem(401 if bearer(request) is None else 403, "forbidden")
    pod = _get(pod_id)
    if pod is None:
        return problem(404, "pod not found")
    action = (await request.json()).get("action")
    if action in ("stop", "restart") and pod["locked"]:
        return problem(400, "pod is locked")
    if action == "stop":
        docker("stop", "-t", "10", pod["_container"])
    elif action == "start":
        docker("start", pod["_container"])
    elif action == "restart":
        docker("restart", "-t", "10", pod["_container"])
    elif action == "terminate":
        if pod["locked"]:
            return problem(409, "pod is locked")
        docker("rm", "-f", pod["_container"])
        pod["_deleted"] = True
    else:
        return problem(400, "unknown action")
    return public(pod)


@app.get("/v2/pods/{pod_id}/logs")
def pod_logs(pod_id: str, request: Request, source: str = "container"):
    if not may_touch(request, pod_id):
        return problem(401 if bearer(request) is None else 403, "forbidden")
    pod = _get(pod_id)
    if pod is None:
        return problem(404, "pod not found")
    r = docker("logs", "--timestamps", pod["_container"])
    events = []
    for raw in (r.stdout + r.stderr).splitlines():
        ts, _, line = raw.partition(" ")
        events.append(f"id: {ts}\ndata: {json.dumps({'source': 'container', 'line': line, 'ts': ts})}\n\n")
    return Response("".join(events), media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache"})


# ---------------------------------------------------------------- admin
def _admin_only(request: Request):
    return None if is_admin(request) else problem(401, "admin key required")


@app.post("/_fake/stock")
async def set_stock(request: Request):
    if (denied := _admin_only(request)) is not None:
        return denied
    body = await request.json()
    with _lock:
        _stock[:] = list(body.get("gpus", []))
    return {"gpus": len(_stock)}


@app.post("/_fake/next_create_error")
async def next_create_error(request: Request):
    if (denied := _admin_only(request)) is not None:
        return denied
    body = await request.json()
    with _lock:
        for _ in range(int(body.get("count", 1))):
            _create_errors.append({"status": int(body["status"]), "detail": body.get("detail", "injected"),
                                   "retry_after": body.get("retry_after")})
    return {"queued": len(_create_errors)}


@app.post("/_fake/pods/{pod_id}/kill")
def kill_pod(pod_id: str, request: Request):
    if (denied := _admin_only(request)) is not None:
        return denied
    pod = _get(pod_id)
    if pod is None:
        return problem(404, "pod not found")
    docker("kill", pod["_container"])
    return public(pod)


@app.post("/_fake/pods/{pod_id}/set")
async def set_pod(pod_id: str, request: Request):
    if (denied := _admin_only(request)) is not None:
        return denied
    pod = _get(pod_id)
    if pod is None:
        return problem(404, "pod not found")
    body = await request.json()
    for key in ("dataCenterId", "cudaVersion"):
        if key in body:
            pod[key] = body[key]
    return public(pod)


@app.get("/_fake/pods")
def fake_pods(request: Request):
    if (denied := _admin_only(request)) is not None:
        return denied
    return {"pods": [{**public(p), "env": sorted(p["env"]), "container": p["_container"],
                      "deleted": p["_deleted"], "create_body_keys": p["_create_body_keys"],
                      "gpu_keys": p["_gpu_keys"]} for p in _pods.values()]}


@app.post("/_fake/reset")
def reset(request: Request):
    if (denied := _admin_only(request)) is not None:
        return denied
    r = docker("ps", "-aq", "--filter", f"label={CONTAINER_LABEL}")
    ids = r.stdout.split()
    if ids:
        docker("rm", "-f", *ids)
    with _lock:
        _pods.clear()
        _create_errors.clear()
        _stock[:] = [dict(s) for s in DEFAULT_STOCK]
    return {"removed_containers": len(ids)}


def main() -> None:
    import uvicorn
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    uvicorn.run(app, host=a.host, port=a.port, log_level="info")


if __name__ == "__main__":
    main()
