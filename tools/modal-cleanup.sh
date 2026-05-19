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
# Usage:
#   ./modal-cleanup.sh             # dry-run (default)
#   ./modal-cleanup.sh --execute   # actually delete
#
set -euo pipefail

DRY_RUN=true
for arg in "$@"; do
  case "$arg" in
    --execute) DRY_RUN=false ;;
    --dry-run) DRY_RUN=true ;;
    -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
    *) echo "Unknown argument: $arg" >&2; exit 2 ;;
  esac
done

command -v modal >/dev/null || { echo "modal CLI not installed" >&2; exit 1; }

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

# ── Apps to delete ──
echo ""
echo ">> Apps to delete (those starting with 'example-mcp-'):"
APPS=$(modal app list 2>/dev/null | grep -oE 'ap-[A-Za-z0-9]+\s+example-mcp[^[:space:]]*' | awk '{print $1}' || true)
if [ -z "$APPS" ]; then
  echo "  (none found)"
else
  for app_id in $APPS; do
    if $DRY_RUN; then
      # NB: literal text, NOT a backtick subshell — otherwise dry-run
      # would actually invoke the command it claims to preview.
      echo "  DRY-RUN: would run 'modal app remove $app_id'"
    else
      echo "  Removing $app_id..."
      modal app stop "$app_id" 2>/dev/null || true
      modal app remove "$app_id" --yes 2>&1 || echo "    WARN: remove failed"
    fi
  done
fi

# ── Secrets to delete ──
echo ""
echo ">> Legacy secrets to delete:"
LEGACY_SECRETS=("stripe" "supabase" "hf-token" "huggingface")
for s in "${LEGACY_SECRETS[@]}"; do
  if modal secret list 2>/dev/null | grep -q "^│ ${s} "; then
    if $DRY_RUN; then
      echo "  DRY-RUN: would run 'modal secret delete $s --yes'"
    else
      echo "  Deleting secret $s..."
      modal secret delete "$s" --yes 2>&1 || echo "    WARN: delete failed"
    fi
  else
    echo "  $s (not present, skipping)"
  fi
done

# ── Volumes to delete ──
echo ""
echo ">> Legacy volumes to delete:"
LEGACY_VOLS=("PaliGemma" "act" "gr00t-n1")
for v in "${LEGACY_VOLS[@]}"; do
  if modal volume list 2>/dev/null | grep -q "^│ ${v} "; then
    if $DRY_RUN; then
      echo "  DRY-RUN: would run 'modal volume delete $v --yes'"
    else
      echo "  Deleting volume $v..."
      modal volume delete "$v" --yes 2>&1 || echo "    WARN: delete failed"
    fi
  else
    echo "  $v (not present, skipping)"
  fi
done

echo ""
if $DRY_RUN; then
  echo ">> DRY-RUN complete. Re-run with --execute to actually delete."
else
  echo ">> Modal cleanup complete."
fi
