"""A deny-by-default reverse proxy in front of a cloudflared quick tunnel (spec §9.0, SR-23). E2E only.

    python tunnel_gate.py --listen 9101 --upstream http://127.0.0.1:8000 --allow 'POST /trainings/worker/'
    python tunnel_gate.py --listen 9102 --upstream http://127.0.0.1:54321 \\
        --allow 'POST /rest/v1/rpc/update_training_progress'
    # F21: the first matching request reaches the upstream, its answer is thrown away and the
    # client's connection is closed with no reply; every later request passes normally.
    ... --drop-once 'POST /trainings/worker/' ...

Only a request whose method matches and whose path starts with an --allow prefix is
forwarded; everything else gets 404. A raw path with '%', '..' or a backslash is
refused (no decoding ambiguity between what is checked and what is forwarded), and
X-Forwarded-For / Forwarded / X-Real-IP are stripped. Bodies are capped at 1 MiB.
"""

import argparse

from aiohttp import ClientSession, ClientTimeout, web

HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
       "transfer-encoding", "upgrade", "host", "content-length"}
STRIPPED = {"x-forwarded-for", "forwarded", "x-real-ip"}
MAX_BODY = 1024 * 1024


def _rule(text):
    method, _, prefix = text.strip().partition(" ")
    if not method or not prefix.startswith("/"):
        raise argparse.ArgumentTypeError(f"expected '<METHOD> /path-prefix', got {text!r}")
    return method.upper(), prefix


def build_app(upstream, allow, drop_once=None):
    state = {"drop": drop_once}

    def matches(rule, request):
        return request.method == rule[0] and request.raw_path.split("?", 1)[0].startswith(rule[1])

    async def handle(request):
        raw = request.raw_path
        if "%" in raw or ".." in raw or "\\" in raw or not any(matches(r, request) for r in allow):
            return web.Response(status=404)
        body = await request.content.read(MAX_BODY + 1)
        if len(body) > MAX_BODY:
            return web.Response(status=413)
        headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in HOP and k.lower() not in STRIPPED}
        async with request.app["session"].request(request.method, upstream + raw, headers=headers,
                                                  data=body, allow_redirects=False) as r:
            data = await r.read()
            if state["drop"] is not None and matches(state["drop"], request):
                state["drop"] = None
                request.transport.close()          # the answer is discarded; no reply at all
                raise web.HTTPException()
            out = {k: v for k, v in r.headers.items() if k.lower() not in HOP}
            return web.Response(status=r.status, body=data, headers=out)

    async def on_startup(app):
        app["session"] = ClientSession(timeout=ClientTimeout(total=600), auto_decompress=False)

    async def on_cleanup(app):
        await app["session"].close()

    app = web.Application(client_max_size=MAX_BODY + 1)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_route("*", "/{tail:.*}", handle)
    return app


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", type=int, required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--upstream", required=True)
    ap.add_argument("--allow", type=_rule, action="append", required=True)
    ap.add_argument("--drop-once", type=_rule)
    a = ap.parse_args()
    web.run_app(build_app(a.upstream.rstrip("/"), a.allow, a.drop_once), host=a.host, port=a.listen)


if __name__ == "__main__":
    main()
