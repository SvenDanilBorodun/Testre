"""E49/E51 delta-test tool: is a pod-scoped RUNPOD_API_KEY confined to its own pod?

Two parts, one file:

* PROBE_SOURCE (a string): runs INSIDE pod A as its own start command
  (RunPod v2 ``entrypoint``, exec form: no SSH, no shell, no tool in the
  image). Reading only its injected key, it asks what that key can do to a
  SECOND pod B on each RunPod API surface still in service (REST v2, REST v1
  until 2026-11-15, GraphQL — one key works on all three). It prints ONLY
  ``E49 `` + a sorted JSON object (a surface tag, a check name, a
  classification, booleans); never the key, a header, a body or an env value.
  Each check is ``allowed`` / ``refused`` / ``unknown`` and no VERDICT claims
  „confined" from a non-answer (review M2).
* the launcher (``run``/``build_bodies``/``decide``): runs on the operator's
  machine with the ACCOUNT key (from a 0600 file, never printed or put into a
  pod body), creates B and A, reads A's pod log over ``GET /v2/pods/{A}/logs``
  (SSE), then always deletes every ``edubotics-e49-*`` pod. The exit code
  (``decide``) is the gate of DEPLOY.md 3b item 8: 0 = confined on every
  measured surface, 1 = reached/changed/created something, 2 = inconclusive
  (any ``unknown``), 3 = pod A got no key.

stdlib only, so it runs from any Python 3.12. Owner decisions E49, E51.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.request

# The trainer image's own base (runpod_training/Dockerfile): the key is injected
# whatever the image, and this public, digest-pinned base pulls in seconds.
BASE_IMAGE = "python:3.12.14-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9"
REST_V2_BASE = "https://api.runpod.io"
REST_V1_BASE = "https://rest.runpod.io"
GRAPHQL_URL = "https://api.runpod.io/graphql"
CANARY_NAME = "E49_CANARY"
ALL_SURFACES = ("v2", "v1", "graphql")

# The bootstrap pod A runs; it decodes PROBE_SOURCE from an env var and execs it,
# so the probe source is never on a command line.
BOOTSTRAP = ["python", "-I", "-c",
             "import base64,os;exec(compile(base64.b64decode(os.environ['E49_PROBE_B64']),'e49','exec'),"
             "{'__name__':'e49'})"]

# ---------------------------------------------------------------------------
# The in-pod probe (a string: pod A runs exactly this, stdlib only).
# ---------------------------------------------------------------------------
PROBE_SOURCE = r'''
import json
import os
import re
import time
import urllib.error
import urllib.request

KEY = os.environ.get("RUNPOD_API_KEY") or ""
OWN = os.environ.get("RUNPOD_POD_ID") or ""
TARGET = os.environ.get("E49_TARGET_POD_ID") or ""
SURFACES = [x for x in (os.environ.get("E49_SURFACES") or "").split(",") if x]
ALL_SURFACES = ("v2", "v1", "graphql")
CANARY_NAME = "E49_CANARY"
V2_BASE = (os.environ.get("E49_API_BASE") or "https://api.runpod.io").rstrip("/")
V1_BASE = (os.environ.get("E49_API_BASE_V1") or "https://rest.runpod.io").rstrip("/")
GQL_URL = os.environ.get("E49_GRAPHQL_URL") or "https://api.runpod.io/graphql"


def out(check, **facts):
    print("E49 " + json.dumps(dict(check=check, **facts), sort_keys=True), flush=True)


def _rest(method, url, headers, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read(2_000_000)
    except urllib.error.HTTPError as e:
        try:
            raw = e.read(2_000_000)
        except Exception:
            raw = b""
        return e.code, raw
    except Exception as e:
        return type(e).__name__, None


def rest_call(method, base, path, body=None):
    status, raw = _rest(method, base + path,
                        {"Authorization": "Bearer " + KEY, "Content-Type": "application/json"}, body)
    parsed = None
    if raw:
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
    return status, parsed


def gql_call(query):
    status, raw = _rest("POST", GQL_URL + "?api_key=" + KEY, {"Content-Type": "application/json"}, {"query": query})
    parsed = None
    if raw:
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
    return status, parsed


def ok2(status):
    return isinstance(status, int) and 200 <= status < 300


def env_has_canary(pod):
    env = pod.get("env") if isinstance(pod, dict) else None
    if isinstance(env, dict):
        return CANARY_NAME in env
    if isinstance(env, list):
        return any(isinstance(i, dict) and i.get("key") == CANARY_NAME for i in env)
    return False


# ----- REST classification -------------------------------------------------
def classify_rest(check, status, parsed):
    if ok2(status):
        if check == "list":
            pods = (parsed or {}).get("pods") if isinstance(parsed, dict) else parsed
            pods = pods if isinstance(pods, list) else []
            others = [p for p in pods if isinstance(p, dict) and p.get("id") != OWN]
            return "refused" if not others else "allowed"
        return "allowed"
    if check == "create":
        return "refused" if status in (401, 403) else "unknown"
    if check == "list":
        return "refused" if status in (401, 403) else "unknown"
    return "refused" if status in (401, 403, 404) else "unknown"


_GQL_DENY = re.compile(r"unauthor|forbidden|permission|not found|does not exist", re.I)


def classify_gql(status, parsed, op):
    if status != 200 or not isinstance(parsed, dict):
        if status in (401, 403):
            return "refused"
        return "unknown"
    errors = parsed.get("errors")
    if isinstance(errors, list) and errors:
        for e in errors:
            if not isinstance(e, dict):
                continue
            msg = str(e.get("message") or "")
            code = ""
            ext = e.get("extensions")
            if isinstance(ext, dict):
                code = str(ext.get("code") or "")
            if _GQL_DENY.search(msg) or _GQL_DENY.search(code):
                return "refused"
        return "unknown"
    data = parsed.get("data")
    if not isinstance(data, dict):
        return "unknown"
    node = data.get(op)
    if op == "myself":
        pods = (node or {}).get("pods") if isinstance(node, dict) else None
        pods = pods if isinstance(pods, list) else []
        others = [p for p in pods if isinstance(p, dict) and p.get("id") != OWN]
        return "allowed" if others else "refused"
    return "allowed" if node else "unknown"


def gql_env_has_canary(parsed):
    data = (parsed or {}).get("data") if isinstance(parsed, dict) else None
    me = (data or {}).get("myself") if isinstance(data, dict) else None
    pods = (me or {}).get("pods") if isinstance(me, dict) else None
    for p in pods if isinstance(pods, list) else []:
        if isinstance(p, dict) and p.get("id") == TARGET:
            return env_has_canary(p)
    return False


# ----- per-surface runs ----------------------------------------------------
def run_rest(tag, base, create_body):
    results = {}
    s, got = rest_call("GET", base, "/v2/pods/" + OWN) if tag == "v2" else rest_call("GET", base, "/v1/pods/" + OWN)
    results["own_get"] = classify_rest("own_get", s, got)
    out("check", surface=tag, name="own_get", verdict=results["own_get"])
    pods_path = "/v2/pods?limit=1000" if tag == "v2" else "/v1/pods"
    s, listed = rest_call("GET", base, pods_path)
    verdict = classify_rest("list", s, listed)
    pods = (listed or {}).get("pods") if isinstance(listed, dict) else listed
    pods = pods if isinstance(pods, list) else []
    others = [p for p in pods if isinstance(p, dict) and p.get("id") != OWN]
    results["list"] = verdict
    out("check", surface=tag, name="list", verdict=verdict,
        sees_target=any(p.get("id") == TARGET for p in others),
        target_env_visible=any(env_has_canary(p) for p in others if p.get("id") == TARGET))
    tp = "/v2/pods/" + TARGET if tag == "v2" else "/v1/pods/" + TARGET
    s, got = rest_call("GET", base, tp)
    results["target_get"] = classify_rest("target_get", s, got)
    out("check", surface=tag, name="target_get", verdict=results["target_get"],
        target_env_visible=ok2(s) and env_has_canary(got))
    s, _ = rest_call("PATCH", base, tp, {"name": "edubotics-e49-b-x"})
    results["target_patch"] = classify_rest("target_patch", s, None)
    out("check", surface=tag, name="target_patch", verdict=results["target_patch"])
    ap = "/v2/pods/" + TARGET + "/action" if tag == "v2" else "/v1/pods/" + TARGET + "/stop"
    s, _ = rest_call("POST", base, ap, {"action": "stop"} if tag == "v2" else {})
    results["target_stop"] = classify_rest("target_stop", s, None)
    out("check", surface=tag, name="target_stop", verdict=results["target_stop"])
    created = None
    if create_body:
        s, made = rest_call("POST", base, "/v2/pods" if tag == "v2" else "/v1/pods", json.loads(create_body))
        results["create"] = classify_rest("create", s, made)
        if ok2(s) and isinstance(made, dict):
            created = made.get("id")
    else:
        results["create"] = "unknown"
    out("check", surface=tag, name="create", verdict=results["create"], created_pod_id=created)
    s, _ = rest_call("DELETE", base, tp)
    results["target_delete"] = classify_rest("target_delete", s, None)
    out("check", surface=tag, name="target_delete", verdict=results["target_delete"])
    return results


def run_gql(create_body):
    results = {}
    s, got = gql_call('query{pod(input:{podId:"%s"}){id env{key value}}}' % OWN)
    results["own_get"] = classify_gql(s, got, "pod")
    out("check", surface="graphql", name="own_get", verdict=results["own_get"])
    s, listed = gql_call("query{myself{pods{id name env{key value}}}}")
    results["list"] = classify_gql(s, listed, "myself")
    out("check", surface="graphql", name="list", verdict=results["list"],
        target_env_visible=gql_env_has_canary(listed))
    s, got = gql_call('query{pod(input:{podId:"%s"}){id env{key value}}}' % TARGET)
    results["target_get"] = classify_gql(s, got, "pod")
    out("check", surface="graphql", name="target_get", verdict=results["target_get"])
    s, _ = gql_call('mutation{podStop(input:{podId:"%s"}){id}}' % TARGET)
    results["target_stop"] = classify_gql(s, _, "podStop")
    out("check", surface="graphql", name="target_stop", verdict=results["target_stop"])
    created = None
    if create_body:
        s, made = gql_call("mutation{podFindAndDeployOnDemand(input:%s){id}}" % create_body)
        results["create"] = classify_gql(s, made, "podFindAndDeployOnDemand")
        data = (made or {}).get("data") if isinstance(made, dict) else None
        node = (data or {}).get("podFindAndDeployOnDemand") if isinstance(data, dict) else None
        if isinstance(node, dict):
            created = node.get("id")
    else:
        results["create"] = "unknown"
    out("check", surface="graphql", name="create", verdict=results["create"], created_pod_id=created)
    return results


def self_stop(tag):
    if tag == "v2":
        return ok2(rest_call("POST", V2_BASE, "/v2/pods/" + OWN + "/action", {"action": "stop"})[0])
    if tag == "v1":
        return ok2(rest_call("POST", V1_BASE, "/v1/pods/" + OWN + "/stop", {})[0])
    s, got = gql_call('mutation{podStop(input:{podId:"%s"}){id}}' % OWN)
    return classify_gql(s, got, "podStop") == "allowed"


def own_cloud():
    s, got = rest_call("GET", V2_BASE, "/v2/pods/" + OWN)
    if ok2(s) and isinstance(got, dict) and isinstance(got.get("cloud"), str):
        return got["cloud"].upper()
    return None


def main():
    if not KEY:
        out("setup", ok=False, has_key=False)
        return
    want = SURFACES or ["v2"]
    measured = {}
    control_ok = False
    for tag in ALL_SURFACES:
        if tag not in want:
            out("surface", surface=tag, state="retired")
            continue
        if tag == "v2":
            r = run_rest("v2", V2_BASE, os.environ.get("E49_CREATE_BODY_V2"))
        elif tag == "v1":
            r = run_rest("v1", V1_BASE, os.environ.get("E49_CREATE_BODY_V1"))
        else:
            r = run_gql(os.environ.get("E49_CREATE_BODY_GQL"))
        measured[tag] = r
    # own_get is the positive control (does the key act on A at all); it is NOT a
    # reach into B, so it never makes any_allowed / inconclusive true.
    control_names = {"own_get"}
    any_allowed = any(v == "allowed" for r in measured.values()
                      for name, v in r.items() if name not in control_names)
    inconclusive = any(v == "unknown" for r in measured.values()
                       for name, v in r.items() if name not in control_names)
    control_ok = any(r.get("own_get") == "allowed" for r in measured.values())
    linger = float(os.environ.get("E49_LINGER_S") or 120)
    deadline = time.time() + linger
    # own_stop contributes to control_ok too; run it after the linger so the launcher
    # has read the VERDICT by then.
    cloud = own_cloud()
    out("VERDICT", surfaces=sorted(measured), any_allowed=any_allowed, inconclusive=inconclusive,
        control_ok=control_ok, own_cloud=cloud)
    while time.time() < deadline:
        time.sleep(1)
    stopped_any = False
    for tag in sorted(measured):
        if self_stop(tag):
            stopped_any = True
    out("own_stop", stopped=stopped_any)


if __name__ == "e49" or __name__ == "__main__":
    main()
    while os.environ.get("E49_SLEEP_FOREVER", "1") == "1":
        time.sleep(3600)
'''


# ---------------------------------------------------------------------------
# The launcher (operator machine; the only part that writes to RunPod).
# ---------------------------------------------------------------------------
def build_bodies(*, a_gpu_id: str, cpu_flavor: str, vcpu: int, canary: str, a_cloud: str = "COMMUNITY") -> dict:
    """The create bodies for pods A, B and C (pure). ``a_cloud`` in COMMUNITY/SECURE."""
    if a_cloud not in ("COMMUNITY", "SECURE"):
        raise ValueError("a_cloud must be COMMUNITY or SECURE")
    b = {"name": "edubotics-e49-b", "image": BASE_IMAGE, "cloud": "SECURE",
         "cpu": {"id": cpu_flavor, "vcpuCount": int(vcpu)}, "entrypoint": ["sleep", "infinity"],
         "env": {canary_key(): canary}, "ports": [], "startSsh": False, "startJupyter": False, "disk": 10}
    c = {"name": "edubotics-e49-c", "image": BASE_IMAGE, "cloud": "SECURE",
         "cpu": {"id": cpu_flavor, "vcpuCount": int(vcpu)}, "entrypoint": ["sleep", "infinity"],
         "ports": [], "startSsh": False, "startJupyter": False, "disk": 10}
    gql_create = json.dumps({"cloudType": "SECURE", "gpuCount": 1, "name": "edubotics-e49-c", "imageName": BASE_IMAGE})
    # GraphQL's input object is unquoted keys; the probe splices it verbatim.
    gql_input = "{cloudType:SECURE,gpuCount:1,name:\"edubotics-e49-c\",imageName:\"%s\"}" % BASE_IMAGE
    a = {"name": "edubotics-e49-a", "image": BASE_IMAGE, "cloud": a_cloud,
         "gpu": {"id": a_gpu_id, "count": 1, "minCudaVersion": "12.6"}, "dataCenterIds": [],
         "entrypoint": list(BOOTSTRAP), "ports": [], "startSsh": False, "startJupyter": False, "disk": 10,
         "env": {"E49_TARGET_POD_ID": "", "E49_SURFACES": "",
                 "E49_PROBE_B64": base64.b64encode(PROBE_SOURCE.encode()).decode(),
                 "E49_CREATE_BODY_V2": json.dumps(c), "E49_CREATE_BODY_V1": json.dumps(c),
                 "E49_CREATE_BODY_GQL": gql_input}}
    return {"a_template": a, "b": b, "c": c, "gql_create": gql_create}


def canary_key() -> str:
    return CANARY_NAME


def _account_call(method, url, key, body=None, *, timeout=20):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Authorization": "Bearer " + key, "Content-Type": "application/json",
                                          "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(5_000_000)
    except urllib.error.HTTPError as e:
        try:
            raw = e.read(5_000_000)
        except Exception:
            raw = b""
        return e.code, raw
    except Exception as e:
        return type(e).__name__, None


def _ok(status):
    return isinstance(status, int) and 200 <= status < 300


def decide(lines, surfaces_passed) -> int:
    """Pure: the exit code from the probe's E49 lines (review M2/m5, E51)."""
    parsed = []
    for line in lines:
        line = line.strip()
        if not line.startswith("E49 "):
            continue
        try:
            parsed.append(json.loads(line[4:]))
        except ValueError:
            continue
    setups = [p for p in parsed if p.get("check") == "setup"]
    if setups and all(p.get("has_key") is False for p in parsed if p.get("check") == "setup") \
            and not any(p.get("check") == "VERDICT" for p in parsed):
        return 3
    verdicts = [p for p in parsed if p.get("check") == "VERDICT"]
    if not verdicts:
        return 2
    v = verdicts[-1]
    if v.get("any_allowed"):
        return 1
    if v.get("inconclusive"):
        return 2
    retired = {p.get("surface") for p in parsed if p.get("check") == "surface" and p.get("state") == "retired"}
    need = [s for s in surfaces_passed if s not in retired]
    measured = set(v.get("surfaces") or [])
    if v.get("control_ok") and v.get("own_cloud") == "COMMUNITY" and all(s in measured for s in need):
        return 0
    return 2


