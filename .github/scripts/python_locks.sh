#!/usr/bin/env bash
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
#
# The hash-locked Python requirement files (E1, 2026-11-11). Every place that
# installs the cloud API's or the Windows GUI build's dependencies installs ONLY
# a lock written here, with `pip install --require-hashes`:
#   robotis_ai_setup/cloud_training_api/Dockerfile        (the production image)
#   .github/workflows/railway-deploy-cloud-api.yml        (W2's import probe)
#   .github/workflows/release-installer.yml               (the EduBotics.exe build)
#   .github/workflows/ci.yml::python-locks                (uv itself)
#
#   .github/scripts/python_locks.sh write   regenerate the locks after an input changed
#   .github/scripts/python_locks.sh check   ci.yml::python-locks: fail when a lock differs
#
# A lock is `uv pip compile --generate-hashes` for exactly one interpreter and
# platform. uv keeps the versions an existing output file already pins, so
# `write` changes only what an input changed, and `check` (which compiles into
# a copy of the committed lock) reproduces it byte for byte until an input
# moves: a requirements.txt edited without `write` (a Dependabot bump
# included) stays red until its lock is regenerated in the same change. To move
# a transitive dependency on purpose, delete its entry from the lock and run
# `write`.
set -euo pipefail

mode="${1:-}"
case "$mode" in
  write|check) ;;
  *) echo "usage: $0 write|check" >&2; exit 2 ;;
esac

cd "$(git rev-parse --show-toplevel)"

UV="${UV:-uv}"
want="$(sed -n 's/^uv==\([0-9.]*\)$/\1/p' .github/scripts/uv-requirements.txt)"
have="$("$UV" --version | awk '{print $2}')"
if [ -z "$want" ] || [ "$have" != "$want" ]; then
  echo "python_locks.sh: uv ${want:-?} required (.github/scripts/uv-requirements.txt), found ${have:-none}" >&2
  exit 1
fi

failed=0

# lock OUTPUT PYTHON PLATFORM EXTRA_FLAGS INPUT...
lock() {
  local out="$1" py="$2" platform="$3" extra="$4"
  shift 4
  local target="$out"
  if [ "$mode" = check ]; then
    target="$(mktemp)"
    cp "$out" "$target"
  fi
  # shellcheck disable=SC2086  # $extra is a list of flags (possibly empty)
  "$UV" pip compile "$@" --quiet --generate-hashes \
    --python-version "$py" --python-platform "$platform" $extra \
    --custom-compile-command ".github/scripts/python_locks.sh write" \
    -o "$target"
  if [ "$mode" = check ]; then
    if ! diff -u "$out" "$target"; then
      echo "::error file=$out::$out does not match its input; run .github/scripts/python_locks.sh write"
      failed=1
    fi
    rm -f "$target"
  else
    echo "wrote $out"
  fi
}

# The cloud API: the production image's interpreter (python:3.12-slim, x86_64).
lock robotis_ai_setup/cloud_training_api/requirements.lock 3.12 x86_64-manylinux_2_28 "" \
  robotis_ai_setup/cloud_training_api/requirements.txt

# The EduBotics.exe build (release-installer.yml: windows-latest, Python 3.11).
# setuptools is left out: PyInstaller needs one, and the build uses the runner's
# own (bundled with Python 3.11, as every installer up to v2.37.0 did); an
# upgraded setuptools would change what PyInstaller bundles into the .exe.
lock robotis_ai_setup/gui/requirements.lock 3.11 x86_64-pc-windows-msvc "--no-emit-package setuptools" \
  robotis_ai_setup/gui/requirements.txt robotis_ai_setup/gui/requirements-build.txt

# The build backend of requirements.lock's one source-only package (proxy_tools).
lock robotis_ai_setup/gui/requirements-build-backend.lock 3.11 x86_64-pc-windows-msvc "" \
  robotis_ai_setup/gui/requirements-build-backend.txt

# uv itself, for ci.yml::python-locks (ubuntu-latest, Python 3.12).
lock .github/scripts/uv-requirements.lock 3.12 x86_64-manylinux_2_28 "" \
  .github/scripts/uv-requirements.txt

exit "$failed"
