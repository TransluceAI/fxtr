#!/usr/bin/env bash
# Build the guest site-packages dir for the pyodide sandbox spike.
#
# Two steps:
#   1. The repo-local pure-python packages, installed --no-deps (rpc_task's
#      declared dep is an unreachable direct URL that uv.sources normally
#      overrides; sidestep it entirely).
#   2. Third-party deps, resolved wheel-only against the pyodide platform so
#      a host-platform compiled wheel can never sneak in. (The exact ABI year
#      doesn't matter for these — they're all pure-python — the pin is just
#      "refuse anything that isn't a wasm-compatible wheel".)
set -euo pipefail
cd "$(dirname "$0")"
ROOT=../..
TARGET=guest_site_packages_gitignore

rm -rf "$TARGET"

uv pip install --target "$TARGET" --no-deps \
  "$ROOT/v2/portable" \
  "$ROOT/projects/fxtr_sandboxing/fxtr_sandbox_types" \
  "$ROOT/projects/fxtr_sandboxing/fxtr_sandbox_rpc_adapter" \
  "$ROOT/projects/fxtr_sandboxing/examples/rpc_task" \
  "$ROOT/projects/fxtr_sandboxing/examples/transport_task"

uv pip install --target "$TARGET" \
  --python-platform wasm32-pyodide2024 --python-version 3.12 \
  --only-binary :all: \
  "anyio>=4.12.1" "cbor2>=5.7.1,<6" "frozendict>=2.4.6" "mashumaro>=3.17"

echo "--- built $TARGET:"
ls "$TARGET" | head -30