def _runpod_base(name):
    env = os.environ
    if name == "v2":
        return (env.get("E49_API_BASE") or REST_V2_BASE).rstrip("/")
    return (env.get("E49_API_BASE_V1") or REST_V1_BASE).rstrip("/")


def _create(key, body):
    status, raw = _account_call("POST", _runpod_base("v2") + "/v2/pods", key, body)
    pod = json.loads(raw) if _ok(status) and raw else None
    return status, (pod.get("id") if isinstance(pod, dict) else None)


def _get_status(key, pod_id):
    status, raw = _account_call("GET", _runpod_base("v2") + "/v2/pods/" + pod_id, key)
    if _ok(status) and raw:
        try:
            return json.loads(raw).get("desiredStatus") or json.loads(raw).get("status")
        except ValueError:
            return None
    return None


def _wait_running(key, pod_id, seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if _get_status(key, pod_id) == "RUNNING":
            return True
        time.sleep(5)
    return False


def _list_e49_pods(key):
    status, raw = _account_call("GET", _runpod_base("v2") + "/v2/pods?limit=1000", key)
    if not (_ok(status) and raw):
        return []
    try:
        pods = json.loads(raw).get("pods") or []
    except ValueError:
        return []
    return [p for p in pods if isinstance(p, dict) and isinstance(p.get("name"), str)
            and p["name"].startswith("edubotics-e49-")]


def _delete(key, pod_id):
    _account_call("DELETE", _runpod_base("v2") + "/v2/pods/" + pod_id, key)


def _read_log_lines(key, pod_id, timeout_s):
    """Read pod A's container log (SSE). Collect every `E49 ` line, dedupe by the
    SSE event id, stop at the first own_stop line or at timeout_s (review m4)."""
    import http.client
    import urllib.parse

    url = _runpod_base("v2") + "/v2/pods/" + pod_id + "/logs?source=container&tail=5000"
    collected = []
    seen_ids = set()
    deadline = time.time() + timeout_s
    last_event_id = None
    while time.time() < deadline:
        parsed = urllib.parse.urlsplit(url)
        headers = {"Authorization": "Bearer " + key, "Accept": "text/event-stream"}
        if last_event_id:
            headers["Last-Event-ID"] = last_event_id
        conn = (http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection)(
            parsed.netloc, timeout=5)
        try:
            conn.request("GET", parsed.path + ("?" + parsed.query if parsed.query else ""), headers=headers)
            resp = conn.getresponse()
            fp = resp.fp
            event_id = None
            while time.time() < deadline:
                try:
                    raw = fp.readline()
                except Exception:
                    break
                if not raw:
                    break
                try:
                    text = raw.decode("utf-8", "replace").rstrip("\n")
                except Exception:
                    continue
                if text.startswith("id:"):
                    event_id = text[3:].strip()
                    continue
                if text.startswith("data:"):
                    payload = text[5:].strip()
                    if event_id:
                        if event_id in seen_ids:
                            continue
                        seen_ids.add(event_id)
                        last_event_id = event_id
                    line = payload
                    try:
                        obj = json.loads(payload)
                        if isinstance(obj, dict) and isinstance(obj.get("line"), str):
                            line = obj["line"]
                    except ValueError:
                        pass
                    for part in line.splitlines():
                        part = part.strip()
                        if part.startswith("E49 "):
                            collected.append(part)
                            if '"check": "own_stop"' in part or '"check":"own_stop"' in part:
                                return collected
        finally:
            conn.close()
        time.sleep(1)
    return collected


def run(key_file, a_gpu_id, cpu_flavor, vcpu, a_cloud="COMMUNITY",
        surfaces=ALL_SURFACES, timeout_s=1200) -> int:
    key = _read_key(key_file)
    canary = "canary-" + secrets.token_hex(8)
    bodies = build_bodies(a_gpu_id=a_gpu_id, cpu_flavor=cpu_flavor, vcpu=vcpu, canary=canary, a_cloud=a_cloud)
    lines = []
    a_id = b_id = None
    try:
        s, b_id = _create(key, bodies["b"])
        if not b_id:
            print("E49 launcher: could not create pod B (status %s)" % s, file=sys.stderr)
            return 2
        if not _wait_running(key, b_id, 300):
            print("E49 launcher: pod B did not reach RUNNING", file=sys.stderr)
            return 2
        a = dict(bodies["a_template"])
        a["env"] = dict(a["env"], E49_TARGET_POD_ID=b_id, E49_SURFACES=",".join(surfaces))
        s, a_id = _create(key, a)
        if not a_id:
            print("E49 launcher: could not create pod A (status %s)" % s, file=sys.stderr)
            return 2
        lines = _read_log_lines(key, a_id, timeout_s)
    finally:
        created_ids = []
        for line in lines:
            try:
                obj = json.loads(line[4:])
            except ValueError:
                continue
            if obj.get("check") == "check" and obj.get("name") == "create" and obj.get("created_pod_id"):
                created_ids.append(obj["created_pod_id"])
        for pod_id in filter(None, [a_id, b_id, *created_ids]):
            _delete(key, pod_id)
        for p in _list_e49_pods(key):
            _delete(key, p["id"])
        leftover = _list_e49_pods(key)
        if leftover:
            print("E49 launcher WARNING: pods left behind: %s" % [p.get("id") for p in leftover], file=sys.stderr)
    for line in lines:
        print(line)
    return decide(lines, surfaces)


def _read_key(key_file) -> str:
    with open(key_file, encoding="utf-8") as f:
        return f.read().strip()


def _dry_run() -> int:
    bodies = build_bodies(a_gpu_id="NVIDIA RTX A5000", cpu_flavor="cpu3c", vcpu=2, canary="canary-xxxx")
    a = bodies["a_template"]
    shown = dict(a)
    shown["env"] = {k: ("<%d bytes>" % len(v) if k.startswith("E49_PROBE_B64") or k.startswith("E49_CREATE_BODY")
                        else v) for k, v in a["env"].items()}
    print(json.dumps(shown, indent=2, sort_keys=True))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="E49/E51 pod-key confinement check")
    parser.add_argument("--dry-run", action="store_true", help="print pod A's body (no network)")
    parser.add_argument("--run", action="store_true", help="create the pods and check (writes to RunPod)")
    parser.add_argument("--key-file", help="a 0600 file holding the ACCOUNT RunPod API key")
    parser.add_argument("--a-gpu", help="the GPU id for pod A (the cheapest Community card with stock)")
    parser.add_argument("--cpu-flavor", help="a CPU flavor id for pods B and C (from GET /v2/catalog/cpus)")
    parser.add_argument("--vcpu", type=int, default=2)
    parser.add_argument("--a-cloud", default="COMMUNITY", choices=["COMMUNITY", "SECURE"])
    parser.add_argument("--surfaces", default=",".join(ALL_SURFACES),
                        help="CSV of v2,v1,graphql (drop v1 after 2026-11-15)")
    args = parser.parse_args(argv)
    if args.dry_run:
        return _dry_run()
    if args.run:
        if not (args.key_file and args.a_gpu and args.cpu_flavor):
            parser.error("--run needs --key-file, --a-gpu and --cpu-flavor")
        surfaces = tuple(x.strip() for x in args.surfaces.split(",") if x.strip())
        return run(args.key_file, args.a_gpu, args.cpu_flavor, args.vcpu, a_cloud=args.a_cloud, surfaces=surfaces)
    parser.error("pass --dry-run or --run")
    return 2


if __name__ == "__main__":
    sys.exit(main())
