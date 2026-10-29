"""Proof gate G0 (spec §9.9, owner G0 = B): does Cloudflare R2 enforce the signed SHA-256?

Migration 052's OQ-1 binds a checkpoint's bytes to its manifest at upload: the API's
presigned PUT signs ``x-amz-checksum-sha256`` (``r2_client.presign_put``) and
ckpt-done reads R2's stored checksum back (``r2_client.head_meta``). Cloudflare
announces SHA-256 on PutObject but documents neither its validation on a PRESIGNED
PUT nor its return on HEAD, so this gate measures both, once, on the operator's R2
TEST bucket — never the production bucket.

It reads exactly four variables, ``R2_GATE_ENDPOINT_URL``, ``R2_GATE_ACCESS_KEY_ID``,
``R2_GATE_SECRET_ACCESS_KEY`` and ``R2_GATE_BUCKET``, and refuses (one line, exit 2,
no request made) when one is missing or empty, and when the bucket is the production
bucket ``edubotics-training`` or equals ``R2_BUCKET`` / ``R2_PRODUCTION_BUCKET`` when
either is set. Only then does it copy the four into ``R2_*`` of its OWN process and
reset ``r2_client``. Without ``--run`` it prints its plan and exits 2. It writes only
under ``train/gate/<uuid>/`` and deletes everything it wrote, also after a failed step.

Steps (bodies A and B: 1 MiB each, random, same length):
  (a) URL signed with A's MD5 but B's SHA-256, PUT A with exactly the returned headers
      → must not be 2xx, and nothing is stored;
  (b) URL signed with A's MD5 and SHA-256, PUT A → 2xx;
  (c) head_meta of (b) → 1 MiB and A's SHA-256;
  (d) a fresh URL signed for A, PUT A WITHOUT the checksum header → 403, nothing stored;
  (d') a fresh URL signed for A, PUT A with the header carrying B's SHA-256 → 403,
       nothing stored;
  (e) informational: copy_object of (b), head_meta of the copy;
  (f) delete every key of the run (and confirm each is gone).

Output: ONE JSON line (statuses, error codes, booleans) — never a key, a URL, a
signature or a response body. Exit 0 when (a)–(d') hold, 1 when one fails, 2 when
inconclusive (a transport error, or a refusal before any request).

Run from ``robotis_ai_setup/cloud_training_api`` with only the test bucket's file
loaded into that one process:
    ( set -a; . ~/.edubotics-test/r2-test.env; set +a; \
      python ../runpod_training/e2e/r2_checksum_gate.py --run )
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import uuid
from pathlib import Path

GATE_VARS = ("R2_GATE_ENDPOINT_URL", "R2_GATE_ACCESS_KEY_ID", "R2_GATE_SECRET_ACCESS_KEY", "R2_GATE_BUCKET")
PRODUCTION_BUCKET = "edubotics-training"  # docs/deploy/DEPLOY.md 3b
BODY_BYTES = 1024 * 1024
KEY_ROOT = "train/gate/"
_CODE_RE = re.compile(r"<Code>([A-Za-z0-9_.-]{1,64})</Code>")

PLAN = ("G0 plan: (a) PUT A under a URL signed for B's SHA-256 must not be 2xx; (b) PUT A signed for A "
        "must be 2xx; (c) HEAD returns 1 MiB and A's SHA-256; (d) PUT without the checksum header must be "
        "403; (d') PUT with B's SHA-256 in the header must be 403; (e) CopyObject + HEAD (informational); "
        "(f) delete every key under train/gate/<uuid>/. Pass --run to execute.")


def _norm(value: str | None) -> str:
    return (value or "").strip().lower()


def check_config(env) -> str | None:
    """The refusal sentence, or None when the gate may run. Makes no request."""
    missing = [k for k in GATE_VARS if not (env.get(k) or "").strip()]
    if missing:
        return "G0 refused: missing or empty " + ", ".join(missing)
    gate_bucket = _norm(env.get("R2_GATE_BUCKET"))
    if gate_bucket == PRODUCTION_BUCKET:
        return "G0 refused: R2_GATE_BUCKET is the production bucket"
    for other in ("R2_BUCKET", "R2_PRODUCTION_BUCKET"):
        if _norm(env.get(other)) and gate_bucket == _norm(env.get(other)):
            return f"G0 refused: R2_GATE_BUCKET equals {other}"
    return None


def adopt_config(env) -> None:
    """Copy the four gate variables into this process's R2_* and reset the client."""
    os.environ["R2_ENDPOINT_URL"] = env["R2_GATE_ENDPOINT_URL"].strip()
    os.environ["R2_ACCESS_KEY_ID"] = env["R2_GATE_ACCESS_KEY_ID"].strip()
    os.environ["R2_SECRET_ACCESS_KEY"] = env["R2_GATE_SECRET_ACCESS_KEY"].strip()
    os.environ["R2_BUCKET"] = env["R2_GATE_BUCKET"].strip()
    from app.services import r2_client

    r2_client._reset_for_tests()


