#!/usr/bin/env bash
# tools/modal-cleanup.sh
# ----------------------------------------------------------------------
# Removes the legacy / unused Modal entities in the `svendanilborodun`
# workspace. Default is --dry-run; pass --execute to actually delete.
#
# Apps targeted for delete:
#   - example-mcp-*       (stale MCP test apps; not edubotics-* prefixed)
#
# Secrets targeted for delete (verified unused via grep against
# robotis_ai_setup/modal_training/*.py):
#   - stripe              (legacy; no code reference)
#   - supabase            (superseded by edubotics-training-secrets)
#   - hf-token            (superseded by edubotics-training-secrets HF_TOKEN)
#   - huggingface         (superseded; old name)
#
# Volumes targeted for delete (verified not mounted in modal_app.py or
# vision_app.py):
#   - PaliGemma
#   - act
#   - gr00t-n1
#
# NEVER touched (production):
#   - apps:     edubotics-training, edubotics-vision
#   - secrets:  edubotics-training-secrets, edubotics-vision-secrets,
#               mcp-edubotics
#   - volumes:  edubotics-vision-cache
#
# Auth: uses the Modal CLI's already-configured credentials. If you
# need to re-auth, run `modal token set --token-id $ID --token-secret $S`.
#
# Discovery: uses `modal <subcommand> list --json` and python3 JSON
# parsing — NOT regex against the rich-formatted CLI table, because the
# table truncates long names like `example-mcp-server-stateless` to
# `example-mcp-…` and silently drops them from greps. See CLAUDE.md
# "Known issues → Pipeline-level" (2026-05 audit) for the original bug.
#
# Usage:
#   ./modal-cleanup.sh             # dry-run (default)
#   ./modal-cleanup.sh --execute   # actually delete
set -euo pipefail

DRY_RUN=true
for arg in "$@"; do
  case "$arg" in
    --execute) DRY_RUN=false ;;
    --dry-run) DRY_RUN=true ;;
    -h|--help) sed -n '2,42p' "$0"; exit 0 ;;
    *) echo "Unknown argument: $arg" >&2; exit 2 ;;
  esac
done

