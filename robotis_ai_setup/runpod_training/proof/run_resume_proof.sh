#!/bin/sh
# R3 resume proof (spec §5 R3, §6 WP-B) in the CPU proof image, offline (--network none).
#
#   robotis_ai_setup/runpod_training/proof/run_resume_proof.sh
#   PROOF_IMAGE=<an already built Dockerfile.proof image> PROOF_WORK=<dir> .../run_resume_proof.sh
#
# Builds Dockerfile.proof unless PROOF_IMAGE is set, makes the synthetic dataset, runs
# A1, A2, A(num_workers 2), A(save_freq N), B (SIGKILL after N/2, resumed through the
# product's own staging), E (N 30) and EB (resume at the epoch boundary, num_workers 0
# and 2), prints ONE JSON verdict and exits 0 iff it passes.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../../.." && pwd)
IMAGE=${PROOF_IMAGE:-edubotics-trainer-proof:local}
if [ -z "${PROOF_IMAGE:-}" ]; then
  docker build -q -f "$HERE/Dockerfile.proof" -t "$IMAGE" "$ROOT" >/dev/null
fi
WORK=${PROOF_WORK:-$(mktemp -d)}
mkdir -p "$WORK"
rc=0
docker run --rm --network none -e HF_HUB_OFFLINE=1 -v "$WORK:/work" --entrypoint python "$IMAGE" \
  /app/proof/resume_proof.py resume --work /work > "$WORK/verdict.json" || rc=$?
tail -n 1 "$WORK/verdict.json"
echo "run_resume_proof: exit $rc (logs and runs in $WORK)" >&2
exit "$rc"
