"""Check `lib.fxtr.mkUiPreset`'s transpose + duplicate-claim detection.

`mkUiPreset` inverts per-renderer declarations into the type-id-keyed routing
table the nix/git dispatcher consumes (the nix analog of the pre-nix
`demo_dispatch.py:_build_routing_table`). It's a pure structural transpose —
it passes the `renderer` / `typeEntity` values through untouched and builds
nothing — so the fixtures feed it plain-attrset stubs and we read back
`builtins.toJSON` of the result.

- `ui-preset-transpose-check` emits the transposed table as JSON. The fixture
  (in the repo-root flake) exercises multiple subtypes in one entry and
  *sharding* one type across two entries with non-overlapping keys.
- `ui-preset-conflict-check` has two entries claim the same
  `(type-id, dispatch-key)` pair, so the helper `throw`s when `toJSON` forces
  it and `nix build` fails at eval.

The `nix_build` / `flake_ref` fixtures (conftest.py) build via a git flake
reference to the repo root, so run from the colocated jj↔git worktree.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path


def test_transpose_inverts_and_shards(nix_build: Callable[[str], str]) -> None:
    table = json.loads(
        Path(nix_build("packages.x86_64-linux.ui-preset-transpose-check")).read_text()
    )

    # Two types, keyed by id_string (the dispatcher's table key).
    assert set(table) == {"type-a", "type-b"}

    # type-a is sharded across both entries: `multi` contributes u1/u2 (subtype
    # "sa"), `shard` contributes u4 (subtype "sc"); the keys merge.
    a = table["type-a"]
    assert a["typeEntity"] == {
        "type": "fxtr_layer_entity",
        "layer": {"drvPath": "/drv/ea.drv"},
        "layerKey": "ka",
    }
    assert set(a["byDispatchKey"]) == {"u1", "u2", "u4"}
    assert a["byDispatchKey"]["u1"] == {
        "subtypeKey": "sa",
        "renderer": {"drvPath": "/drv/r-multi.drv"},
    }
    assert a["byDispatchKey"]["u2"]["subtypeKey"] == "sa"
    # The sharded key carries the *second* entry's renderer + subtype.
    assert a["byDispatchKey"]["u4"] == {
        "subtypeKey": "sc",
        "renderer": {"drvPath": "/drv/r-shard.drv"},
    }

    # type-b is the second subtype of the `multi` entry (one module, two types).
    b = table["type-b"]
    assert b["typeEntity"]["layerKey"] == "kb"
    assert b["byDispatchKey"] == {
        "u3": {"subtypeKey": "sb", "renderer": {"drvPath": "/drv/r-multi.drv"}},
    }


def test_duplicate_dispatch_key_claim_throws(flake_ref: Callable[[str], str]) -> None:
    # Build directly (not via the nix_build fixture) so we can read stderr.
    result = subprocess.run(
        ["nix", "build", "--no-link", flake_ref("packages.x86_64-linux.ui-preset-conflict-check")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0, "expected the duplicate (type, key) claim to fail the build"
    # The throw names the colliding key, type, and both entries.
    assert "claimed twice" in result.stderr
    assert "'dup'" in result.stderr
    assert "'type-a'" in result.stderr
    assert "'e1'" in result.stderr and "'e2'" in result.stderr
