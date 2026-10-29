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

G0 tests the SHIPPED ``r2_client.presign_put`` (since G0b: variant V1, the SHA-256
signed, no Content-MD5). Steps (bodies A and B: 1 MiB each, random, same length):
  (a) URL signed with B's SHA-256, PUT A with exactly the returned headers
      → must not be 2xx, and nothing is stored;
  (b) URL signed with A's SHA-256, PUT A → 2xx;
  (c) head_meta of (b) → 1 MiB and A's SHA-256;
  (d) a fresh URL signed for A, PUT A WITHOUT the checksum header → 403, nothing stored;
  (d') a fresh URL signed for A, PUT A with the header carrying B's SHA-256 → 403,
       nothing stored;
  (e) informational: copy_object of (b), head_meta of the copy;
  (f) delete every key of the run (and confirm each is gone).

Output: ONE JSON line (statuses, error codes, booleans) — never a key, a URL, a
signature or a response body. Exit 0 when (a)–(d') hold, 1 when one fails, 2 when
inconclusive (a transport error, or a refusal before any request).

G0 counts step (a) only when the honest step (b) passed: a refusal of every request
proves nothing about the checksum (the 2026-10-29 run refused (a) and (b) alike).

``--variants`` runs G0b instead (owner decision after G0 failed): a matrix of ways to
bind the SHA-256 to a presigned PUT, each with an honest PUT, a wrong-SHA body, a
missing checksum, a forged checksum and a HEAD with ChecksumMode=ENABLED, plus two
informational probes. A variant PASSES only when the honest PUT is 2xx, a wrong-SHA
body is not stored, a missing checksum is 403, a forged checksum is not stored and
HEAD returns the signed SHA-256. For every non-2xx answer R2's <Code> and its
<Message> (scrubbed of anything key-, URL- or signature-like) are recorded. Exit 0
when at least one variant passes, 1 when none does, 2 when inconclusive.
``--variants V1,V2`` limits the matrix.

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

PLAN = ("G0 plan (the shipped presign_put): (a) PUT A under a URL signed for B's SHA-256 must not be 2xx; "
        "(b) PUT A signed for A "
        "must be 2xx; (c) HEAD returns 1 MiB and A's SHA-256; (d) PUT without the checksum header must be "
        "403; (d') PUT with B's SHA-256 in the header must be 403; (e) CopyObject + HEAD (informational); "
        "(f) delete every key under train/gate/<uuid>/. Pass --run to execute.")


PLAN_B = ("G0b plan: per variant an honest PUT (must be 2xx), a wrong-SHA body (must not be stored), a "
          "missing checksum (must be 403), a forged checksum (must not be stored) and a HEAD with "
          "ChecksumMode=ENABLED (must return the signed SHA-256); two informational probes; every key under "
          "train/gate/<uuid>/ deleted. Pass --run to execute.")


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


_MESSAGE_RE = re.compile(r"<Message>(.{0,600}?)</Message>", re.S)
_HEXRUN_RE = re.compile(r"[0-9a-fA-F]{16,}")
_HEADER_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*[.,:;]?$")


def _error_code(body: bytes) -> str | None:
    m = _CODE_RE.search(body[:4096].decode("utf-8", "replace"))
    return m.group(1) if m else None


def scrub_message(text: str | None) -> str | None:
    """R2's <Message>, with anything that could be a key, a URL or a signature removed:
    a token containing '://', 'X-Amz-' or 'Signature', any hex run of 16+, and any
    token of 20+ characters that is not a lower-case hyphenated word (a header name)."""
    if text is None:
        return None
    out = []
    for tok in str(text).replace("\n", " ").split():
        low = tok.lower()
        if "://" in tok or "x-amz-signature" in low or "x-amz-credential" in low or "signature=" in low:
            out.append("<redacted>")
        elif len(tok) >= 20 and not _HEADER_NAME_RE.match(tok):
            out.append("<redacted>")
        else:
            out.append(_HEXRUN_RE.sub("<redacted>", tok))
    return " ".join(out)[:300]


def _error_message(body: bytes) -> str | None:
    m = _MESSAGE_RE.search(body[:8192].decode("utf-8", "replace"))
    return scrub_message(m.group(1).strip()) if m else None


class Inconclusive(Exception):
    """A transport error or an unexpected API failure: the gate cannot judge."""


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
    sha_a, sha_b = hashlib.sha256(a).hexdigest(), hashlib.sha256(b).hexdigest()
    sha_b_b64 = base64.b64encode(hashlib.sha256(b).digest()).decode("ascii")
    keys = {s: f"{prefix}{s}" for s in ("a", "b", "d", "d2", "e")}
    written = list(keys.values())
    out: dict = {"gate": "G0", "bytes": BODY_BYTES}
    code = 0
    try:
        # (a) the signature is valid (exactly the returned headers), only the SHA-256 is wrong
        url, hdr = r2_client.presign_put(keys["a"], len(a), sha_b)
        out["a"] = _put_like_worker(http_client, url, dict(hdr), a)
        out["a"].update(stored=_head(r2_client, keys["a"])["stored"])
        out["a"]["ok"] = not (200 <= out["a"]["status"] < 300) and not out["a"]["stored"]

        # (b) everything right → stored
        url, hdr = r2_client.presign_put(keys["b"], len(a), sha_a)
        out["b"] = _put_like_worker(http_client, url, dict(hdr), a)
        out["b"]["ok"] = 200 <= out["b"]["status"] < 300
        # (a) counts only when the honest PUT passed: refusing everything proves nothing
        out["a"]["ok"] = out["a"]["ok"] and out["b"]["ok"]

        # (c) the stored checksum is returned
        h = _head(r2_client, keys["b"])
        out["c"] = {"stored": h["stored"], "size_ok": h["size"] == BODY_BYTES,
                    "sha256_present": h["sha256"] is not None, "sha256_ok": h["sha256"] == sha_a}
        out["c"]["ok"] = h["stored"] and out["c"]["size_ok"] and out["c"]["sha256_ok"]

        # (d) the signed header omitted → signature mismatch
        url, hdr = r2_client.presign_put(keys["d"], len(a), sha_a)
        stripped = {k: v for k, v in dict(hdr).items() if k.lower() != "x-amz-checksum-sha256"}
        out["d"] = _put_like_worker(http_client, url, stripped, a)
        out["d"].update(stored=_head(r2_client, keys["d"])["stored"])
        out["d"]["ok"] = out["d"]["status"] == 403 and not out["d"]["stored"]

        # (d') the header present but carrying B's SHA-256 (what a hostile host would send)
        url, hdr = r2_client.presign_put(keys["d2"], len(a), sha_a)
        forged = dict(hdr)
        forged["x-amz-checksum-sha256"] = sha_b_b64
        out["d2"] = _put_like_worker(http_client, url, forged, a)
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


# ---------------------------------------------------------------------------
# G0b: the variant matrix
# ---------------------------------------------------------------------------
# mode "header": x-amz-checksum-sha256 is a signed header; "query": it is a signed
# query parameter of the URL. algo "manual" signs x-amz-sdk-checksum-algorithm: SHA256
# through a before-sign hook; "botocore" passes ChecksumAlgorithm='SHA256' and lets
# botocore add what it adds.
VARIANTS = {
    "V0": {"md5": True, "mode": "header", "algo": None},      # G0's design (control)
    "V1": {"md5": False, "mode": "header", "algo": None},
    "V2": {"md5": False, "mode": "header", "algo": "manual"},
    "V3": {"md5": True, "mode": "header", "algo": "manual"},
    "V4a": {"md5": True, "mode": "header", "algo": "botocore"},
    "V4b": {"md5": False, "mode": "header", "algo": "botocore"},
    "V6": {"md5": False, "mode": "query", "algo": None},
    "V6m": {"md5": True, "mode": "query", "algo": None},
}
# the informational probes: no checksum signed (MD5 signed as before 052, or nothing)
INFO_SPECS = {"md5": {"md5": True, "mode": "none", "algo": None},
              "plain": {"md5": False, "mode": "none", "algo": None}}
CHECKSUM_QUERY = "x-amz-checksum-sha256"


def presign_variant(r2_client, key: str, size: int, md5_b64: str, sha256_hex: str, spec: dict):
    """(url, headers) for one variant: the headers are exactly what botocore signed."""
    from urllib.parse import quote

    c = r2_client._client()
    b64 = r2_client.sha256_b64(sha256_hex)
    params = {"Bucket": r2_client.bucket(), "Key": key, "ContentType": "application/octet-stream",
              "ContentLength": int(size)}
    if spec["md5"]:
        params["ContentMD5"] = md5_b64
    if spec["mode"] == "header":
        params["ChecksumSHA256"] = b64
        if spec["algo"] == "botocore":
            params["ChecksumAlgorithm"] = "SHA256"
    seen: dict = {}

    def add(request, **kw):
        if spec["mode"] == "header" and spec["algo"] == "manual":
            request.headers["x-amz-sdk-checksum-algorithm"] = "SHA256"
        if spec["mode"] == "query":
            sep = "&" if "?" in request.url else "?"
            request.url += f"{sep}{CHECKSUM_QUERY}={quote(b64, safe='')}"

    def capture(request, **kw):
        for k, v in request.headers.items():
            seen[k] = v.decode() if isinstance(v, bytes) else v

    event = "before-sign.s3.PutObject"
    c.meta.events.register_first(event, add, unique_id="g0b-add")
    c.meta.events.register_last(event, capture, unique_id="g0b-capture")
    try:
        url = c.generate_presigned_url("put_object", Params=params, ExpiresIn=r2_client.PUT_TTL_S)
    finally:
        c.meta.events.unregister(event, unique_id="g0b-add")
        c.meta.events.unregister(event, unique_id="g0b-capture")
    headers = {k: v for k, v in seen.items() if k.lower().startswith(("content-", "x-amz-"))}
    return url, headers


def _with_query(url: str, value: str | None) -> str:
    """The URL with the checksum query parameter removed (None) or replaced."""
    from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

    parts = urlsplit(url)
    pairs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != CHECKSUM_QUERY]
    if value is not None:
        pairs.append((CHECKSUM_QUERY, value))
    return urlunsplit(parts._replace(query=urlencode(pairs, quote_via=quote, safe="")))


def _chunks(body: bytes):
    """The worker's body form: a generator of 1 MiB chunks with an explicit Content-Length."""
    for i in range(0, len(body), 1 << 20):
        yield body[i:i + (1 << 20)]


def _put_like_worker(client, url, headers, body):
    import httpx

    try:
        resp = client.put(url, content=_chunks(body), headers=dict(headers))
    except (httpx.HTTPError, OSError) as exc:
        raise Inconclusive(f"PUT {type(exc).__name__}") from None
    out = {"status": resp.status_code, "code": _error_code(resp.content)}
    if not 200 <= resp.status_code < 300:
        out["message"] = _error_message(resp.content)
    return out


def _head_fields(r2_client, key: str) -> list[str] | None:
    try:
        resp = r2_client._client().head_object(Bucket=r2_client.bucket(), Key=key, ChecksumMode="ENABLED")
    except Exception:  # informational only
        return None
    return sorted(k for k in resp if k.startswith("Checksum"))


def _probe_variant(r2_client, http_client, name, spec, keys, a, b) -> dict:
    md5_a = base64.b64encode(hashlib.md5(a).digest()).decode("ascii")
    sha_a, sha_b = hashlib.sha256(a).hexdigest(), hashlib.sha256(b).hexdigest()
    b64_b = r2_client.sha256_b64(sha_b)
    res: dict = {}
    # honest
    url, hdr = presign_variant(r2_client, keys["honest"], len(a), md5_a, sha_a, spec)
    res["signed"] = sorted(k.lower() for k in hdr) + (["?" + CHECKSUM_QUERY] if spec["mode"] == "query" else [])
    res["honest"] = _put_like_worker(http_client, url, hdr, a)
    h = _head(r2_client, keys["honest"])
    res["honest"].update(stored=h["stored"], head_sha256_ok=h["sha256"] == sha_a,
                         head_sha256_present=h["sha256"] is not None,
                         head_fields=_head_fields(r2_client, keys["honest"]) if h["stored"] else None)
    # a body whose SHA-256 is not the signed one (signed for B, body A, exactly the returned headers)
    url, hdr = presign_variant(r2_client, keys["wrong"], len(a), md5_a, sha_b, spec)
    res["wrong"] = _put_like_worker(http_client, url, hdr, a)
    res["wrong"]["stored"] = _head(r2_client, keys["wrong"])["stored"]
    # the checksum missing
    url, hdr = presign_variant(r2_client, keys["missing"], len(a), md5_a, sha_a, spec)
    if spec["mode"] == "query":
        url = _with_query(url, None)
    else:
        hdr = {k: v for k, v in hdr.items() if k.lower() != "x-amz-checksum-sha256"}
    res["missing"] = _put_like_worker(http_client, url, hdr, a)
    res["missing"]["stored"] = _head(r2_client, keys["missing"])["stored"]
    # the checksum forged (B's SHA-256 instead of the signed A)
    url, hdr = presign_variant(r2_client, keys["forged"], len(a), md5_a, sha_a, spec)
    if spec["mode"] == "query":
        url = _with_query(url, b64_b)
    else:
        hdr = {k: (b64_b if k.lower() == "x-amz-checksum-sha256" else v) for k, v in hdr.items()}
    res["forged"] = _put_like_worker(http_client, url, hdr, a)
    res["forged"]["stored"] = _head(r2_client, keys["forged"])["stored"]
    res["pass"] = (200 <= res["honest"]["status"] < 300 and res["honest"]["stored"]
                   and res["honest"]["head_sha256_ok"]
                   and not res["wrong"]["stored"]
                   and res["missing"]["status"] == 403 and not res["missing"]["stored"]
                   and not res["forged"]["stored"])
    return res


def _probe_info(r2_client, http_client, keys, a, b) -> dict:
    """V5: the pre-052 URL (MD5 signed, no checksum) plus an UNSIGNED checksum header —
    does R2 accept an unsigned checksum at all? V7: the pre-052 URL, no checksum —
    does R2 store a checksum of its own?"""
    md5_a = base64.b64encode(hashlib.md5(a).digest()).decode("ascii")
    b64_a = r2_client.sha256_b64(hashlib.sha256(a).hexdigest())
    b64_b = r2_client.sha256_b64(hashlib.sha256(b).hexdigest())
    info: dict = {}
    sha_a = hashlib.sha256(a).hexdigest()
    url, hdr = presign_variant(r2_client, keys["v5_honest"], len(a), md5_a, sha_a, INFO_SPECS["md5"])
    info["V5_honest"] = _put_like_worker(http_client, url, dict(hdr, **{"x-amz-checksum-sha256": b64_a}), a)
    hh = _head(r2_client, keys["v5_honest"])
    info["V5_honest"].update(stored=hh["stored"], head_sha256_ok=hh["sha256"] == sha_a)
    url, hdr = presign_variant(r2_client, keys["v5_wrong"], len(a), md5_a, sha_a, INFO_SPECS["md5"])
    info["V5_wrong"] = _put_like_worker(http_client, url, dict(hdr, **{"x-amz-checksum-sha256": b64_b}), a)
    info["V5_wrong"]["stored"] = _head(r2_client, keys["v5_wrong"])["stored"]
    url, hdr = presign_variant(r2_client, keys["v7"], len(a), md5_a, sha_a, INFO_SPECS["md5"])
    info["V7_plain"] = _put_like_worker(http_client, url, hdr, a)
    hv = _head(r2_client, keys["v7"])
    info["V7_plain"].update(stored=hv["stored"], head_sha256_ok=hv["sha256"] == hashlib.sha256(a).hexdigest(),
                            head_fields=_head_fields(r2_client, keys["v7"]) if hv["stored"] else None)
    return info


def run_variants(r2_client, http_client, names=None, run_id: str | None = None, rand=os.urandom,
                 with_info: bool = True) -> tuple[int, dict]:
    """G0b. Returns (exit code, the JSON-able result)."""
    run_id = run_id or uuid.uuid4().hex
    prefix = f"{KEY_ROOT}{run_id}/"
    names = list(names or VARIANTS)
    a, b = rand(BODY_BYTES), rand(BODY_BYTES)
    out: dict = {"gate": "G0b", "bytes": BODY_BYTES, "variants": {}}
    written: list[str] = []
    inconclusive = False
    try:
        for name in names:
            keys = {s: f"{prefix}{name}/{s}" for s in ("honest", "wrong", "missing", "forged")}
            written.extend(keys.values())
            try:
                out["variants"][name] = _probe_variant(r2_client, http_client, name, VARIANTS[name], keys, a, b)
            except Inconclusive as exc:
                out["variants"][name] = {"inconclusive": str(exc), "pass": False}
                inconclusive = True
            except Exception as exc:  # never print a message (it may carry a URL)
                out["variants"][name] = {"inconclusive": type(exc).__name__, "pass": False}
                inconclusive = True
        if with_info:
            keys = {s: f"{prefix}info/{s}" for s in ("v5_honest", "v5_wrong", "v7")}
            written.extend(keys.values())
            try:
                out["info"] = _probe_info(r2_client, http_client, keys, a, b)
            except Exception as exc:  # informational only
                out["info"] = {"error": type(exc).__name__}
    finally:
        out["f"] = _cleanup(r2_client, written)
    out["passing"] = [n for n in names if out["variants"].get(n, {}).get("pass")]
    if not out["f"]["ok"]:
        code = 2
    elif out["passing"]:
        code = 0
    else:
        code = 2 if inconclusive else 1
    out["verdict"] = {0: "pass", 1: "fail", 2: "inconclusive"}[code]
    return code, out


def _import_r2_client():
    api_root = Path(__file__).resolve().parents[2] / "cloud_training_api"
    if str(api_root) not in sys.path:
        sys.path.insert(0, str(api_root))
    from app.services import r2_client

    return r2_client


def main(argv=None, env=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run", action="store_true", help="make the requests (else print the plan)")
    parser.add_argument("--variants", nargs="?", const="", default=None,
                        help="run G0b, the variant matrix (optionally a comma list such as V1,V2)")
    args = parser.parse_args(argv)
    names = None
    if args.variants is not None:
        names = [n for n in args.variants.split(",") if n] or list(VARIANTS)
        unknown = [n for n in names if n not in VARIANTS]
        if unknown:
            print("G0b refused: unknown variant " + ", ".join(unknown))
            return 2
    env = dict(os.environ if env is None else env)
    refusal = check_config(env)
    if refusal:
        print(refusal)
        return 2
    if not args.run:
        print(PLAN if names is None else PLAN_B + " Variants: " + ", ".join(names) + ".")
        return 2
    r2_client = _import_r2_client()
    adopt_config(env)
    import httpx

    with httpx.Client(follow_redirects=False, timeout=httpx.Timeout(60.0, connect=15.0)) as http_client:
        if names is None:
            code, out = run_gate(r2_client, http_client)
        else:
            code, out = run_variants(r2_client, http_client, names)
    print(json.dumps(out, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
