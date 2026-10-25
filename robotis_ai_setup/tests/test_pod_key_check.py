"""The E49/E51 pod-key check (runpod_training/e2e/pod_key_check.py), deps-free.

Runs PROBE_SOURCE exactly as pod A would (``python -I -c <bootstrap>`` with the
probe base64 in an env var) against a stdlib ``ThreadingHTTPServer`` stub serving
REST v2, REST v1 and GraphQL, with E49_SURFACES=v2,v1,graphql. The stub enforces
RunPod's documented request and answer shapes per API (GraphQL ``env`` is
``[String]`` of ``KEY=value``, the v1 create body is v1's own), so a green run
means the probe's requests are the ones the real APIs accept. Pinned: a confined
key, a leaky key, that the key and the canary value never appear in the output,
the safe pod bodies, the trainer base image, the exit-code table, that each
``unknown`` source on its own is inconclusive (never confined), that the SSE log
reader does not hang, reads a chunk-split event and survives a refused
connection, that a tool failure exits 2, and exit 3 on no key. CI's
``unittest discover -s tests`` runs it.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
E2E = os.path.join(os.path.dirname(HERE), "runpod_training", "e2e")
sys.path.insert(0, E2E)
import pod_key_check as K  # noqa: E402

SECRET = "rpa_POD_A_SECRET_e49e49e49e49"
A, B = "podA", "podB"
_PODID = re.compile(r'podId:"([^"]+)"')


def _gql_op(query):
    if "myself" in query:
        return "myself", None
    if "podFindAndDeployOnDemand" in query:
        return "podFindAndDeployOnDemand", None
    if "podStop" in query:
        m = _PODID.search(query)
        return "podStop", (m.group(1) if m else None)
    if "pod(" in query:
        m = _PODID.search(query)
        return "pod", (m.group(1) if m else None)
    return "?", None


# RunPod's documented shapes, which the stub ENFORCES (verifier M1): a green run means
# the probe's requests are the ones the real APIs accept.
#  * GraphQL (docs.runpod.io/sdks/graphql/manage-pods): `env` is `[String]` of
#    "KEY=value" — a sub-selection `env{…}` is a validation error; the create
#    mutation podFindAndDeployOnDemand names a gpuTypeId.
#  * REST v1 (docs.runpod.io/api-reference/pods/POST/pods): POST /v1/pods takes
#    imageName, cloudType, computeType (CPU) + cpuFlavorIds, dockerEntrypoint.
#  * REST v2 (OpenAPI 2.0.0): POST /v2/pods takes name, image, cloud, exactly one of cpu/gpu.
V1_CREATE_REQUIRED = {"imageName", "cloudType", "computeType", "cpuFlavorIds", "dockerEntrypoint"}
V2_ONLY_KEYS = {"image", "cloud", "cpu", "gpu", "entrypoint"}
_ENV_SUBSELECTION = re.compile(r"\benv\s*\{")


def _valid_v2_create(body):
    return (isinstance(body, dict) and {"name", "image", "cloud"} <= set(body)
            and (("cpu" in body) != ("gpu" in body)))


def _valid_v1_create(body):
    return (isinstance(body, dict) and V1_CREATE_REQUIRED <= set(body) and not (V2_ONLY_KEYS & set(body))
            and body.get("computeType") == "CPU" and isinstance(body.get("cpuFlavorIds"), list)
            and isinstance(body.get("dockerEntrypoint"), list))


def make_server(mode, unknown=()):
    """mode: 'confined' | 'leaky'. ``unknown``: the sources that give a non-answer instead —
    'target' (B's GET/PATCH/stop/DELETE answer 429), 'list' (5xx), 'create' (RunPod's
    capacity 400), 'graphql' (B-directed GraphQL ops answer errors no deny word matches)."""
    unknown = set(unknown)
    state = {"calls": []}
    pod_a = {"id": A, "name": "edubotics-e49-a", "cloud": "COMMUNITY", "env": {"X": "1"}}
    pod_b = {"id": B, "name": "edubotics-e49-b", "env": {"E49_CANARY": "canary-value-xyz"}}

    def gql_pod(pod):  # GraphQL returns env as "KEY=value" strings
        return {"id": pod["id"], "name": pod["name"], "env": [f"{k}={v}" for k, v in pod["env"].items()]}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body=None):
            data = json.dumps(body).encode() if body is not None else b""
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _read_body(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            try:
                return json.loads(raw) if raw else None
            except ValueError:
                return None

        def _rest(self, method, path):
            body = self._read_body() if method in ("POST", "PATCH") else None
            if self.headers.get("Authorization") != "Bearer " + SECRET:
                return self._send(401, {"detail": "no"})
            parts = path.strip("/").split("/")  # v2|v1, pods, [id], [action|stop]
            version = parts[0]
            is_list = len(parts) == 2 and parts[1] == "pods"
            pod_id = parts[2] if len(parts) >= 3 else None
            if is_list and method == "GET":
                if "list" in unknown:
                    return self._send(503, {"detail": "unavailable"})
                pods = [pod_a, pod_b] if mode == "leaky" else [pod_a]
                return self._send(200, {"pods": pods} if version == "v2" else pods)
            if pod_id == A:  # own pod: the positive control, always answered
                return self._send(200, pod_a if method == "GET" else {"id": A})
            if is_list and method == "POST":  # a create
                valid = _valid_v2_create(body) if version == "v2" else _valid_v1_create(body)
                if not valid:
                    return self._send(400, {"error": "invalid input"})
                if "create" in unknown:
                    return self._send(400, {"detail": "There are no longer any instances available with the "
                                                      "requested specifications."})
                if mode == "leaky":
                    return self._send(201, {"id": "podC"})
                return self._send(403, {"detail": "forbidden"})
            # B-directed
            if "target" in unknown:
                return self._send(429, {"detail": "slow down"})
            if mode == "leaky":
                if method == "DELETE":
                    return self._send(204)
                return self._send(200, pod_b)
            return self._send(404 if method == "GET" else 403, {"detail": "forbidden"})

        def _graphql(self):
            body = self._read_body() or {}
            if ("api_key=" + SECRET) not in self.path:
                return self._send(200, {"errors": [{"message": "unauthorized"}]})
            query = body.get("query") or ""
            if _ENV_SUBSELECTION.search(query):
                return self._send(200, {"errors": [{"message": 'Field "env" must not have a selection since '
                                                              'type "[String]" has no subfields.'}]})
            op, pod_id = _gql_op(query)
            if op == "podFindAndDeployOnDemand" and "gpuTypeId" not in query:
                return self._send(200, {"errors": [{"message": 'Argument "input" has invalid value: In field '
                                                              '"gpuTypeId": Expected "String!", found null.'}]})
            if op == "pod" and pod_id == A:
                return self._send(200, {"data": {"pod": gql_pod(pod_a)}})
            if op == "podStop" and pod_id == A:
                return self._send(200, {"data": {"podStop": {"id": A}}})
            if op == "podFindAndDeployOnDemand":
                if "create" in unknown:
                    return self._send(200, {"errors": [{"message": "There are no longer any instances available "
                                                                  "with the requested specifications."}]})
                if mode == "leaky":
                    return self._send(200, {"data": {"podFindAndDeployOnDemand": {"id": "podC"}}})
                return self._send(200, {"errors": [{"message": "You do not have permission to deploy pods"}]})
            # the list and B-directed operations
            if "graphql" in unknown:
                return self._send(200, {"errors": [{"message": "rate limited"}]})
            if op == "myself":
                pods = [pod_a, pod_b] if mode == "leaky" else [pod_a]
                return self._send(200, {"data": {"myself": {"pods": [gql_pod(p) for p in pods]}}})
            if mode == "leaky":
                if op == "pod":
                    return self._send(200, {"data": {"pod": gql_pod(pod_b)}})
                return self._send(200, {"data": {"podStop": {"id": B}}})
            return self._send(200, {"errors": [{"message": "pod not found", "extensions": {"code": "NOT_FOUND"}}]})

        def do_GET(self):
            state["calls"].append(("GET", self.path.split("?")[0]))
            self._rest("GET", self.path.split("?")[0])

        def do_POST(self):
            p = self.path.split("?")[0]
            state["calls"].append(("POST", p))
            if p == "/graphql":
                return self._graphql()
            self._rest("POST", p)

        def do_PATCH(self):
            state["calls"].append(("PATCH", self.path.split("?")[0]))
            self._rest("PATCH", self.path.split("?")[0])

        def do_DELETE(self):
            state["calls"].append(("DELETE", self.path.split("?")[0]))
            self._rest("DELETE", self.path.split("?")[0])

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, state


def run_probe(mode, surfaces="v2,v1,graphql", with_key=True, unknown=()):
    """Run PROBE_SOURCE as pod A would, with the create bodies build_bodies() really emits."""
    srv, state = make_server(mode, unknown)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    a_env = K.build_bodies(a_gpu_id="NVIDIA RTX A5000", cpu_flavor="cpu3c", vcpu=2, canary="c")["a_template"]["env"]
    env = {"PATH": os.environ.get("PATH", ""),
           "RUNPOD_POD_ID": A, "E49_TARGET_POD_ID": B, "E49_SURFACES": surfaces,
           "E49_API_BASE": base, "E49_API_BASE_V1": base, "E49_GRAPHQL_URL": base + "/graphql",
           "E49_LINGER_S": "0", "E49_SLEEP_FOREVER": "0",
           "E49_CREATE_BODY_V2": a_env["E49_CREATE_BODY_V2"],
           "E49_CREATE_BODY_V1": a_env["E49_CREATE_BODY_V1"],
           "E49_CREATE_BODY_GQL": a_env["E49_CREATE_BODY_GQL"],
           "E49_PROBE_B64": base64.b64encode(K.PROBE_SOURCE.encode()).decode()}
    if with_key:
        env["RUNPOD_API_KEY"] = SECRET
    p = subprocess.run([sys.executable, "-I", "-c", K.BOOTSTRAP[-1]], env=env,
                       capture_output=True, text=True, timeout=90)
    srv.shutdown()
    return p, state


def lines_of(p):
    return [line for line in p.stdout.splitlines() if line.strip()]


def facts_of(p):
    checks = {}
    verdict = None
    for line in lines_of(p):
        obj = json.loads(line[4:])
        if obj.get("check") == "VERDICT":
            verdict = obj
        elif obj.get("check") == "check":
            checks[(obj["surface"], obj["name"])] = obj
    return checks, verdict


class ProbeRuns(unittest.TestCase):
    def _no_secret(self, p):
        self.assertNotIn(SECRET, p.stdout + p.stderr)
        self.assertNotIn("canary-value-xyz", p.stdout + p.stderr)
        for line in lines_of(p):
            self.assertTrue(line.startswith("E49 "), line)

    def test_a_confined_key(self):
        p, _ = run_probe("confined")
        self.assertEqual(p.returncode, 0, p.stderr)
        self._no_secret(p)
        checks, v = facts_of(p)
        self.assertIsNotNone(v)
        self.assertEqual((v["any_allowed"], v["inconclusive"], v["control_ok"]), (False, False, True))
        self.assertEqual(v["own_cloud"], "COMMUNITY")
        self.assertEqual(sorted(v["surfaces"]), ["graphql", "v1", "v2"])
        for surface in ("v2", "v1", "graphql"):
            self.assertEqual(checks[(surface, "own_get")]["verdict"], "allowed")
            for name in ("list", "target_get", "target_stop", "create"):
                self.assertEqual(checks[(surface, name)]["verdict"], "refused", (surface, name))
        self.assertEqual(K.decide(lines_of(p), ("v2", "v1", "graphql")), 0)

    def test_a_leaky_key(self):
        p, _ = run_probe("leaky")
        self._no_secret(p)
        checks, v = facts_of(p)
        self.assertTrue(v["any_allowed"])
        self.assertTrue(checks[("v2", "list")]["target_env_visible"])
        self.assertTrue(checks[("v2", "target_get")]["target_env_visible"])
        self.assertEqual(checks[("v2", "create")]["created_pod_id"], "podC")
        self.assertEqual(checks[("graphql", "target_stop")]["verdict"], "allowed")
        self.assertTrue(checks[("graphql", "list")]["target_env_visible"], "env as KEY=value strings")
        self.assertTrue(checks[("graphql", "target_get")]["target_env_visible"])
        self.assertTrue(checks[("v1", "list")]["target_env_visible"], "v1 lists a bare array")
        for surface in ("v2", "v1", "graphql"):
            self.assertEqual(checks[(surface, "create")]["verdict"], "allowed", surface)
            self.assertEqual(checks[(surface, "create")]["created_pod_id"], "podC", surface)
        self.assertEqual(K.decide(lines_of(p), ("v2", "v1", "graphql")), 1)

    # One non-answering source at a time (verifier M2): each must make ITS checks
    # `unknown` and the run inconclusive (exit 2) on its own, so no other source masks it.
    UNKNOWN_SOURCES = {
        "target": [(sf, n) for sf in ("v2", "v1")
                   for n in ("target_get", "target_patch", "target_stop", "target_delete")],
        "list": [("v2", "list"), ("v1", "list")],
        "create": [("v2", "create"), ("v1", "create"), ("graphql", "create")],
        "graphql": [("graphql", n) for n in ("list", "target_get", "target_stop")],
    }

    def _one_unknown_source(self, source):
        p, _ = run_probe("confined", unknown={source})
        self._no_secret(p)
        checks, v = facts_of(p)
        self.assertIsNotNone(v, p.stderr)
        expect = set(self.UNKNOWN_SOURCES[source])
        for key, row in checks.items():
            if key[1] == "own_get":
                self.assertEqual(row["verdict"], "allowed", key)
            elif key in expect:
                self.assertEqual(row["verdict"], "unknown", (source, key))
            else:
                self.assertEqual(row["verdict"], "refused", (source, key))
        self.assertTrue(expect <= set(checks), sorted(checks))
        self.assertEqual((v["any_allowed"], v["inconclusive"], v["control_ok"]), (False, True, True))
        self.assertEqual(K.decide(lines_of(p), ("v2", "v1", "graphql")), 2, source)

    def test_an_unknown_target_answer_is_inconclusive_not_confined(self):
        self._one_unknown_source("target")

    def test_an_unknown_list_answer_is_inconclusive_not_confined(self):
        self._one_unknown_source("list")

    def test_an_unknown_create_answer_is_inconclusive_not_confined(self):
        self._one_unknown_source("create")

    def test_an_unknown_graphql_answer_is_inconclusive_not_confined(self):
        self._one_unknown_source("graphql")

    def test_every_source_unknown_at_once_is_inconclusive(self):
        p, _ = run_probe("confined", unknown=set(self.UNKNOWN_SOURCES))
        self._no_secret(p)
        _, v = facts_of(p)
        self.assertEqual((v["any_allowed"], v["inconclusive"]), (False, True))
        self.assertEqual(K.decide(lines_of(p), ("v2", "v1", "graphql")), 2)

    def test_the_stub_refuses_the_wrong_shapes(self):
        # The stub is the fence of M1: it must reject RunPod's documented wrong shapes.
        self.assertFalse(_valid_v1_create({"name": "x", "image": "i", "cloud": "SECURE",
                                           "cpu": {"id": "cpu3c"}, "entrypoint": ["sleep"]}))
        self.assertTrue(_ENV_SUBSELECTION.search("query{pod(input:{podId:\"a\"}){id env{key value}}}"))
        self.assertFalse(_ENV_SUBSELECTION.search("query{pod(input:{podId:\"a\"}){id env}}"))

    def test_no_key_in_the_pod(self):
        p, _ = run_probe("confined", with_key=False)
        self._no_secret(p)
        setups = [json.loads(line[4:]) for line in lines_of(p) if '"setup"' in line]
        self.assertEqual(setups, [{"check": "setup", "has_key": False, "ok": False}])
        self.assertFalse(any('"VERDICT"' in line for line in lines_of(p)))
        self.assertEqual(K.decide(lines_of(p), ("v2", "v1", "graphql")), 3)

    def test_a_retired_surface_is_skipped_and_does_not_block_zero(self):
        p, _ = run_probe("confined", surfaces="v2,graphql")
        retired = [json.loads(line[4:]) for line in lines_of(p) if '"surface"' in line and '"retired"' in line]
        self.assertEqual(retired, [{"check": "surface", "surface": "v1", "state": "retired"}])
        self.assertEqual(K.decide(lines_of(p), ("v2", "graphql")), 0)
        self.assertEqual(K.decide(lines_of(p), ("v2", "v1", "graphql")), 0, "v1 retired does not block 0")


class Bodies(unittest.TestCase):
    def test_the_bodies_are_safe(self):
        canary = "canary-abc"
        bodies = K.build_bodies(a_gpu_id="NVIDIA RTX A5000", cpu_flavor="cpu3c", vcpu=2, canary=canary)
        a, b, c = bodies["a_template"], bodies["b"], bodies["c"]
        reaper = re.compile(r"edubotics-train-(\d+)-a(\d+)")
        for name in ("edubotics-e49-a", "edubotics-e49-b", "edubotics-e49-c"):
            self.assertIsNone(reaper.fullmatch(name))
        self.assertEqual((a["name"], b["name"], c["name"]), ("edubotics-e49-a", "edubotics-e49-b", "edubotics-e49-c"))
        self.assertEqual(a["cloud"], "COMMUNITY")
        self.assertEqual(a["dataCenterIds"], [])
        self.assertIs(a["startSsh"], False)
        self.assertIs(a["startJupyter"], False)
        self.assertEqual(a["ports"], [])
        self.assertEqual(a["entrypoint"][:3], ["python", "-I", "-c"])
        self.assertEqual(set(a["env"]), {"E49_TARGET_POD_ID", "E49_SURFACES", "E49_PROBE_B64",
                                         "E49_CREATE_BODY_V2", "E49_CREATE_BODY_V1", "E49_CREATE_BODY_GQL"})
        self.assertEqual(base64.b64decode(a["env"]["E49_PROBE_B64"]).decode(), K.PROBE_SOURCE)
        # Each surface's create body in its own documented shape (verifier M1).
        self.assertTrue(_valid_v2_create(c))
        self.assertEqual(json.loads(a["env"]["E49_CREATE_BODY_V2"]), c)
        c_v1 = json.loads(a["env"]["E49_CREATE_BODY_V1"])
        self.assertEqual(c_v1, bodies["c_v1"])
        self.assertTrue(_valid_v1_create(c_v1), c_v1)
        self.assertEqual(c_v1, {"name": "edubotics-e49-c", "imageName": K.BASE_IMAGE, "cloudType": "SECURE",
                                "computeType": "CPU", "cpuFlavorIds": ["cpu3c"], "vcpuCount": 2,
                                "dockerEntrypoint": ["sleep", "infinity"], "containerDiskInGb": 10,
                                "volumeInGb": 0, "ports": []})
        gql = a["env"]["E49_CREATE_BODY_GQL"]
        self.assertIn('gpuTypeId:"NVIDIA RTX A5000"', gql)
        self.assertIn("cloudType:COMMUNITY", gql)
        self.assertIn('imageName:"%s"' % K.BASE_IMAGE, gql)
        self.assertIn('name:"edubotics-e49-c"', gql)
        self.assertNotIn("env", gql)
        self.assertEqual(b["env"], {"E49_CANARY": canary})
        for body in (a, b, c):
            self.assertEqual(body["image"], K.BASE_IMAGE)
            self.assertRegex(body["image"], r"@sha256:[0-9a-f]{64}$")
        self.assertIs(b["startSsh"], False)
        self.assertEqual(c.get("env", None), None)
        self.assertEqual(K.build_bodies(a_gpu_id="g", cpu_flavor="f", vcpu=1, canary="c", a_cloud="SECURE")
                         ["a_template"]["cloud"], "SECURE")
        with self.assertRaises(ValueError):
            K.build_bodies(a_gpu_id="g", cpu_flavor="f", vcpu=1, canary="c", a_cloud="X")

    def test_the_base_image_is_the_trainers_base(self):
        dockerfile = os.path.join(os.path.dirname(HERE), "runpod_training", "Dockerfile")
        text = open(dockerfile, encoding="utf-8").read()
        digest = K.BASE_IMAGE.split("@", 1)[1]
        froms = [line for line in text.splitlines() if line.startswith("FROM")]
        self.assertTrue(any(digest in line for line in froms), froms)


class ExitTable(unittest.TestCase):
    def _verdict(self, **kw):
        base = {"check": "VERDICT", "surfaces": ["v2", "v1", "graphql"], "any_allowed": False,
                "inconclusive": False, "control_ok": True, "own_cloud": "COMMUNITY"}
        base.update(kw)
        return "E49 " + json.dumps(base, sort_keys=True)

    def test_the_exit_code_table(self):
        all3 = ("v2", "v1", "graphql")
        self.assertEqual(K.decide([self._verdict()], all3), 0)
        self.assertEqual(K.decide([self._verdict(any_allowed=True)], all3), 1)
        self.assertEqual(K.decide([self._verdict(inconclusive=True)], all3), 2)
        self.assertEqual(K.decide([self._verdict(control_ok=False)], all3), 2)
        self.assertEqual(K.decide([self._verdict(own_cloud="SECURE")], all3), 2)
        self.assertEqual(K.decide([], all3), 2, "no VERDICT line")
        self.assertEqual(K.decide(['E49 {"check": "setup", "has_key": false, "ok": false}'], all3), 3)
        # own_get refused but own_stop allowed: the probe's VERDICT carries control_ok True.
        self.assertEqual(K.decide([self._verdict(control_ok=True)], all3), 0)
        # a surface not passed is not required for a 0.
        self.assertEqual(K.decide([self._verdict(surfaces=["v2", "graphql"])], ("v2", "graphql")), 0)


class LogReader(unittest.TestCase):
    def test_the_log_reader_stops_without_hanging(self):
        # An SSE stream that keeps the socket open after the own_stop line: the reader
        # must return right after own_stop, not at timeout_s (review m4).
        e49_lines = [
            'E49 {"check": "check", "name": "own_get", "surface": "v2", "verdict": "allowed"}',
            'E49 {"any_allowed": false, "check": "VERDICT", "control_ok": true, "inconclusive": false, '
            '"own_cloud": "COMMUNITY", "surfaces": ["v2"]}',
            'E49 {"check": "own_stop", "stopped": true}',
        ]

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for i, line in enumerate(e49_lines):
                    self.wfile.write(("id: %d\n" % i).encode())
                    self.wfile.write(("data: " + json.dumps({"line": line}) + "\n\n").encode())
                    self.wfile.flush()
                # keep the socket open (an SSE stream never ends on its own)
                for _ in range(300):
                    time.sleep(0.1)
                    try:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                    except Exception:
                        return

        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        old = os.environ.get("E49_API_BASE")
        os.environ["E49_API_BASE"] = base
        try:
            t0 = time.monotonic()
            collected = K._read_log_lines("key", A, timeout_s=30)
            elapsed = time.monotonic() - t0
        finally:
            if old is None:
                os.environ.pop("E49_API_BASE", None)
            else:
                os.environ["E49_API_BASE"] = old
            srv.shutdown()
        self.assertLess(elapsed, 10.0, "the reader returned at own_stop, not at timeout_s")
        self.assertTrue(any('"own_stop"' in line for line in collected))

    def _with_base(self, base, fn):
        old = os.environ.get("E49_API_BASE")
        os.environ["E49_API_BASE"] = base
        try:
            return fn()
        finally:
            if old is None:
                os.environ.pop("E49_API_BASE", None)
            else:
                os.environ["E49_API_BASE"] = old

    def test_an_event_split_across_http_chunks_is_read(self):
        # Chunked transfer encoding cuts the VERDICT event in the middle (verifier m2a):
        # the reader must reassemble it, not read the raw socket.
        verdict = ('E49 {"any_allowed": false, "check": "VERDICT", "control_ok": true, "inconclusive": false, '
                   '"own_cloud": "COMMUNITY", "surfaces": ["v2"]}')
        stop = 'E49 {"check": "own_stop", "stopped": true}'
        body = ("id: 1\ndata: " + json.dumps({"line": verdict}) + "\n\n"
                "id: 2\ndata: " + json.dumps({"line": stop}) + "\n\n").encode()
        cut = body.index(b"VERDICT") + 3

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for piece in (body[:cut], body[cut:]):
                    self.wfile.write(b"%x\r\n" % len(piece) + piece + b"\r\n")
                    self.wfile.flush()
                    time.sleep(0.05)
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()

        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            collected = self._with_base(f"http://127.0.0.1:{srv.server_address[1]}",
                                        lambda: K._read_log_lines("key", A, timeout_s=10))
        finally:
            srv.shutdown()
        self.assertEqual(collected, [verdict, stop])
        self.assertEqual(K.decide(collected, ("v2",)), 0)

    def test_a_refused_connection_is_retried_not_raised(self):
        # Nothing listens (verifier m2b): the reader retries until its deadline and returns.
        import socket
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        t0 = time.monotonic()
        collected = self._with_base(f"http://127.0.0.1:{port}", lambda: K._read_log_lines("key", A, timeout_s=2))
        self.assertEqual(collected, [])
        self.assertLess(time.monotonic() - t0, 10.0)


class MainExit(unittest.TestCase):
    def test_an_unexpected_error_exits_2_never_1(self):
        # Exit 1 means „the key is not confined"; a crash of the tool must not say that (verifier m2b).
        real = K.run

        def boom(*a, **kw):
            raise ConnectionRefusedError("no route")

        K.run = boom
        try:
            code = K.main(["--run", "--key-file", "/nonexistent", "--a-gpu", "g", "--cpu-flavor", "f"])
        finally:
            K.run = real
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
