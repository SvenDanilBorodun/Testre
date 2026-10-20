"""The E49/E51 pod-key check (runpod_training/e2e/pod_key_check.py), deps-free.

Runs PROBE_SOURCE exactly as pod A would (``python -I -c <bootstrap>`` with the
probe base64 in an env var) against a stdlib ``ThreadingHTTPServer`` stub serving
REST v2, REST v1 and GraphQL, with E49_SURFACES=v2,v1,graphql. Pinned: a confined
key, a leaky key, that the key and the canary value never appear in the output,
the safe pod bodies, the trainer base image, the exit-code table, that an
``unknown`` answer is inconclusive (never confined), that the SSE log reader does
not hang, and exit 3 on no key. CI's ``unittest discover -s tests`` runs it.
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


def make_server(mode):
    state = {"calls": []}
    pod_a = {"id": A, "name": "edubotics-e49-a", "cloud": "COMMUNITY", "env": [{"key": "X", "value": "1"}]}
    pod_b = {"id": B, "name": "edubotics-e49-b", "env": [{"key": "E49_CANARY", "value": "canary-value-xyz"}]}

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
            if self.headers.get("Authorization") != "Bearer " + SECRET:
                return self._send(401, {"detail": "no"})
            parts = path.strip("/").split("/")  # v2|v1, pods, [id], [action]
            is_list = len(parts) == 2 and parts[1] == "pods"
            pod_id = parts[2] if len(parts) >= 3 else None
            own = pod_id == A or (parts[-1] in ("action", "stop") and pod_id == A)
            if is_list and method == "GET":
                if mode == "leaky":
                    return self._send(200, {"pods": [pod_a, pod_b]})
                return self._send(200, {"pods": [pod_a]})  # confined / unknown: only A
            if pod_id == A:  # own pod: the positive control, always answered
                if method == "GET":
                    return self._send(200, pod_a)
                return self._send(200, {"id": A})
            # B-directed, or a create
            if (len(parts) == 2 and parts[1] == "pods" and method == "POST"):
                if mode == "leaky":
                    return self._send(201, {"id": "podC"})
                if mode == "unknown":
                    return self._send(400, {"detail": "There are no longer any instances available."})
                return self._send(403, {"detail": "forbidden"})
            if mode == "leaky":
                if method == "DELETE":
                    return self._send(204)
                return self._send(200, pod_b)
            if mode == "unknown":
                return self._send(429, {"detail": "slow down"})
            return self._send(403 if method != "GET" else 404, {"detail": "forbidden"})

        def _graphql(self):
            if ("api_key=" + SECRET) not in self.path:
                return self._send(200, {"errors": [{"message": "unauthorized"}]})
            body = self._read_body() or {}
            op, pod_id = _gql_op(body.get("query") or "")
            if op == "myself":
                if mode == "leaky":
                    return self._send(200, {"data": {"myself": {"pods": [pod_a, pod_b]}}})
                return self._send(200, {"data": {"myself": {"pods": [pod_a]}}})
            if op == "pod" and pod_id == A:
                return self._send(200, {"data": {"pod": pod_a}})
            if op == "podStop" and pod_id == A:
                return self._send(200, {"data": {"podStop": {"id": A}}})
            # B-directed or create
            if mode == "leaky":
                if op == "pod":
                    return self._send(200, {"data": {"pod": pod_b}})
                if op == "podStop":
                    return self._send(200, {"data": {"podStop": {"id": B}}})
                return self._send(200, {"data": {"podFindAndDeployOnDemand": {"id": "podC"}}})
            if mode == "unknown":
                return self._send(200, {"errors": [{"message": "rate limited"}]})
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


def run_probe(mode, surfaces="v2,v1,graphql", with_key=True):
    srv, state = make_server(mode)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    env = {"PATH": os.environ.get("PATH", ""),
           "RUNPOD_POD_ID": A, "E49_TARGET_POD_ID": B, "E49_SURFACES": surfaces,
           "E49_API_BASE": base, "E49_API_BASE_V1": base, "E49_GRAPHQL_URL": base + "/graphql",
           "E49_LINGER_S": "0", "E49_SLEEP_FOREVER": "0",
           "E49_CREATE_BODY_V2": json.dumps({"name": "edubotics-e49-c", "image": "x", "cloud": "SECURE"}),
           "E49_CREATE_BODY_V1": json.dumps({"name": "edubotics-e49-c", "image": "x", "cloud": "SECURE"}),
           "E49_CREATE_BODY_GQL": '{cloudType:SECURE,gpuCount:1,name:"edubotics-e49-c",imageName:"x"}',
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
        self.assertEqual(K.decide(lines_of(p), ("v2", "v1", "graphql")), 1)

    def test_an_unknown_answer_is_inconclusive_not_confined(self):
        p, _ = run_probe("unknown")
        self._no_secret(p)
        _, v = facts_of(p)
        self.assertEqual((v["any_allowed"], v["inconclusive"]), (False, True))
        self.assertEqual(K.decide(lines_of(p), ("v2", "v1", "graphql")), 2)

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


if __name__ == "__main__":
    unittest.main()
