"""r2_client against REAL botocore (spec §3.4, FR-11: CI installs boto3/botocore 1.43.107).

A presigned PUT must sign Content-Type, Content-Length and Content-MD5, so a
body of another length (or, F13 permitting, another MD5) is refused by R2's
signature check; the key layout and the expiries are the spec's; head/get
errors are classified (404 → R2Missing, anything else propagates). Offline:
botocore signs locally; its Stubber answers the HEAD/GET/DELETE calls.

Without boto3 the module skips locally and FAILS under GitHub Actions (the same
rule as the executed installer tests: a lost dependency must not go green).
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import os
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from app.services import r2_client

_HAVE_BOTO = importlib.util.find_spec("boto3") is not None and importlib.util.find_spec("botocore") is not None
ENV = {"R2_ENDPOINT_URL": "https://0123456789abcdef.eu.r2.cloudflarestorage.com", "R2_ACCESS_KEY_ID": "test-access-key",
       "R2_SECRET_ACCESS_KEY": "test-secret", "R2_BUCKET": "edubotics-train"}

if not _HAVE_BOTO and os.environ.get("GITHUB_ACTIONS") == "true":
    raise ImportError("boto3/botocore 1.43.107 must be installed in CI (ci.yml python-tests, FR-11)")


class KeyLayout(unittest.TestCase):
    def test_keys(self):
        with patch.dict(os.environ, {"R2_KEY_PREFIX": ""}, clear=False):
            os.environ.pop("R2_KEY_PREFIX")
            self.assertEqual(r2_client.ckpt_key(7, 2, 2500, "pretrained_model/config.json"),
                             "train/t7/a2/s0002500/pretrained_model/config.json")
            self.assertEqual(r2_client.log_key(7, 2), "train/t7/a2/log/training_log.txt")
            self.assertEqual(r2_client.training_prefix(7), "train/t7/")
        with patch.dict(os.environ, {"R2_KEY_PREFIX": "e2e/"}):
            self.assertEqual(r2_client.log_key(7, 1), "e2e/t7/a1/log/training_log.txt")

    def test_keep_key(self):
        with patch.dict(os.environ, {"R2_KEEP_PREFIX": ""}, clear=False):
            os.environ.pop("R2_KEEP_PREFIX")
            self.assertEqual(r2_client.keep_key(7, 42500, "pretrained_model/config.json"),
                             "keep/t7/s0042500/pretrained_model/config.json")
        with patch.dict(os.environ, {"R2_KEEP_PREFIX": "kept/"}):
            self.assertEqual(r2_client.keep_key(7, 2500, "training_state/x"), "kept/t7/s0002500/training_state/x")

    def test_ttls(self):
        self.assertEqual((r2_client.PUT_TTL_S, r2_client.GET_TTL_S), (900, 3600))

    def test_not_configured(self):
        r2_client._reset_for_tests()
        with patch.dict(os.environ, {k: "" for k in ENV}):
            self.assertFalse(r2_client.configured())
            with self.assertRaises(r2_client.R2NotConfigured) as ctx:
                r2_client._client()
            self.assertNotIn("test-access-key", str(ctx.exception))
        r2_client._reset_for_tests()

    def test_boto3_is_imported_lazily(self):
        import ast

        with open(r2_client.__file__, encoding="utf-8") as fh:
            src = fh.read()
        top = [n for n in ast.parse(src).body if isinstance(n, (ast.Import, ast.ImportFrom))]
        self.assertFalse([n for n in top if "boto" in ast.dump(n)])


@unittest.skipUnless(_HAVE_BOTO, "boto3/botocore not installed (CI installs them)")
class RealBotocore(unittest.TestCase):
    def setUp(self):
        r2_client._reset_for_tests()
        self._env = patch.dict(os.environ, ENV)
        self._env.__enter__()

    def tearDown(self):
        self._env.__exit__(None, None, None)
        r2_client._reset_for_tests()

    def test_the_pinned_versions(self):
        import boto3
        import botocore

        self.assertEqual((boto3.__version__, botocore.__version__), ("1.43.107", "1.43.107"))

    def test_the_client_settings(self):
        c = r2_client._client()
        cfg = c.meta.config
        self.assertEqual(c.meta.region_name, "auto")
        self.assertEqual(cfg.signature_version, "s3v4")
        self.assertEqual(cfg.s3, {"addressing_style": "path"})
        self.assertEqual((cfg.connect_timeout, cfg.read_timeout), (10, 60))
        # botocore stores the spec's {"max_attempts": 3} (retries) as 4 total attempts
        self.assertEqual(cfg.retries, {"total_max_attempts": 4, "mode": "standard"})
        self.assertIs(r2_client._client(), c, "one client per process")

    def test_without_a_sha256_the_presigned_put_is_unchanged(self):
        # gate G0 failed (R2: 400 InvalidRequest to any PUT carrying the checksum header),
        # so every production caller still presigns exactly today's three headers
        data = b"x" * 1665
        md5 = base64.b64encode(hashlib.md5(data).digest()).decode()
        url, headers = r2_client.presign_put("train/t1/a1/s0002500/pretrained_model/config.json", len(data), md5)
        q = parse_qs(urlparse(url).query)
        self.assertEqual(q["X-Amz-SignedHeaders"], ["content-length;content-md5;content-type;host"])
        self.assertEqual(headers, {"Content-Type": "application/octet-stream", "Content-Length": "1665",
                                   "Content-MD5": md5})

    def test_no_production_caller_passes_a_sha256(self):
        import re

        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for dirpath, _, files in os.walk(here):
            if os.sep + "tests" in dirpath:
                continue
            for name in files:
                if name.endswith(".py"):
                    with open(os.path.join(dirpath, name), encoding="utf-8") as fh:
                        src = fh.read()
                    for call in re.findall(r"presign_put\(([^)]*)\)", src):
                        if name == "r2_client.py" and "sha256_hex" in call:
                            continue
                        self.assertLess(call.count(","), 3, f"{name}: presign_put({call}) signs a checksum")

    def test_a_presigned_put_signs_length_md5_type_and_sha256(self):
        data = b"x" * 1665
        md5 = base64.b64encode(hashlib.md5(data).digest()).decode()
        sha_hex = hashlib.sha256(data).hexdigest()
        sha_b64 = base64.b64encode(hashlib.sha256(data).digest()).decode()
        url, headers = r2_client.presign_put("train/t1/a1/s0002500/pretrained_model/config.json", len(data), md5,
                                             sha_hex)
        q = parse_qs(urlparse(url).query)
        # 052 (OQ-1): the checksum is a SIGNED HEADER, never a query parameter, and
        # x-amz-sdk-checksum-algorithm is not signed (ChecksumAlgorithm is not passed)
        self.assertEqual(q["X-Amz-SignedHeaders"],
                         ["content-length;content-md5;content-type;host;x-amz-checksum-sha256"])
        self.assertNotIn("x-amz-sdk-checksum-algorithm", q["X-Amz-SignedHeaders"][0])
        self.assertFalse([k for k in q if "checksum" in k.lower()], "no checksum query parameter")
        self.assertEqual(q["X-Amz-Expires"], ["900"])
        self.assertEqual(urlparse(url).path, "/edubotics-train/train/t1/a1/s0002500/pretrained_model/config.json")
        self.assertEqual(headers, {"Content-Type": "application/octet-stream", "Content-Length": "1665",
                                   "Content-MD5": md5, "x-amz-checksum-sha256": sha_b64})
        self.assertNotIn("test-secret", url)

    def test_another_length_or_sha256_is_another_signature(self):
        md5 = base64.b64encode(hashlib.md5(b"y").digest()).decode()
        sha_a, sha_b = hashlib.sha256(b"a").hexdigest(), hashlib.sha256(b"b").hexdigest()
        with patch("botocore.auth.datetime") as fake_dt:
            import datetime as _dt
            fake_dt.datetime.now.return_value = _dt.datetime(2026, 10, 28, 12, 0, 0, tzinfo=_dt.timezone.utc)
            fake_dt.datetime.utcnow.return_value = _dt.datetime(2026, 10, 28, 12, 0, 0)
            a, _ = r2_client.presign_put("k", 10, md5, sha_a)
            b, _ = r2_client.presign_put("k", 11, md5, sha_a)
            c, _ = r2_client.presign_put("k", 10, md5, sha_b)
            a2, _ = r2_client.presign_put("k", 10, md5, sha_a)
        sig = lambda u: parse_qs(urlparse(u).query)["X-Amz-Signature"][0]  # noqa: E731
        self.assertNotEqual(sig(a), sig(b))
        self.assertNotEqual(sig(a), sig(c))
        self.assertEqual(sig(a), sig(a2), "same inputs at the same instant sign alike")

    def test_presign_put_refuses_a_malformed_sha256(self):
        md5 = base64.b64encode(hashlib.md5(b"y").digest()).decode()
        for bad in ("", "zz", "ab" * 31, "ab" * 33):
            with self.assertRaises(ValueError):
                r2_client.presign_put("k", 1, md5, bad)

    def test_a_presigned_get(self):
        url = r2_client.presign_get("train/t1/a1/s0002500/training_state/rng_state.safetensors")
        q = parse_qs(urlparse(url).query)
        self.assertEqual(q["X-Amz-Expires"], ["3600"])
        self.assertEqual(q["X-Amz-SignedHeaders"], ["host"])

    def test_copy_object_classifies_unsupported_and_missing(self):
        c, st = self._stubbed()
        st.add_response("copy_object", {}, {"Bucket": "edubotics-train", "Key": "keep/x",
                                            "CopySource": {"Bucket": "edubotics-train", "Key": "train/x"}})
        for code, status in (("NotImplemented", 501), ("BadRequest", 400), ("NotImplemented", 400)):
            st.add_client_error("copy_object", service_error_code=code, http_status_code=status)
        st.add_client_error("copy_object", service_error_code="NoSuchKey", http_status_code=404)
        st.add_client_error("copy_object", service_error_code="InternalError", http_status_code=500)
        with st:
            r2_client.copy_object("train/x", "keep/x")  # a 200 returns
            for _ in range(3):
                with self.assertRaises(r2_client.R2CopyUnsupported):
                    r2_client.copy_object("train/x", "keep/x")
            with self.assertRaises(r2_client.R2Missing):
                r2_client.copy_object("train/x", "keep/x")
            with self.assertRaises(Exception) as ctx:
                r2_client.copy_object("train/x", "keep/x")
            self.assertNotIsInstance(ctx.exception, (r2_client.R2CopyUnsupported, r2_client.R2Missing))

    def test_put_file_presigned_sends_the_three_signed_headers(self):
        import httpx

        data = b"z" * 2048
        path = os.path.join(self._tmp(), "blob")
        with open(path, "wb") as fh:
            fh.write(data)
        md5 = base64.b64encode(hashlib.md5(data).digest()).decode()
        seen = {}

        def handler(request):
            seen["headers"] = dict(request.headers)
            seen["body_len"] = len(request.read())
            return httpx.Response(200)

        real_client = httpx.Client

        def fake_client(**kw):
            return real_client(transport=httpx.MockTransport(handler),
                               **{k: v for k, v in kw.items() if k != "transport"})

        with patch.object(httpx, "Client", fake_client):
            r2_client.put_file_presigned("keep/t1/s0002500/pretrained_model/config.json", path)
        self.assertEqual(seen["headers"]["content-type"], "application/octet-stream")
        self.assertEqual(seen["headers"]["content-length"], "2048")
        self.assertEqual(seen["headers"]["content-md5"], md5)
        self.assertEqual(seen["body_len"], 2048)
        # G0 failed: the kept-copy fallback keeps today's three signed headers
        self.assertEqual({k for k in seen["headers"] if k.startswith(("content-", "x-amz-"))},
                         {"content-type", "content-length", "content-md5"})

    def test_put_file_presigned_raises_on_403(self):
        import httpx

        path = os.path.join(self._tmp(), "b2")
        with open(path, "wb") as fh:
            fh.write(b"x")
        real_client = httpx.Client

        def fake_client(**kw):
            return real_client(transport=httpx.MockTransport(lambda r: httpx.Response(403)),
                               **{k: v for k, v in kw.items() if k != "transport"})

        with patch.object(httpx, "Client", fake_client):
            with self.assertRaises(RuntimeError):
                r2_client.put_file_presigned("keep/x", path)

    def _tmp(self):
        import tempfile

        d = tempfile.mkdtemp(prefix="r2test-")
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        return d

    def _stubbed(self):
        from botocore.stub import Stubber

        c = r2_client._client()
        return c, Stubber(c)

    def test_head_get_and_missing(self):
        from botocore.response import StreamingBody

        c, st = self._stubbed()
        st.add_response("head_object", {"ContentLength": 18}, {"Bucket": "edubotics-train", "Key": "a"})
        st.add_client_error("head_object", service_error_code="404", http_status_code=404,
                            expected_params={"Bucket": "edubotics-train", "Key": "b"})
        body = b'{\n    "step": 20\n}'
        st.add_response("get_object", {"Body": StreamingBody(io.BytesIO(body), len(body))},
                        {"Bucket": "edubotics-train", "Key": "c"})
        st.add_client_error("get_object", service_error_code="NoSuchKey", http_status_code=404,
                            expected_params={"Bucket": "edubotics-train", "Key": "d"})
        st.add_client_error("head_object", service_error_code="InternalError", http_status_code=500,
                            expected_params={"Bucket": "edubotics-train", "Key": "e"})
        with st:
            self.assertEqual(r2_client.head_size("a"), 18)
            with self.assertRaises(r2_client.R2Missing):
                r2_client.head_size("b")
            self.assertEqual(r2_client.get_bytes("c", 4096), body)
            with self.assertRaises(r2_client.R2Missing):
                r2_client.get_bytes("d", 10)
            with self.assertRaises(Exception) as ctx:
                r2_client.head_size("e")
            self.assertNotIsInstance(ctx.exception, r2_client.R2Missing)

    def test_head_meta_asks_for_the_checksum_and_decodes_it(self):
        blob = b"q" * 100
        sha = hashlib.sha256(blob)
        c, st = self._stubbed()
        st.add_response("head_object", {"ContentLength": 100, "ChecksumSHA256": base64.b64encode(sha.digest()).decode()},
                        {"Bucket": "edubotics-train", "Key": "a", "ChecksumMode": "ENABLED"})
        st.add_response("head_object", {"ContentLength": 7}, {"Bucket": "edubotics-train", "Key": "b",
                                                              "ChecksumMode": "ENABLED"})
        # a composite multipart value and a 20-byte value are not a whole-object SHA-256
        st.add_response("head_object", {"ContentLength": 9,
                                        "ChecksumSHA256": base64.b64encode(sha.digest()).decode() + "-3"},
                        {"Bucket": "edubotics-train", "Key": "c", "ChecksumMode": "ENABLED"})
        st.add_response("head_object", {"ContentLength": 9,
                                        "ChecksumSHA256": base64.b64encode(b"x" * 20).decode()},
                        {"Bucket": "edubotics-train", "Key": "d", "ChecksumMode": "ENABLED"})
        st.add_client_error("head_object", service_error_code="404", http_status_code=404,
                            expected_params={"Bucket": "edubotics-train", "Key": "e", "ChecksumMode": "ENABLED"})
        st.add_client_error("head_object", service_error_code="InternalError", http_status_code=500,
                            expected_params={"Bucket": "edubotics-train", "Key": "f", "ChecksumMode": "ENABLED"})
        with st:
            self.assertEqual(r2_client.head_meta("a"), (100, sha.hexdigest()))
            self.assertEqual(r2_client.head_meta("b"), (7, None))
            self.assertEqual(r2_client.head_meta("c"), (9, None))
            self.assertEqual(r2_client.head_meta("d"), (9, None))
            with self.assertRaises(r2_client.R2Missing):
                r2_client.head_meta("e")
            with self.assertRaises(Exception) as ctx:
                r2_client.head_meta("f")
            self.assertNotIsInstance(ctx.exception, r2_client.R2Missing)
        st.assert_no_pending_responses()

    def test_head_meta_sends_the_checksum_mode_header_on_the_wire(self):
        # beyond the Stubber's parameter check: the serialised request carries the header
        seen = {}

        def capture(request, **kw):
            seen.update({k.lower(): (v.decode() if isinstance(v, bytes) else v) for k, v in request.headers.items()})

        c = r2_client._client()
        c.meta.events.register("before-send.s3.HeadObject", lambda request, **kw: (capture(request), _fake_head())[1])
        self.assertEqual(r2_client.head_meta("k")[1], hashlib.sha256(b"").hexdigest())
        self.assertEqual(seen.get("x-amz-checksum-mode"), "ENABLED")

    def test_stream_hashes_and_delete(self):
        from botocore.response import StreamingBody

        c, st = self._stubbed()
        blob = os.urandom(3 * r2_client.STREAM_CHUNK + 7)
        st.add_response("get_object", {"Body": StreamingBody(io.BytesIO(blob), len(blob))},
                        {"Bucket": "edubotics-train", "Key": "s"})
        st.add_response("delete_objects", {"Deleted": [{"Key": "x"}]},
                        {"Bucket": "edubotics-train", "Delete": {"Objects": [{"Key": "x"}], "Quiet": True}})
        st.add_response("delete_objects", {"Errors": [{"Key": "y", "Code": "AccessDenied"}]},
                        {"Bucket": "edubotics-train", "Delete": {"Objects": [{"Key": "y"}], "Quiet": True}})
        with st:
            self.assertEqual(r2_client.stream_sha256("s"), (len(blob), hashlib.sha256(blob).hexdigest()))
            r2_client.delete_keys(["x"])
            with self.assertRaises(RuntimeError):
                r2_client.delete_keys(["y"])
            r2_client.delete_keys([])


def _fake_head():
    from botocore.awsrequest import AWSResponse

    class _Raw:
        def stream(self, **kw):
            return iter([b""])

    empty = base64.b64encode(hashlib.sha256(b"").digest()).decode()
    return AWSResponse("https://x", 200, {"Content-Length": "0", "x-amz-checksum-sha256": empty}, _Raw())


def _load_gate():
    path = os.path.join(os.path.dirname(__file__), "..", "..", "..", "runpod_training", "e2e", "r2_checksum_gate.py")
    spec = importlib.util.spec_from_file_location("r2_checksum_gate_under_test", os.path.abspath(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeR2:
    """R2 as the gate expects it to behave (``honest``) or not: a signed URL carries the
    exact headers it was signed with; a PUT whose headers differ is 403; a body whose
    SHA-256 differs from the signed one is 400 BadDigest unless ``enforce`` is off."""

    R2Missing = r2_client.R2Missing
    R2CopyUnsupported = r2_client.R2CopyUnsupported

    def __init__(self, enforce=True, returns_checksum=True, copy_keeps=False):
        self.enforce, self.returns_checksum, self.copy_keeps = enforce, returns_checksum, copy_keeps
        self.objects: dict = {}
        self.signed: dict = {}
        self.deleted: list = []

    def presign_put(self, key, size, md5_b64, sha256_hex):
        hdr = {"Content-Type": "application/octet-stream", "Content-Length": str(size), "Content-MD5": md5_b64,
               "x-amz-checksum-sha256": base64.b64encode(bytes.fromhex(sha256_hex)).decode()}
        url = f"https://fake-r2.example/{key}?X-Amz-Signature=SECRETSIGNATURE{len(self.signed)}"
        self.signed[url] = (key, dict(hdr))
        return url, hdr

    def head_meta(self, key):
        if key not in self.objects:
            raise r2_client.R2Missing(key)
        data, sha = self.objects[key]
        return len(data), sha

    def exists(self, key):
        return key in self.objects

    def copy_object(self, src, dst):
        data, sha = self.objects[src]
        self.objects[dst] = (data, sha if self.copy_keeps else None)

    def delete_keys(self, keys):
        self.deleted.extend(keys)
        for k in keys:
            self.objects.pop(k, None)

    def put(self, url, content, headers):
        import httpx

        key, signed = self.signed[url]
        sent = {k.lower(): v for k, v in headers.items()}
        if {k.lower(): v for k, v in signed.items()} != sent:
            return httpx.Response(403, content=b"<Error><Code>SignatureDoesNotMatch</Code><StringToSign>x</StringToSign></Error>")
        claimed = base64.b64decode(sent["x-amz-checksum-sha256"]).hex()
        if self.enforce and claimed != hashlib.sha256(content).hexdigest():
            return httpx.Response(400, content=b"<Error><Code>BadDigest</Code></Error>")
        self.objects[key] = (content, claimed if self.returns_checksum else None)
        return httpx.Response(200)


class ChecksumGate(unittest.TestCase):
    """runpod_training/e2e/r2_checksum_gate.py (spec §9.9): the refusals happen before
    any request, the plan without --run, the exit codes, the cleanup, no secret printed."""

    GOOD = {"R2_GATE_ENDPOINT_URL": "https://acct.r2.cloudflarestorage.com", "R2_GATE_ACCESS_KEY_ID": "AKID",
            "R2_GATE_SECRET_ACCESS_KEY": "SECRETKEY", "R2_GATE_BUCKET": "edubotics-training-test"}

    def setUp(self):
        self.gate = _load_gate()

    def _main(self, argv, env):
        import contextlib

        buf = io.StringIO()
        calls = []
        with patch.object(self.gate, "_import_r2_client", lambda: calls.append(1) or (_ for _ in ()).throw(
                AssertionError("no request may be made"))), contextlib.redirect_stdout(buf):
            code = self.gate.main(argv, env=env)
        return code, buf.getvalue(), calls

    def test_every_missing_or_empty_variable_is_refused_before_any_request(self):
        for name in self.gate.GATE_VARS:
            for value in (None, "", "   "):
                env = dict(self.GOOD)
                if value is None:
                    env.pop(name)
                else:
                    env[name] = value
                code, text, calls = self._main(["--run"], env)
                self.assertEqual(code, 2)
                self.assertEqual(calls, [])
                self.assertIn(name, text)
                self.assertEqual(len(text.strip().splitlines()), 1)

    def test_the_production_bucket_is_refused(self):
        for bucket in ("edubotics-training", " Edubotics-Training ", "EDUBOTICS-TRAINING"):
            code, text, calls = self._main(["--run"], dict(self.GOOD, R2_GATE_BUCKET=bucket))
            self.assertEqual((code, calls), (2, []))
            self.assertIn("production bucket", text)

    def test_a_bucket_equal_to_r2_bucket_or_r2_production_bucket_is_refused(self):
        for other in ("R2_BUCKET", "R2_PRODUCTION_BUCKET"):
            code, text, calls = self._main(["--run"], dict(self.GOOD, **{other: "edubotics-training-test"}))
            self.assertEqual((code, calls), (2, []))
            self.assertIn(other, text)
        # a different R2_BUCKET is not a refusal (the plan is printed)
        code, text, calls = self._main([], dict(self.GOOD, R2_BUCKET="something-else"))
        self.assertEqual((code, calls), (2, []))
        self.assertIn("Pass --run", text)

    def test_without_run_the_plan_is_printed_and_nothing_is_contacted(self):
        code, text, calls = self._main([], dict(self.GOOD))
        self.assertEqual((code, calls), (2, []))
        self.assertIn("G0 plan", text)
        for secret in self.GOOD.values():
            self.assertNotIn(secret, text)

    def test_the_gate_reads_exactly_its_four_variables(self):
        self.assertEqual(self.gate.GATE_VARS, ("R2_GATE_ENDPOINT_URL", "R2_GATE_ACCESS_KEY_ID",
                                               "R2_GATE_SECRET_ACCESS_KEY", "R2_GATE_BUCKET"))
        self.assertEqual(self.gate.PRODUCTION_BUCKET, "edubotics-training")

    def _run(self, fake, **kw):
        return self.gate.run_gate(fake, fake, run_id="0123abcd", **kw)

    def test_an_enforcing_r2_passes(self):
        fake = _FakeR2()
        code, out = self._run(fake)
        self.assertEqual((code, out["verdict"]), (0, "pass"))
        self.assertEqual((out["a"]["status"], out["a"]["code"], out["a"]["stored"]), (400, "BadDigest", False))
        self.assertEqual(out["b"]["status"], 200)
        self.assertTrue(out["c"]["sha256_ok"] and out["c"]["size_ok"])
        self.assertEqual((out["d"]["status"], out["d"]["code"]), (403, "SignatureDoesNotMatch"))
        self.assertEqual(out["d2"]["status"], 403)
        self.assertEqual(out["e"], {"copied": True, "sha256_present": False, "sha256_ok": False})
        self.assertEqual(out["f"], {"deleted": True, "left": 0, "ok": True})
        self.assertEqual(fake.objects, {})
        self.assertTrue(fake.deleted and all(k.startswith("train/gate/0123abcd/") for k in fake.deleted))
        text = json.dumps(out)
        for secret in ("SECRETSIGNATURE", "fake-r2.example", "StringToSign"):
            self.assertNotIn(secret, text)

    def test_an_r2_that_ignores_the_checksum_fails(self):
        code, out = self._run(_FakeR2(enforce=False))
        self.assertEqual((code, out["verdict"]), (1, "fail"))
        self.assertFalse(out["a"]["ok"])
        self.assertTrue(out["f"]["ok"], "cleanup ran after the failed step")

    def test_an_r2_that_returns_no_checksum_fails_step_c(self):
        code, out = self._run(_FakeR2(returns_checksum=False))
        self.assertEqual(code, 1)
        self.assertFalse(out["c"]["ok"])
        self.assertFalse(out["c"]["sha256_present"])

    def test_a_transport_error_is_inconclusive_and_still_cleans_up(self):
        import httpx

        fake = _FakeR2()
        real_put = fake.put
        state = {"n": 0}

        def flaky(url, content, headers):
            state["n"] += 1
            if state["n"] == 2:
                raise httpx.ConnectError("boom https://fake-r2.example/secret")
            return real_put(url, content, headers)

        fake.put = flaky
        code, out = self._run(fake)
        self.assertEqual((code, out["verdict"]), (2, "inconclusive"))
        self.assertEqual(out["inconclusive"], "PUT ConnectError")
        self.assertNotIn("fake-r2.example", json.dumps(out))
        self.assertEqual(fake.objects, {})
        self.assertEqual(len(fake.deleted), 5)

    def test_a_failed_cleanup_never_reads_as_a_pass(self):
        fake = _FakeR2()
        fake.delete_keys = lambda keys: (_ for _ in ()).throw(RuntimeError("nope"))
        code, out = self._run(fake)
        self.assertEqual(code, 2)
        self.assertFalse(out["f"]["ok"])


if __name__ == "__main__":
    unittest.main()