def _error_code(body: bytes) -> str | None:
    m = _CODE_RE.search(body[:4096].decode("utf-8", "replace"))
    return m.group(1) if m else None


class Inconclusive(Exception):
    """A transport error or an unexpected API failure: the gate cannot judge."""


def _put(client, url: str, headers: dict, body: bytes) -> dict:
    import httpx

    try:
        resp = client.put(url, content=body, headers=headers)
    except (httpx.HTTPError, OSError) as exc:
        raise Inconclusive(f"PUT {type(exc).__name__}") from None
    return {"status": resp.status_code, "code": _error_code(resp.content)}


def _head(r2_client, key: str) -> dict:
    try:
        size, sha = r2_client.head_meta(key)
    except r2_client.R2Missing:
        return {"stored": False, "size": None, "sha256": None}
    except Exception as exc:  # botocore ClientError / transport
        raise Inconclusive(f"HEAD {type(exc).__name__}") from None
    return {"stored": True, "size": size, "sha256": sha}


def run_gate(r2_client, http_client, run_id: str | None = None, rand=os.urandom) -> tuple[int, dict]:
    """Steps (a)–(f). Returns (exit code, the JSON-able result). Pure in its inputs."""
    run_id = run_id or uuid.uuid4().hex
    prefix = f"{KEY_ROOT}{run_id}/"
    a, b = rand(BODY_BYTES), rand(BODY_BYTES)
    md5_a = base64.b64encode(hashlib.md5(a).digest()).decode("ascii")
    sha_a, sha_b = hashlib.sha256(a).hexdigest(), hashlib.sha256(b).hexdigest()
    sha_b_b64 = base64.b64encode(hashlib.sha256(b).digest()).decode("ascii")
    keys = {s: f"{prefix}{s}" for s in ("a", "b", "d", "d2", "e")}
    written = list(keys.values())
    out: dict = {"gate": "G0", "bytes": BODY_BYTES}
    code = 0
    try:
        # (a) the signature is valid (exactly the returned headers), only the SHA-256 is wrong
        url, hdr = r2_client.presign_put(keys["a"], len(a), md5_a, sha_b)
        out["a"] = _put(http_client, url, dict(hdr), a)
        out["a"].update(stored=_head(r2_client, keys["a"])["stored"])
        out["a"]["ok"] = not (200 <= out["a"]["status"] < 300) and not out["a"]["stored"]

        # (b) everything right → stored
        url, hdr = r2_client.presign_put(keys["b"], len(a), md5_a, sha_a)
        out["b"] = _put(http_client, url, dict(hdr), a)
        out["b"]["ok"] = 200 <= out["b"]["status"] < 300

        # (c) the stored checksum is returned
        h = _head(r2_client, keys["b"])
        out["c"] = {"stored": h["stored"], "size_ok": h["size"] == BODY_BYTES,
                    "sha256_present": h["sha256"] is not None, "sha256_ok": h["sha256"] == sha_a}
        out["c"]["ok"] = h["stored"] and out["c"]["size_ok"] and out["c"]["sha256_ok"]

        # (d) the signed header omitted → signature mismatch
        url, hdr = r2_client.presign_put(keys["d"], len(a), md5_a, sha_a)
        stripped = {k: v for k, v in dict(hdr).items() if k.lower() != "x-amz-checksum-sha256"}
        out["d"] = _put(http_client, url, stripped, a)
        out["d"].update(stored=_head(r2_client, keys["d"])["stored"])
        out["d"]["ok"] = out["d"]["status"] == 403 and not out["d"]["stored"]

        # (d') the header present but carrying B's SHA-256 (what a hostile host would send)
        url, hdr = r2_client.presign_put(keys["d2"], len(a), md5_a, sha_a)
        forged = dict(hdr)
        forged["x-amz-checksum-sha256"] = sha_b_b64
        out["d2"] = _put(http_client, url, forged, a)
        out["d2"].update(stored=_head(r2_client, keys["d2"])["stored"])
        out["d2"]["ok"] = out["d2"]["status"] == 403 and not out["d2"]["stored"]

        # (e) informational: does a CopyObject copy carry the checksum?
        out["e"] = {}
        if out["b"]["ok"]:
            try:
                r2_client.copy_object(keys["b"], keys["e"])
                he = _head(r2_client, keys["e"])
                out["e"] = {"copied": he["stored"], "sha256_present": he["sha256"] is not None,
                            "sha256_ok": he["sha256"] == sha_a}
            except r2_client.R2CopyUnsupported:
                out["e"] = {"copied": False, "unsupported": True}
            except Exception as exc:  # informational only, never decides
                out["e"] = {"copied": False, "error": type(exc).__name__}
        code = 0 if all(out[s]["ok"] for s in ("a", "b", "c", "d", "d2")) else 1
    except Inconclusive as exc:
        out["inconclusive"] = str(exc)
        code = 2
    except Exception as exc:  # never print a message (it may carry a URL)
        out["inconclusive"] = type(exc).__name__
        code = 2
    finally:
        out["f"] = _cleanup(r2_client, written)
    if not out["f"]["ok"] and code == 0:
        code = 2
    out["verdict"] = {0: "pass", 1: "fail", 2: "inconclusive"}[code]
    return code, out


