#!/bin/sh
# E17 proof (spec §5 R1, §B.7) in the CPU proof image: LeRobot 0.5.1 with
# --dataset.revision=<commit sha>, against the public lerobot/pusht at its v3.0 commit.
# Network: the public Hub only, no token (no HF_TOKEN is passed into any container).
#
#   robotis_ai_setup/runpod_training/proof/revision_proof.sh
#   PROOF_IMAGE=<an already built Dockerfile.proof image> PROOF_WORK=<dir> .../revision_proof.sh
#
# Phases, each in its OWN container with its own Hugging Face cache:
#   versions  is_valid_version on real shas, a PEP 440-shaped sha and "v3.0"
#   fresh     20 steps pinned to the commit; snapshot named by the sha; train_config pins it;
#             the product's regenerated config equals LeRobot's own (000010, 000020)
#   resume    an EMPTY cache: the product's staging resumes from 000010 and completes to 20,
#             the same commit is downloaded again, the config equal after the 4 resume keys
#   negative  the v2.1 tag's commit is refused with LeRobot's BackwardCompatibilityError
# Prints ONE JSON verdict and exits 0 iff every phase holds.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../../.." && pwd)
IMAGE=${PROOF_IMAGE:-edubotics-trainer-proof:local}
if [ -z "${PROOF_IMAGE:-}" ]; then
  docker build -q -f "$HERE/Dockerfile.proof" -t "$IMAGE" "$ROOT" >/dev/null
fi
WORK=${PROOF_WORK:-$(mktemp -d)}
mkdir -p "$WORK"
phase() {
  mkdir -p "$WORK/hf/$1"
  docker run --rm -e HF_HOME=/hf -e HF_HUB_DISABLE_TELEMETRY=1 -v "$WORK:/work" -v "$WORK/hf/$1:/hf" \
    --entrypoint python "$IMAGE" /app/proof/resume_proof.py "revision-$1" --work /work \
    >> "$WORK/revision.log" 2>&1 || true
}
phase versions
phase fresh
phase resume
phase negative
rc=0
docker run --rm --network none -v "$WORK:/work" --entrypoint python "$IMAGE" \
  /app/proof/resume_proof.py revision-verdict --work /work > "$WORK/verdict.json" || rc=$?
tail -n 1 "$WORK/verdict.json"
echo "revision_proof: exit $rc (logs in $WORK)" >&2
exit "$rc"
