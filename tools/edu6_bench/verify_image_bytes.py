"""Step-1 verification: prove the PUBLISHED image bytes equal the repo bytes.

Pulls only the small layers from the registry (no multi-GB docker pull), finds
the layer that carries each target file, takes the LAST writer, and compares
sha256 against `git show <rev>:<path>` (never the Windows worktree — that file
may be CRLF; see the bringup doc's CRLF trap).
"""
import gzip
import hashlib
import io
import json
import subprocess
import sys
import tarfile
import urllib.request

REPO_DIR = r"C:\Users\svend\newaarm\Testre"
NS = "svendanilborodun"
REV = "b8f3c71d67797a22cb4dc815ad276ba0f5b35bac"

TARGETS = {
    "open-manipulator": {
        "usr/local/bin/edu6_arm_node.py":
            "robotis_ai_setup/docker/open_manipulator/edu6_arm_node.py",
        "usr/local/bin/feetech_bus.py":
            "robotis_ai_setup/docker/open_manipulator/feetech_bus.py",
    },
}
MAX_LAYER = 400_000


def get(url, token=None, accept=None):
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if accept:
        req.add_header("Accept", accept)
    with urllib.request.urlopen(req, timeout=90) as r:
        return r.read()


def token_for(repo):
    u = (f"https://ghcr.io/token?scope=repository:{NS}/{repo}:pull"
         f"&service=ghcr.io")
    return json.loads(get(u))["token"]


def git_sha(path):
    out = subprocess.run(["git", "show", f"{REV}:{path}"], cwd=REPO_DIR,
                         capture_output=True, check=True).stdout
    return hashlib.sha256(out).hexdigest(), len(out)


ACCEPT_LIST = ("application/vnd.oci.image.index.v1+json,"
               "application/vnd.docker.distribution.manifest.list.v2+json")
ACCEPT_MAN = ("application/vnd.docker.distribution.manifest.v2+json,"
              "application/vnd.oci.image.manifest.v1+json")

rc = 0
for repo, files in TARGETS.items():
    print(f"=== {repo}:latest ===")
    tok = token_for(repo)
    idx = json.loads(get(f"https://ghcr.io/v2/{NS}/{repo}/manifests/latest",
                         tok, ACCEPT_LIST))
    child = idx["manifests"][0]["digest"] if "manifests" in idx else None
    man = json.loads(get(f"https://ghcr.io/v2/{NS}/{repo}/manifests/{child}",
                         tok, ACCEPT_MAN))
    cfg = json.loads(get(f"https://ghcr.io/v2/{NS}/{repo}/blobs/"
                         f"{man['config']['digest']}", tok))
    rev = ((cfg.get("config") or {}).get("Labels") or {}).get(
        "org.opencontainers.image.revision", "?")
    print(f"  revision label: {rev[:12]}  "
          f"{'MATCH' if rev == REV else 'MISMATCH'}")
    if rev != REV:
        rc = 1

    # walk layers in order; keep the LAST occurrence of each target
    found = {}
    for i, layer in enumerate(man["layers"]):
        if layer["size"] > MAX_LAYER:
            continue
        try:
            blob = get(f"https://ghcr.io/v2/{NS}/{repo}/blobs/{layer['digest']}",
                       tok)
            raw = gzip.decompress(blob) if blob[:2] == b"\x1f\x8b" else blob
            tf = tarfile.open(fileobj=io.BytesIO(raw))
        except Exception:
            continue
        names = set(tf.getnames())
        for member in files:
            if member in names:
                data = tf.extractfile(member).read()
                found[member] = (i, hashlib.sha256(data).hexdigest(), len(data))
    for member, path in files.items():
        want, wlen = git_sha(path)
        if member not in found:
            print(f"  MISSING {member} in any small layer")
            rc = 1
            continue
        lyr, got, glen = found[member]
        ok = got == want
        print(f"  {'OK  ' if ok else 'FAIL'} {member}")
        print(f"        image (layer {lyr}): {got[:32]}… {glen} B")
        print(f"        git {REV[:8]}:      {want[:32]}… {wlen} B")
        if not ok:
            rc = 1
sys.exit(rc)