def _cleanup(r2_client, keys: list[str]) -> dict:
    result = {"deleted": False, "left": None}
    try:
        r2_client.delete_keys(keys)
        result["deleted"] = True
    except Exception as exc:
        result["error"] = type(exc).__name__
    left = 0
    try:
        for k in keys:
            if r2_client.exists(k):
                left += 1
        result["left"] = left
    except Exception as exc:
        result["check_error"] = type(exc).__name__
    result["ok"] = result["deleted"] and result.get("left") == 0
    return result


def _import_r2_client():
    api_root = Path(__file__).resolve().parents[2] / "cloud_training_api"
    if str(api_root) not in sys.path:
        sys.path.insert(0, str(api_root))
    from app.services import r2_client

    return r2_client


def main(argv=None, env=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run", action="store_true", help="make the requests (else print the plan)")
    args = parser.parse_args(argv)
    env = dict(os.environ if env is None else env)
    refusal = check_config(env)
    if refusal:
        print(refusal)
        return 2
    if not args.run:
        print(PLAN)
        return 2
    r2_client = _import_r2_client()
    adopt_config(env)
    import httpx

    with httpx.Client(follow_redirects=False, timeout=httpx.Timeout(60.0, connect=15.0)) as http_client:
        code, out = run_gate(r2_client, http_client)
    print(json.dumps(out, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
