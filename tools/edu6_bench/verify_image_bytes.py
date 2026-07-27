"""Step-1 verification: prove the PUBLISHED image bytes equal the repo bytes.

Pulls only the small layers from the registry (no multi-GB docker pull), finds
the layer that carries each target file, takes the LAST writer, and compares
sha256 against `git show <rev>:<path>` (never the Windows worktree — that file
may be CRLF; see the bringup doc's CRLF trap).

Two independent checks, and only one of them is about bytes:
  * the TARGET FILES must be byte-identical to the compare revision — the
    authoritative gate, and always fatal on a mismatch;
  * the image's revision LABEL is JUDGED, not merely compared (see
    judge_revision) — an older label is fine when nothing the image is built
    from has changed since, and fatal when something has.

Exit 0 means "proceed with powered work"; the final VERDICT line says so in
words, because a gate whose output has to be interpreted is a gate that gets
ignored.
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

# The git revision the image is expected to have been built from.
#
# This used to be a HARDCODED sha, which is a trap: after the next push the tool
# compares a correct image against a stale baseline and reports MISMATCH on
# bytes that are in fact right. That happened on 2026-07-26 — the image carried
# exactly the pushed hashes and the tool still failed, because REV still named
# the previous commit. A stale baseline in a verification tool is worse than no
# tool, because it trains you to ignore its output.
#
# Default is now the current origin/main, resolved at run time. Pass an explicit
# ref as argv[1] to check an older image.
def _default_rev() -> str:
    for ref in ("origin/main", "HEAD"):
        try:
            return subprocess.run(
                ["git", "-C", REPO_DIR, "rev-parse", ref],
                capture_output=True, text=True, check=True).stdout.strip()
        except Exception:  # noqa: BLE001 - fall through to the next candidate
            continue
    raise SystemExit("[FAIL] cannot resolve a git revision to compare against")


REV = sys.argv[1] if len(sys.argv) > 1 else _default_rev()

TARGETS = {
    "open-manipulator": {
        "usr/local/bin/edu6_arm_node.py":
            "robotis_ai_setup/docker/open_manipulator/edu6_arm_node.py",
        "usr/local/bin/feetech_bus.py":
            "robotis_ai_setup/docker/open_manipulator/feetech_bus.py",
    },
}

# Images whose bytes CANNOT be verified this cheaply, so only their revision is
# judged. Both have a principled reason, not an oversight:
#   * physical-ai-server is FLATTENED (`docker export | import`, the SLIM_CUDA
#     path) into ONE ~5 GB layer, so there is no small layer to walk;
#   * physical-ai-manager ships a built JS bundle, which has no source file to
#     compare bytes against by construction.
# A revision judgement is weaker than a byte comparison and is stated as such,
# but it is far from nothing: it proves the image was built from a commit whose
# image-relevant sources are identical to the compare revision, and CI's
# `image-source-parity` job asserts byte equality per-arch at publish time.
REVISION_ONLY = ("physical-ai-server", "physical-ai-manager")

# Everything each image is BUILT FROM, so a revision-label difference can be
# judged instead of merely reported. Derived from the Dockerfiles and
# build-images.sh, not guessed: every COPY/ADD in
# docker/open_manipulator/Dockerfile takes its source from that directory (the
# build context); the server image is COPY-wholesale from the package tree with
# build-images.sh's documented staging exclusions applied here as git pathspec
# exclusions, so a test-only edit does not mark a correct image stale; and the
# base-image tags pinned in build-images.sh change every resulting image.
IMAGE_SOURCES = {
    "open-manipulator": (
        "robotis_ai_setup/docker/open_manipulator/",
        "robotis_ai_setup/docker/build-images.sh",
    ),
    "physical-ai-server": (
        "physical_ai_tools/physical_ai_server/",
        "physical_ai_tools/physical_ai_interfaces/",
        "robotis_ai_setup/docker/physical_ai_server/",
        "robotis_ai_setup/docker/build-images.sh",
        ":(exclude)physical_ai_tools/physical_ai_server/test/",
        ":(exclude)physical_ai_tools/physical_ai_server/Dockerfile.amd64",
        ":(exclude)physical_ai_tools/physical_ai_server/Dockerfile.arm64",
        ":(exclude)physical_ai_tools/physical_ai_server/Dockerfile.arm64cpu",
        ":(exclude)physical_ai_tools/physical_ai_server/CHANGELOG.rst",
    ),
    "physical-ai-manager": (
        "physical_ai_tools/physical_ai_manager/",
        "robotis_ai_setup/docker/build-images.sh",
        ":(exclude)physical_ai_tools/physical_ai_manager/src/**/*.test.js",
        ":(exclude)physical_ai_tools/physical_ai_manager/src/**/*.test.jsx",
    ),
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


def judge_revision(rev, repo):
    """Decide whether an image built at `rev` is STALE with respect to REV.

    An exact sha equality test is the wrong gate and was itself a trap: images
    are rebuilt only when image-relevant paths change, so every docs-only or
    tools-only push makes a PERFECTLY CURRENT image report MISMATCH. The bringup
    doc's step-0 instruction is "if it does not say MATCH, stop", so that false
    red halts a healthy bench session — the same class of defect as the
    hardcoded baseline fixed in 6cce3da0, one level up.

    What actually matters is whether anything the image is BUILT FROM changed
    between the two revisions. Returns (state, fatal, changed_paths).
    """
    if rev == REV:
        return "MATCH", False, []
    if not rev or rev == "?":
        return "UNKNOWN (image carries no revision label)", True, []
    known = subprocess.run(["git", "cat-file", "-e", f"{rev}^{{commit}}"],
                           cwd=REPO_DIR, capture_output=True)
    if known.returncode != 0:
        # Cannot prove currency either way; do not let a byte-identical driver
        # imply the rest of the image is current.
        return ("UNKNOWN (revision not in local history - try `git fetch`)",
                True, [])
    changed = subprocess.run(
        ["git", "diff", "--name-only", f"{rev}..{REV}", "--",
         *IMAGE_SOURCES.get(repo, ())],
        cwd=REPO_DIR, capture_output=True, text=True, check=True,
    ).stdout.split()
    if changed:
        return "STALE (image-relevant files changed since)", True, changed
    return "older build, image-relevant paths unchanged", False, []


ACCEPT_LIST = ("application/vnd.oci.image.index.v1+json,"
               "application/vnd.docker.distribution.manifest.list.v2+json")
ACCEPT_MAN = ("application/vnd.docker.distribution.manifest.v2+json,"
              "application/vnd.oci.image.manifest.v1+json")

def fetch_manifest(repo, tok):
    """Return (manifest, config) for <repo>:latest, following a manifest list."""
    idx = json.loads(get(f"https://ghcr.io/v2/{NS}/{repo}/manifests/latest",
                         tok, ACCEPT_LIST))
    child = idx["manifests"][0]["digest"] if "manifests" in idx else None
    man = json.loads(get(f"https://ghcr.io/v2/{NS}/{repo}/manifests/{child}",
                         tok, ACCEPT_MAN))
    cfg = json.loads(get(f"https://ghcr.io/v2/{NS}/{repo}/blobs/"
                         f"{man['config']['digest']}", tok))
    return man, cfg


def report_revision(repo, cfg):
    """Print and judge the image's revision label. Returns 1 when fatal."""
    rev = ((cfg.get("config") or {}).get("Labels") or {}).get(
        "org.opencontainers.image.revision", "?")
    state, fatal, changed = judge_revision(rev, repo)
    print(f"  revision label: {rev[:12]}  {state}")
    for path in changed:
        print(f"        changed since build: {path}")
    return 1 if fatal else 0


rc = 0
for repo, files in TARGETS.items():
    print(f"=== {repo}:latest ===")
    tok = token_for(repo)
    man, cfg = fetch_manifest(repo, tok)
    rc |= report_revision(repo, cfg)

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

# The remaining two images carry the Roboter-Studio workflow layer and the SPA
# that the later bench gates (R7, R8, R10) exercise. Their bytes cannot be
# checked cheaply (see REVISION_ONLY), but leaving them out entirely was the
# real gap: "the image is byte-verified" covered the driver's 2 files while the
# same session changed ~8 more that ship in the server image.
for repo in REVISION_ONLY:
    print(f"=== {repo}:latest (revision only) ===")
    tok = token_for(repo)
    _man, cfg = fetch_manifest(repo, tok)
    rc |= report_revision(repo, cfg)

print()
if rc == 0:
    print(f"VERDICT: GREEN vs git {REV[:8]} - the published driver is "
          "byte-identical, and no image-relevant source has changed since any "
          "of the three images was built. Powered work may proceed.")
else:
    print("VERDICT: RED - do NOT start powered work. At least one image is not "
          "provably built from the current source; rebuild/republish it (or "
          "fetch the missing revision) and re-run this check.")
sys.exit(rc)
