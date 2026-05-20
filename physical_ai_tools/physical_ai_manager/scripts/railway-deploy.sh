#!/bin/bash
# railway-deploy.sh — Deploy physical_ai_manager (teacher-web) to Railway.
#
# Why this exists:
#   `railway up` from physical_ai_manager/ walks up to the .git root and
#   uploads the whole monorepo, which buries Dockerfile.web nested under
#   physical_ai_manager/Dockerfile.web — Railway then can't find the
#   Dockerfile referenced by railway.json and falls back to Railpack
#   auto-detect, which doesn't know our build. Using `--path-as-root .`
#   fixes that.
#
#   Separately, the React app's prebuild Jest hook
#   (src/components/Workshop/blocks/__tests__/objectClasses.sync.test.js)
#   reads coco_classes.py from a sibling repo path that isn't in the
#   Docker build context. build-images.sh stages it locally as
#   _coco_classes.py before `docker build`; this script does the same
#   for Railway.
#
# Run from anywhere — paths are resolved from the script's own location.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MANAGER_DIR="$(dirname "$SCRIPT_DIR")"
PHYSICAL_AI_TOOLS_DIR="$(dirname "$MANAGER_DIR")"
SERVER_COCO="${PHYSICAL_AI_TOOLS_DIR}/physical_ai_server/physical_ai_server/workflow/coco_classes.py"
STAGED_COCO="${MANAGER_DIR}/_coco_classes.py"

if [ ! -f "${SERVER_COCO}" ]; then
    echo "ERROR: coco_classes.py not found at ${SERVER_COCO}"
    echo "       Expected the sibling-repo layout: physical_ai_server/ next"
    echo "       to physical_ai_manager/. Check your clone."
    exit 1
fi

# Stage. Cleanup on exit guarantees the file doesn't leak into the
# source tree (it's not gitignored — the developer should never have
# this checked in).
cp "${SERVER_COCO}" "${STAGED_COCO}"
cleanup() { rm -f "${STAGED_COCO}"; }
trap cleanup EXIT

cd "${MANAGER_DIR}"

# Default to the production environment + teacher-web service. Override
# via TEACHER_WEB_SERVICE / TEACHER_WEB_ENV if you ever rename.
SERVICE="${TEACHER_WEB_SERVICE:-teacher-web}"
ENVIRONMENT="${TEACHER_WEB_ENV:-production}"

# Stamp REACT_APP_BUILD_ID as a Railway service variable BEFORE `railway up`
# so railway.json's `buildArgs: {"REACT_APP_BUILD_ID": "$REACT_APP_BUILD_ID"}`
# interpolation has the current SHA to substitute. Without this, the
# Dockerfile.web `ARG REACT_APP_BUILD_ID=dev` default wins and /version.json
# serves "dev" — Railway's `$RAILWAY_GIT_COMMIT_SHA` only populates on the
# native git-push integration, NOT on `railway up --ci` source uploads
# (which is how GHA invokes this script).
#
# `--skip-deploys` prevents the variable change from triggering its own
# deploy; the `railway up` two lines below picks up the new value
# atomically.
if [ -n "${GITHUB_SHA:-}" ]; then
    echo "  stamping REACT_APP_BUILD_ID=${GITHUB_SHA:0:7} (full=${GITHUB_SHA})"
    railway variable set "REACT_APP_BUILD_ID=${GITHUB_SHA}" \
        --service "${SERVICE}" --environment "${ENVIRONMENT}" \
        --skip-deploys >/dev/null 2>&1 || \
        echo "  warning: failed to stamp REACT_APP_BUILD_ID — build may use 'dev'"
fi

echo "========================================"
echo "Deploying physical_ai_manager → Railway"
echo "  service:     ${SERVICE}"
echo "  environment: ${ENVIRONMENT}"
echo "  context:     ${MANAGER_DIR}"
echo "  staged coco_classes.py? yes"
echo "========================================"

# --path-as-root . forces Railway to treat physical_ai_manager/ as the
# upload root, so Dockerfile.web and railway.json are at the top level
# of the snapshot. Without it the upload prefixes everything with
# physical_ai_tools/physical_ai_manager/ and the Dockerfile path
# resolution silently breaks.
exec railway up . --path-as-root --ci \
    --service "${SERVICE}" \
    --environment "${ENVIRONMENT}" \
    "$@"
