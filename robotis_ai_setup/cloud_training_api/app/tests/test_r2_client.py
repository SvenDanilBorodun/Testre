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

        src = open(r2_client.__file__, encoding="utf-8").read()
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

    def test_a_presigned_put_signs_length_md5_and_type(self):
        data = b"x" * 1665
        md5 = base64.b64encode(hashlib.md5(data).digest()).decode()
        url, headers = r2_client.presign_put("train/t1/a1/s0002500/pretrained_model/config.json", len(data), md5)
        q = parse_qs(urlparse(url).query)
        self.assertEqual(q["X-Amz-SignedHeaders"], ["content-length;content-md5;content-type;host"])
        self.assertEqual(q["X-Amz-Expires"], ["900"])
        self.assertEqual(urlparse(url).path, "/edubotics-train/train/t1/a1/s0002500/pretrained_model/config.json")
        self.assertEqual(headers, {"Content-Type": "application/octet-stream", "Content-Length": "1665",
                                   "Content-MD5": md5})
        self.assertNotIn("test-secret", url)

    def test_another_length_is_another_signature(self):
        md5 = base64.b64encode(hashlib.md5(b"y").digest()).decode()
        a, _ = r2_client.presign_put("k", 10, md5)
        b, _ = r2_client.presign_put("k", 11, md5)
        sig = lambda u: parse_qs(urlparse(u).query)["X-Amz-Signature"][0]  # noqa: E731
        self.assertNotEqual(sig(a), sig(b))

    def test_a_presigned_get(self):
        url = r2_client.presign_get("train/t1/a1/s0002500/training_state/rng_state.safetensors")
        q = parse_qs(urlparse(url).query)
        self.assertEqual(q["X-Amz-Expires"], ["3600"])
        self.assertEqual(q["X-Amz-SignedHeaders"], ["host"])

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


if __name__ == "__main__":
    unittest.main()
