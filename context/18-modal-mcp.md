# 18 — Modal MCP Server (Autonomous Gateway)

> **Layer:** Autonomous-agent gateway over the EduBotics infrastructure
> **Location:** `C:\Users\svend\cloud\modal_mcp\mcp_server_stateless.py` (note: outside `Testre/`)
> **Owner:** Our code
> **Read this before:** adding/modifying MCP tools, changing bearer auth, deploying to Modal.

This is **not** part of the student-facing pipeline. It's a private MCP server used by Claude (and other agents) to query EduBotics state autonomously: list trainings, look up users, inspect HF models, check Docker Hub tag metadata.

---

## 1. File

```
modal_mcp/
└── mcp_server_stateless.py     # FastMCP server wrapped in FastAPI on Modal
```

That's it. One file, ~few hundred lines. No tests yet.

---

## 2. Deployment

### Modal app

| Property | Value |
|---|---|
| App name | `example-mcp-server-stateless` |
| Workspace | `svendanilborodun` |
| Secret | `mcp-edubotics` |

### Image

```python
modal.Image.debian_slim().pip_install(
    "fastapi==0.115.14",
    "fastmcp==2.10.6",
    "pydantic==2.11.10",
    "httpx==0.28.1",
    "huggingface_hub==0.28.1",
)
```

Python 3.12. All deps pinned.

### Deploy

```bash
cd modal_mcp
modal deploy mcp_server_stateless.py    # production
modal serve mcp_server_stateless.py     # dev (auto-reload)
```

### Test

```bash
# List tools
modal run -m mcp_server_stateless::test_tool

# Call a tool
modal run -m mcp_server_stateless::test_tool \
    --tool-name hf_model_info \
    --tool-args-json '{"repo_id":"lerobot/act"}'
```

---

## 3. Auth: Bearer token

Every `/mcp/*` request must carry `Authorization: Bearer ${MCP_BEARER_TOKEN}`. The `MCP_BEARER_TOKEN` value lives in the `mcp-edubotics` Modal Secret.

Middleware:

```python
@fastapi_app.middleware("http")
async def bearer_guard(request, call_next):
    if request.url.path.startswith("/mcp"):
        auth = request.headers.get("authorization", "")
        if auth != f"Bearer {expected_token}":
            return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await call_next(request)
```

Non-`/mcp*` paths (root health check) bypass the guard.

---

## 4. HTTP transport

- **Framework**: FastAPI + FastMCP
- **Mode**: `mcp.http_app(transport="streamable-http", stateless_http=True)`
- **Stateless**: every request fully independent; no session state on server (so Modal can scale horizontally without session replication)
- **Endpoint**: `<modal-url>/mcp/`
- **Streaming**: MCP's streamable HTTP transport for real-time multi-line results

---

## 5. Supabase integration

Internal helper:

```python
async def _supa_get(path: str, params: dict) -> Any:
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url, headers=headers, params=params)
        r.raise_for_status()
        return r.json()
```

Service-role key → bypasses RLS. Read-only access only (no write/delete tools exposed).

---

## 6. Exposed tools (6 total)

### Tool 1: `current_date_and_time(timezone="UTC") → str`

Returns ISO 8601 in the given IANA timezone. Pure local; no external calls.

```python
zone = ZoneInfo(timezone)  # raises ValueError if invalid
return datetime.now(zone).isoformat()
```

### Tool 2: `query_trainings(user_id=None, status=None, limit=20) → list[dict]`

Query Supabase `trainings` table.

| Param | Type | Notes |
|---|---|---|
| `user_id` | str (UUID) or None | filter by single user |
| `status` | str or None | one of `pending, running, succeeded, failed` |
| `limit` | int (default 20, max 100 enforced) | row limit |

Returns list with: `id, user_id, status, dataset_name, model_name, model_type, current_step, total_steps, current_loss, cloud_job_id, requested_at, terminated_at, error_message`. Ordered `requested_at.desc`.

PostgREST filters: `eq.{value}` for exact match.

### Tool 3: `query_classroom(classroom_id) → dict`

Two queries:
1. `classrooms` WHERE id = classroom_id
2. `users` WHERE classroom_id = ?, fields: `id, username, full_name, training_credits, created_at`, ordered `username.asc`

Returns:
```json
{
  "classroom": {...} or null,
  "student_count": <int>,
  "students": [...]
}
```

### Tool 4: `query_user(username_or_id) → dict`

UUID detection regex: `^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$` (case-insensitive).
- Match → filter by `id`
- Else → filter by `username`

