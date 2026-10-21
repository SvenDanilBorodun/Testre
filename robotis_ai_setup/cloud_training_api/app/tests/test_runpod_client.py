"""runpod_client: exactly §3.6's calls, through httpx.MockTransport (no network).

Pinned: the base URL and Bearer key, the catalog query (AVAILABILITY / POD /
cloud / the 30 EEA codes / CUDA 12.6, and the worldwide form without the
codes, owner E42), Secure's GDPR list, list pagination by nextCursor, a transport failure as
RunpodNetworkError, problem+json details cut to 300 characters, Retry-After,
and the 20 s timeout with no redirect following.
"""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

import httpx

from app.services import runpod_client as rc


class Recorder:
    def __init__(self, answer):
        self.answer = answer
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return self.answer(request)


class RunpodClient(unittest.IsolatedAsyncioTestCase):
    def use(self, answer):
        rec = Recorder(answer)
        rc.set_transport_for_tests(httpx.MockTransport(rec))
        self.addCleanup(rc.set_transport_for_tests, None)
        return rec

    async def test_auth_base_and_catalog_query(self):
        rec = self.use(lambda r: httpx.Response(200, json={"gpus": []}))
        with patch.dict(os.environ, {"RUNPOD_API_KEY": "rp-test", "RUNPOD_API_BASE": "https://runpod.test/"}):
            resp = await rc.catalog_gpus("COMMUNITY")
        self.assertEqual((resp.status, resp.body), (200, {"gpus": []}))
        (req,) = rec.requests
        self.assertEqual(req.headers["authorization"], "Bearer rp-test")
        self.assertEqual(str(req.url.copy_with(query=None)), "https://runpod.test/v2/catalog/gpus")
        q = dict(req.url.params)
        self.assertEqual({k: q[k] for k in ("include", "product", "cloud", "minCudaVersion")},
                         {"include": "AVAILABILITY", "product": "POD", "cloud": "COMMUNITY", "minCudaVersion": "12.6"})
        self.assertEqual(q["countryCodes"].split(","), list(rc.EEA_COUNTRY_CODES))
        self.assertEqual(len(rc.EEA_COUNTRY_CODES), 30)
        self.assertNotIn("GB", rc.EEA_COUNTRY_CODES)
        self.assertNotIn("CH", rc.EEA_COUNTRY_CODES)
        self.assertEqual(list(q), ["include", "product", "cloud", "countryCodes", "minCudaVersion"])
        # Owner E42: the worldwide form drops only the countryCodes key.
        with patch.dict(os.environ, {"RUNPOD_API_KEY": "rp-test", "RUNPOD_API_BASE": "https://runpod.test/"}):
            await rc.catalog_gpus("COMMUNITY", worldwide=True)
        world = dict(rec.requests[1].url.params)
        self.assertNotIn("countryCodes", world)
        self.assertEqual(world, {k: v for k, v in q.items() if k != "countryCodes"})

    async def test_the_data_centre_calls(self):
        rec = self.use(lambda r: httpx.Response(200, json={"dataCenters": []}))
        await rc.datacenters_gdpr()
        self.assertEqual(dict(rec.requests[0].url.params), {"regions": "EUROPE", "compliance": "GDPR"})

    async def test_list_pods_follows_the_cursor(self):
        pages = {None: ([{"id": "a"}, {"id": "b"}], "c1", True), "c1": ([{"id": "c"}], None, False)}

        def answer(r):
            pods, nxt, more = pages[r.url.params.get("cursor")]
            self.assertEqual(r.url.params["limit"], "1000")
            return httpx.Response(200, json={"pods": pods, "pagination": {"nextCursor": nxt, "hasNextPage": more}})

        self.use(answer)
        self.assertEqual([p["id"] for p in await rc.list_pods()], ["a", "b", "c"])

    async def test_list_errors(self):
        self.use(lambda r: httpx.Response(500, json={"detail": "x"}))
        with self.assertRaises(rc.RunpodListError):
            await rc.list_pods()
        self.use(lambda r: httpx.Response(200, json={"items": []}))
        with self.assertRaises(rc.RunpodListError):
            await rc.list_pods()

    async def test_a_transport_failure(self):
        def boom(r):
            raise httpx.ConnectTimeout("slow")

        self.use(boom)
        with self.assertRaises(rc.RunpodNetworkError) as ctx:
            await rc.create_pod({"name": "x", "env": {"EDUBOTICS_WORKER_TOKEN": "secret-token"}})
        self.assertNotIn("secret-token", str(ctx.exception))

    async def test_problem_details_and_retry_after(self):
        self.use(lambda r: httpx.Response(429, json={"title": "Too Many", "status": 429, "detail": "x" * 400},
                                          headers={"Retry-After": "12"}))
        resp = await rc.create_pod({"name": "x"})
        self.assertEqual((resp.status, resp.retry_after, len(resp.detail)), (429, 12.0, 300))
        self.use(lambda r: httpx.Response(429, headers={"Retry-After": "soon"}))
        resp = await rc.get_pod("p")
        self.assertIsNone(resp.retry_after)
        self.assertEqual(resp.detail, "")

    async def test_pod_calls(self):
        rec = self.use(lambda r: httpx.Response(204) if r.method == "DELETE" else httpx.Response(200, json={"id": "p"}))
        self.assertEqual((await rc.delete_pod("pod_1")).status, 204)
        await rc.patch_pod("pod_1", {"locked": False})
        await rc.get_pod("pod_1")
        self.assertEqual([(r.method, r.url.path) for r in rec.requests],
                         [("DELETE", "/v2/pods/pod_1"), ("PATCH", "/v2/pods/pod_1"), ("GET", "/v2/pods/pod_1")])
        self.assertEqual(json.loads(rec.requests[1].content), {"locked": False})

    def test_the_client_settings(self):
        rc.set_transport_for_tests(None)
        client = rc._get_client()
        self.addCleanup(rc.set_transport_for_tests, None)
        self.assertEqual(client.timeout.read, 20.0)
        self.assertFalse(client.follow_redirects)
        self.assertEqual(rc.api_base(), os.environ.get("RUNPOD_API_BASE", "https://api.runpod.io").rstrip("/"))
        self.assertEqual(rc.GONE_STATUSES, {"EXITED", "ERROR", "TERMINATED"})


if __name__ == "__main__":
    unittest.main()