command -v modal >/dev/null || { echo "modal CLI not installed" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 required for JSON parsing" >&2; exit 1; }

# ── Sanity check: refuse if we're auth'd as the wrong workspace ──
WS=$(modal profile current 2>/dev/null | head -1 || true)
echo "Current Modal workspace: $WS"
if [ "$WS" != "svendanilborodun" ]; then
  echo "WARNING: not authenticated as 'svendanilborodun' (got: $WS)"
  if ! $DRY_RUN; then
    echo "Refusing to run --execute against an unexpected workspace."
    exit 1
  fi
fi

# Counters for the final summary line.
APPS_HANDLED=0
SECRETS_HANDLED=0
VOLS_HANDLED=0

# ── Apps to delete ──
echo ""
echo ">> Apps to delete (Description starts with 'example-mcp'):"

# `modal app list --json` returns an array of objects with keys
# "App ID", "Description", "State", "Tasks", "Created at", "Stopped at".
# We filter on Description prefix and emit "<app_id>\t<description>" pairs.
APPS_JSON=$(modal app list --json 2>/dev/null || true)
if [ -z "$APPS_JSON" ]; then
  echo "::error::modal app list --json returned no output — is the CLI authenticated?" >&2
  exit 1
fi

APPS=$(printf '%s' "$APPS_JSON" | python3 -c '
import json, sys
data = json.load(sys.stdin)
for a in data:
    desc = a.get("Description") or ""
    app_id = a.get("App ID") or ""
    if desc.startswith("example-mcp") and app_id:
        print(f"{app_id}\t{desc}")
' || true)

if [ -z "$APPS" ]; then
  echo "  (none found)"
else
  while IFS=$'\t' read -r app_id desc; do
    [ -z "$app_id" ] && continue
    if $DRY_RUN; then
      # Literal text, NOT a backtick subshell — dry-run must never invoke
      # the command it claims to preview.
      echo "  DRY-RUN: would run 'modal app stop $app_id --yes'  ($desc)"
    else
      echo "  Stopping $app_id ($desc)..."
      # `modal app stop` needs --yes under non-interactive shells (it
      # otherwise aborts on its own confirmation prompt — and the old
      # `|| true` swallowed exactly that abort, 2026-06-07 finding).
      # There is NO `modal app remove` on modal CLI >=1.x: stopped apps
      # linger in `modal app list` history by design.
      modal app stop "$app_id" --yes 2>&1 || echo "    WARN: stop failed"
    fi
    APPS_HANDLED=$((APPS_HANDLED + 1))
  done <<< "$APPS"
fi

# ── Secrets to delete ──
echo ""
echo ">> Legacy secrets to delete:"
LEGACY_SECRETS=("stripe" "supabase" "hf-token" "huggingface")

SECRETS_JSON=$(modal secret list --json 2>/dev/null || true)
if [ -z "$SECRETS_JSON" ]; then
  echo "  WARN: modal secret list --json returned no output; skipping secrets pass"
  SECRETS_JSON='[]'
fi

# Materialise the live secret names once; the per-target loop just greps.
LIVE_SECRETS=$(printf '%s' "$SECRETS_JSON" | python3 -c '
import json, sys
for s in json.load(sys.stdin):
    name = s.get("Name") or ""
    if name:
        print(name)
' || true)

for s in "${LEGACY_SECRETS[@]}"; do
  if printf '%s\n' "$LIVE_SECRETS" | grep -Fxq "$s"; then
    if $DRY_RUN; then
      echo "  DRY-RUN: would run 'modal secret delete $s --yes'"
    else
      echo "  Deleting secret $s..."
      modal secret delete "$s" --yes 2>&1 || echo "    WARN: delete failed"
    fi
    SECRETS_HANDLED=$((SECRETS_HANDLED + 1))
  else
    echo "  $s (not present, skipping)"
  fi
done

# ── Volumes to delete ──
echo ""
echo ">> Legacy volumes to delete:"
LEGACY_VOLS=("PaliGemma" "act" "gr00t-n1")

VOLS_JSON=$(modal volume list --json 2>/dev/null || true)
if [ -z "$VOLS_JSON" ]; then
  echo "  WARN: modal volume list --json returned no output; skipping volumes pass"
  VOLS_JSON='[]'
fi

LIVE_VOLS=$(printf '%s' "$VOLS_JSON" | python3 -c '
import json, sys
for v in json.load(sys.stdin):
    name = v.get("Name") or ""
    if name:
        print(name)
' || true)

for v in "${LEGACY_VOLS[@]}"; do
  if printf '%s\n' "$LIVE_VOLS" | grep -Fxq "$v"; then
    if $DRY_RUN; then
      echo "  DRY-RUN: would run 'modal volume delete $v --yes'"
    else
      echo "  Deleting volume $v..."
      modal volume delete "$v" --yes 2>&1 || echo "    WARN: delete failed"
    fi
    VOLS_HANDLED=$((VOLS_HANDLED + 1))
  else
    echo "  $v (not present, skipping)"
  fi
done

# ── Summary ──
echo ""
TOTAL=$((APPS_HANDLED + SECRETS_HANDLED + VOLS_HANDLED))
if $DRY_RUN; then
  echo ">> DRY-RUN complete. Would clean $TOTAL items ($APPS_HANDLED apps, $SECRETS_HANDLED secrets, $VOLS_HANDLED volumes). Re-run with --execute to actually delete."
else
  echo ">> Modal cleanup complete. Cleaned $TOTAL items ($APPS_HANDLED apps, $SECRETS_HANDLED secrets, $VOLS_HANDLED volumes)."
fi
