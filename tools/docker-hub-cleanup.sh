#!/usr/bin/env bash
# tools/docker-hub-cleanup.sh
# ----------------------------------------------------------------------
# Deletes junk repos and dirty tags from the `nettername` Docker Hub
# namespace. Default mode is --dry-run; pass --execute to actually
# delete. Used by `.github/workflows/docker-publish.yml` cleanup job.
#
# Junk repos targeted for FULL delete:
#   nettername/edubotics            (0 pulls, garbage)
#   nettername/edubotics-probe      (25 pulls, debug)
#   nettername/pushtest             (14 pulls, smoke test)
#   nettername/physical-ai-server-base  (43 pulls, unused base —
#                                        Dockerfiles no longer FROM it)
#   nettername/robotis-ai-training  (stale — Modal owns this now per
#                                    build-images.sh:395-397)
#
# Dirty tags swept on production repos (these match `*-dirty$` glob):
#   - nettername/open-manipulator:*-dirty
#   - nettername/physical-ai-server:*-dirty
#   - nettername/physical-ai-manager:*-dirty
#
# Auth: requires DOCKERHUB_USERNAME + DOCKERHUB_TOKEN env vars. The
# token must have repo:delete scope.
#
# Usage:
#   ./docker-hub-cleanup.sh                  # dry-run (default)
#   ./docker-hub-cleanup.sh --execute        # actually delete
#   ./docker-hub-cleanup.sh --only-dirty     # only sweep *-dirty tags
#   ./docker-hub-cleanup.sh --only-junk      # only delete junk repos
#
set -euo pipefail

NAMESPACE="nettername"
JUNK_REPOS=(
  edubotics
  edubotics-probe
  pushtest
  physical-ai-server-base
  robotis-ai-training
)
PROD_REPOS=(
  open-manipulator
  physical-ai-server
  physical-ai-manager
  open-manipulator-jetson
  physical-ai-server-jetson
)

# ── Argument parsing ──
DRY_RUN=true
SCOPE="all"  # all | dirty | junk
for arg in "$@"; do
  case "$arg" in
    --execute) DRY_RUN=false ;;
    --dry-run) DRY_RUN=true ;;
    --only-dirty) SCOPE="dirty" ;;
    --only-junk) SCOPE="junk" ;;
    -h|--help)
      sed -n '2,30p' "$0"
      exit 0
      ;;
    *)
      echo "Unknown argument: $arg" >&2
      exit 2
      ;;
  esac
done

# ── Auth helpers ──
: "${DOCKERHUB_USERNAME:?DOCKERHUB_USERNAME env var is required}"
: "${DOCKERHUB_TOKEN:?DOCKERHUB_TOKEN env var is required}"

API_TOKEN=""

_login() {
  # Exchange the username+PAT for a Hub Bearer token. Docker Hub's
  # delete endpoint refuses PATs in the Authorization header directly
  # — it wants the Bearer issued by /v2/users/login/.
  local body resp http_code
  body=$(printf '{"username":"%s","password":"%s"}' "$DOCKERHUB_USERNAME" "$DOCKERHUB_TOKEN")
  resp=$(curl -s -w "\n%{http_code}" -H "Content-Type: application/json" \
              -d "$body" \
              "https://hub.docker.com/v2/users/login/")
  http_code=$(echo "$resp" | tail -n1)
  if [ "$http_code" != "200" ]; then
    echo "::error::Docker Hub login returned HTTP $http_code. Check DOCKERHUB_USERNAME / DOCKERHUB_TOKEN."
    exit 1
  fi
  API_TOKEN=$(echo "$resp" | head -n-1 | python3 -c "import sys,json; print(json.load(sys.stdin).get('token',''))")
  if [ -z "$API_TOKEN" ]; then
    echo "::error::Docker Hub login returned 200 but no token in response."
    exit 1
  fi
}

_delete_repo() {
  local repo="$1"
  local url="https://hub.docker.com/v2/repositories/${NAMESPACE}/${repo}/"
  if $DRY_RUN; then
    echo "DRY-RUN: would DELETE $url"
    return 0
  fi
  CODE=$(curl -s -o /dev/null -w "%{http_code}" \
         -X DELETE \
         -H "Authorization: JWT ${API_TOKEN}" \
         "$url")
  if [ "$CODE" = "204" ] || [ "$CODE" = "202" ]; then
    echo "OK: deleted $NAMESPACE/$repo (HTTP $CODE)"
  else
    echo "WARN: delete $NAMESPACE/$repo returned HTTP $CODE"
  fi
}

_list_tags() {
  local repo="$1"
  # Pagination: this returns up to 100. None of our repos has more dirty
  # tags than that, but if they do you'll see truncation.
  curl -s "https://hub.docker.com/v2/repositories/${NAMESPACE}/${repo}/tags/?page_size=100" \
    | python3 -c "import sys,json; d=json.load(sys.stdin); print('\n'.join(t['name'] for t in d.get('results', [])))"
}

_delete_tag() {
  local repo="$1" tag="$2"
  local url="https://hub.docker.com/v2/repositories/${NAMESPACE}/${repo}/tags/${tag}/"
  if $DRY_RUN; then
    echo "DRY-RUN: would DELETE $url"
    return 0
  fi
  CODE=$(curl -s -o /dev/null -w "%{http_code}" \
         -X DELETE \
         -H "Authorization: JWT ${API_TOKEN}" \
         "$url")
  if [ "$CODE" = "204" ] || [ "$CODE" = "202" ]; then
    echo "OK: deleted $NAMESPACE/$repo:$tag (HTTP $CODE)"
  else
    echo "WARN: delete $NAMESPACE/$repo:$tag returned HTTP $CODE"
  fi
}

# ── Main ──
echo ">> Docker Hub cleanup — namespace=$NAMESPACE dry_run=$DRY_RUN scope=$SCOPE"
_login
echo "Logged in to Docker Hub."

if [ "$SCOPE" = "all" ] || [ "$SCOPE" = "junk" ]; then
  echo ""
  echo ">> Deleting junk repos:"
  for repo in "${JUNK_REPOS[@]}"; do
    _delete_repo "$repo"
  done
fi

if [ "$SCOPE" = "all" ] || [ "$SCOPE" = "dirty" ]; then
  echo ""
  echo ">> Sweeping *-dirty tags from production repos:"
  for repo in "${PROD_REPOS[@]}"; do
    echo "  -- $NAMESPACE/$repo --"
    # Process substitution preserves `set -e` semantics in the loop body.
    # `|| true` on grep handles "no matching tags" without aborting the script.
    while read -r tag; do
      [ -z "$tag" ] && continue
      _delete_tag "$repo" "$tag"
    done < <(_list_tags "$repo" | grep -E '\-dirty$' || true)
  done
fi

echo ""
if $DRY_RUN; then
  echo ">> DRY-RUN complete. Re-run with --execute to actually delete."
else
  echo ">> Cleanup complete."
fi