Returns first row of: `id, username, full_name, email, role, classroom_id, training_credits, created_by, created_at`. Or `{"found": false, "query": "..."}` if empty.

### Tool 5: `hf_model_info(repo_id) → dict`

Public HuggingFace API (no auth needed). `huggingface_hub.HfApi().model_info(repo_id)`.

Returns:
```json
{
  "id": "...", "sha": "...", "created_at": "...", "last_modified": "...",
  "downloads": 0, "likes": 0, "private": false,
  "tags": [...], "pipeline_tag": "...",
  "files": [{"path": "README.md", "size": 1234}, ...]
}
```

`files` flattens `info.siblings` to simple path + size dicts.

### Tool 6: `check_docker_image(image, tag="latest") → dict`

Public Docker Hub API: `GET https://hub.docker.com/v2/repositories/{image}/tags/{tag}/`.

On 404 (tag missing):
```json
{"exists": false, "image": "...", "tag": "..."}
```

On 200:
```json
{
  "exists": true,
  "image": "nettername/physical-ai-server",
  "tag": "latest",
  "last_pushed": "2026-05-03T...",
  "full_size_bytes": 1234567890,
  "digest": "sha256:...",
  "platforms": [
    {"arch": "amd64", "os": "linux", "size_bytes": ..., "digest": "sha256:..."},
    ...
  ]
}
```

---

## 7. ASGI assembly

```python
@app.function(image=image, secrets=[secret])
@modal.asgi_app()
def web():
    expected_token = os.environ["MCP_BEARER_TOKEN"]
    mcp = make_mcp_server()           # builds FastMCP with all 6 tools
    mcp_app = mcp.http_app(transport="streamable-http", stateless_http=True)
    fastapi_app = FastAPI(lifespan=mcp_app.lifespan)

    @fastapi_app.middleware("http")
    async def bearer_guard(request, call_next):
        if request.url.path.startswith("/mcp"):
            if request.headers.get("authorization", "") != f"Bearer {expected_token}":
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)

    fastapi_app.mount("/", mcp_app)
    return fastapi_app
```

---

## 8. test_tool function

```python
@app.function(secrets=[secret])
async def test_tool(tool_name: str = "", tool_args_json: str = "{}"):
    transport = StreamableHttpTransport(
        url=f"{MCP_URL}/mcp/",
        headers={"Authorization": f"Bearer {token}"}
    )
    async with Client(transport) as client:
        tools = await client.list_tools()
        print("\n".join(t.name for t in tools))
        if tool_name:
            args = json.loads(tool_args_json)
            result = await client.call_tool(tool_name, args)
            # Print up to 2000 chars of structured_content or content
```

Used to smoke-test from local `modal run`.

---

## 9. Adding a new tool

1. Edit `mcp_server_stateless.py` — add a new `@mcp.tool()` decorated function.
2. Type hints + docstring required (FastMCP exposes them as the tool description).
3. Async if doing HTTP/DB.
4. **Read-only by default.** Adding write tools (DELETE, UPDATE) requires explicit user approval.
5. Test locally:
   ```bash
   modal serve mcp_server_stateless.py
   modal run -m mcp_server_stateless::test_tool --tool-name <new-tool> --tool-args-json '{...}'
   ```
6. Deploy:
   ```bash
   modal deploy mcp_server_stateless.py
   ```

For the workflow, see [`WORKFLOW-add-feature.md`](WORKFLOW-add-feature.md).

---

## 10. Configuration

Modal Secret `mcp-edubotics` provides:

| Var | Purpose |
|---|---|
| `MCP_BEARER_TOKEN` | Bearer header value clients must send |
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | Service-role key (read-only access in this server) |

Update via:
```bash
modal secret create mcp-edubotics --from-dotenv .env
```

Then redeploy: `modal deploy mcp_server_stateless.py`.

---

## 11. Footguns

1. **Don't expose write tools without approval.** This server has Supabase service-role access. A `delete_user` or `update_credits` tool is one prompt-injection away from disaster.
2. **Don't trust LLM-supplied UUIDs.** Always validate format before passing to PostgREST (the regex in `query_user` is the model).
3. **Don't paginate naively** — the 100-row hard cap on `query_trainings` is intentional.
4. **Don't log the bearer token.** Keep it out of stdout.
5. **Don't add session state.** `stateless_http=True` is required for Modal horizontal scaling.

---

## 12. Cross-references

- Project context (where this fits): [`01-architecture.md`](01-architecture.md) §3
- Auto-memory note about this server: `~/.claude/projects/.../memory/reference_modal_mcp.md`

---

**Last verified:** 2026-05-04.
