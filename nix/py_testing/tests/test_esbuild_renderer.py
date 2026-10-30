"""Build the `esbuildRenderer` smoke target and check the compiled ESM.

Focused regression test for `lib.fxtr.esbuildRenderer`: builds the repo-root
`renderer-esm-check` target (a trivial TSX fixture: imports `react` + uses
JSX) and asserts the output is an ESM module that keeps the externals as
bare-specifier imports rather than bundling them. Guards against a nixpkgs
esbuild bump breaking `--jsx=automatic` or the externals handling regressing.

The `nix_build` / `flake_ref` fixtures (in conftest.py) build via a git
flake reference to the repo root, so run from the colocated jj↔git worktree.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def renderer_esm(nix_build: Callable[[str], str]) -> str:
    return nix_build("packages.x86_64-linux.renderer-esm-check")


def test_esbuild_renderer_emits_esm_with_bare_externals(renderer_esm: str) -> None:
    out = Path(renderer_esm)
    # esbuildRenderer's $out is the .mjs file itself, not a directory.
    assert out.is_file(), f"expected a file at {out}"
    source = out.read_text()
    assert source.strip(), "compiled ESM is empty"

    # ESM output (the dispatcher serves it with `--format=esm`).
    assert "export" in source

    # Automatic-JSX transform ran and routed through the externalized
    # `react/jsx-runtime` rather than bundling a JSX helper.
    assert 'from "react/jsx-runtime"' in source

    # `react` stays a bare-specifier import (resolved by the frontend's
    # import-map shim at load time) — not bundled into the module.
    assert 'from "react"' in source
