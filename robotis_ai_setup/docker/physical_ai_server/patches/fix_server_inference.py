#!/usr/bin/env python3
"""Patch server_inference.py to fix two bugs:

1. CRITICAL: self._endpoints dict is never initialized in __init__,
   causing AttributeError when register_endpoint() is called.
   Fix: insert 'self._endpoints = {}' before the first register_endpoint call.

2. MINOR: InferenceManager is constructed twice (the second overwrites the first).
   Fix: remove the duplicate block.
"""
import re
import sys


def patch(filepath: str) -> int:
    with open(filepath, "r") as f:
        content = f.read()

    original = content

    # --- Fix 1: Add self._endpoints = {} before first register_endpoint call ---
    if "self._endpoints = {}" not in content:
        content = content.replace(
            "        # Register the ping endpoint by default\n",
            "        self._endpoints = {}\n"
            "\n"
            "        # Register the ping endpoint by default\n",
        )

    # --- Fix 2: Remove duplicate InferenceManager initialization ---
    # The pattern is: zmq setup block, then a duplicate InferenceManager block.
    # We remove the second InferenceManager(...) block that appears after socket.bind.
    # Match: after socket.bind(...), a blank line, then duplicate InferenceManager block.
    dup_pattern = re.compile(
        r"(self\.socket\.bind\([^)]+\)\n)"       # socket.bind line
        r"\n"                                      # blank line
        r"        self\.inference_manager = InferenceManager\(\n"
        r"            policy_type=policy_type,\n"
        r"            policy_path=policy_path,\n"
        r"            device=device\n"
        r"        \)\n",
    )
    content = dup_pattern.sub(r"\1", content)

    if content != original:
        with open(filepath, "w") as f:
            f.write(content)
        print(f"  Patched successfully: {filepath}")
    else:
        print(f"  No changes needed: {filepath}")

    # Post-condition: verify Fix 1 is definitely in effect. If it isn't,
    # the upstream file was reformatted enough that our replace() slid off
    # and we'd ship an image that crashes on first register_endpoint()
    # call. Better to fail the build.
    if "self._endpoints = {}" not in content:
        print(
            f"ERROR: post-patch verification failed for {filepath}: "
            f"'self._endpoints = {{}}' is not present. "
            f"Upstream server_inference.py may have been reformatted — "
            f"update fix_server_inference.py to match.",
            file=sys.stderr,
        )
        return 2

    # Post-condition: Fix 2 (no duplicate InferenceManager). Count occurrences.
    duplicate_check = re.compile(
        r"self\.inference_manager = InferenceManager\(",
    )
    matches = duplicate_check.findall(content)
    if len(matches) > 1:
        print(
            f"ERROR: post-patch verification failed for {filepath}: "
            f"{len(matches)} InferenceManager constructions still present "
            f"(expected 1). Duplicate-removal regex did not match — "
            f"update fix_server_inference.py.",
            file=sys.stderr,
        )
        return 3

    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <server_inference.py>", file=sys.stderr)
        sys.exit(1)
    sys.exit(patch(sys.argv[1]))
