#!/usr/bin/env bash
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
#
# Refuse a production deploy that would roll production back (2026-11-15).
#
# W2 (cloud API) and W3 (teacher web) deploy on a push to main AND inside a
# tagged release, in two concurrency lanes keyed by the ref, so neither waits
# for the other. A release's deploy that waited for its approval (or for the
# trainer image) while a later merge to main went live would then deploy the
# OLDER tag commit over it, and its health gate would still pass (it compares
# against its own commit). This runs in the deploy job, after the approval and
# right before `railway up`, and allows the deploy only when the commit about to
# go live CONTAINS the one production serves.
#
# Usage: refuse_rollback.sh <served-sha> <deploy-sha> <allow-rollback>
#   served-sha      the commit production answers now (W2: /health `commit`,
#                   W3: /version.json `buildId`); empty or not a full SHA when
#                   it could not be read
#   deploy-sha      the commit this run deploys (github.sha)
#   allow-rollback  "true" only from a hand dispatch whose input says so: a
#                   deliberate rollback (DEPLOY.md, the rollback order)
#
# Runs in a checkout with the full history (actions/checkout fetch-depth 0):
# it never fetches, so it needs no credentials. Exit 0 = deploy, 1 = refused.
set -euo pipefail

served="${1:-}"
deploy="${2:?the deploy commit is required}"
allow="${3:-false}"

if ! [[ "$served" =~ ^[0-9a-f]{40}$ ]]; then
  # Production down, an old build without a commit, a first deploy: nothing to
  # compare with, and refusing would block exactly the fix such a state needs.
  echo "::warning::Production's commit could not be read; the rollback check is skipped."
  exit 0
fi
if [ "$served" = "$deploy" ]; then
  echo "Production already serves ${deploy}: a redeploy of the same commit."
  exit 0
fi
if git cat-file -e "${served}^{commit}" 2>/dev/null \
    && git merge-base --is-ancestor "$served" "$deploy"; then
  echo "${deploy} contains production's ${served}: deploying forward."
  exit 0
fi
if [ "$allow" = "true" ]; then
  echo "::warning::Deploying ${deploy} although production serves ${served}, which it does not contain (allow_rollback=true: a deliberate rollback)."
  exit 0
fi
echo "::error::Production serves ${served}, which ${deploy} does not contain. Deploying would roll production back (an older release, or a commit from another branch), so it is refused. To roll back on purpose, run this workflow by hand with allow_rollback=true."
exit 1
