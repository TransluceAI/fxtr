"""Shared fixtures for the nix-integration tests.

Flake outputs are built/queried via a git reference resolved by
``fxtr_workdir_harness.resolve_lib_ref`` (see ``design_notes/10_nix_infrastructure.md``
Q10.E for why the repo-root flake is built through a git ref rather than
``path:``). Resolving through the harness means these tests build correctly
from a colocated *or* non-colocated jj workspace (and a plain-git CI checkout)
— not only from the colocated jj↔git worktree:

- In a jj workspace, the ref pins the workspace's ``@`` working-copy commit
  (``jj log`` snapshots current edits first, so uncommitted working-tree
  changes are included) via the harness-owned bare mirror under
  ``~/.cache/fxtr-workdir-harness/``.
- In a plain git checkout, the ref pins ``HEAD``.

Either way the build still needs a reachable ``x86_64-linux`` builder.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable

import pytest
from fxtr_workdir_harness import resolve_lib_ref


@pytest.fixture(scope="session")
def flake_ref() -> Callable[[str], str]:
    """Return ``attr -> "<lib flake ref>#<attr>"``.

    The flake ref comes from :func:`fxtr_workdir_harness.resolve_lib_ref`, so it
    points at the workspace's ``@`` commit (jj) or ``HEAD`` (plain git) via a
    fetchable git ref — the same resolution the workdir-template tests use.
    """
    lib_ref = resolve_lib_ref()
    return lambda attr: f"{lib_ref.flake_url}#{attr}"


@pytest.fixture(scope="session")
def nix_build(flake_ref: Callable[[str], str]) -> Callable[[str], str]:
    """Return ``attr -> realized store path`` (via ``nix build``)."""

    def _build(attr: str) -> str:
        result = subprocess.run(
            ["nix", "build", "--no-link", "--print-out-paths", flake_ref(attr)],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip().splitlines()[-1]

    return _build
