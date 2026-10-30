"""Scaffold a runnable copy of ``agent_workdir/`` into its own git repo.

The in-repo ``agent_workdir/`` is the **template**; the *runtime* workdir is a
copy-out into a fresh git repo. Two reasons it's its own repo (see
``projects/fxtr_workstation/plans/phase0/part5_nix_workdir/stage6_workstation_demo.md``):
``current_commit()``'s clean-tree guard would otherwise check the whole fxtr
monorepo (never clean in dev), and the agent would be committing into the
library/source repo.

This wraps :func:`fxtr_workdir_harness.prepare_workdir_repo`: copy the template,
substitute the ``fxtr`` lib ref (→ a pinned ``git+file`` rev of this checkout;
in a jj checkout the working-copy commit ``@``, made fetchable via the
harness's bare mirror under ``~/.cache/fxtr-workdir-harness/``),
``uv lock`` / ``nix flake lock`` the copy, ``git init`` + commit, and print the
path + committed HEAD. Point ``run_demo_workstation.py`` / ``start_demo.sh`` +
the MCP plugin at the printed path.

Usage (run from inside ``examples/fxtr_workstation_demo/``)::

    uv run python scaffold_workdir.py <dest-path> [--force]
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_TEMPLATE = _HERE / "agent_workdir"
# examples/fxtr_workstation_demo -> repo root.
_REPO_ROOT = _HERE.parents[1]
# `fxtr_workdir_harness` is not a dependency of this demo — add its src to
# sys.path (same spirit as run_demo_workstation.py's sys.path inserts for the
# agent packages). Its own `fxtr_nix_runner` import resolves from this demo's
# venv (via fxtr_sandboxing).
sys.path.insert(0, str(_REPO_ROOT / "nix" / "fxtr_workdir_harness" / "src"))

from fxtr_workdir_harness import prepare_workdir_repo  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scaffold a runnable agent_workdir copy into its own git repo.",
    )
    parser.add_argument(
        "dest",
        type=Path,
        help="Where to create the runtime workdir repo (must not exist unless --force).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Remove dest first if it already exists (you re-scaffold often).",
    )
    args = parser.parse_args()

    dest = args.dest.resolve()
    if dest.exists():
        if not args.force:
            parser.error(f"{dest} already exists (pass --force to replace it).")
        shutil.rmtree(dest)

    # Async because the flake lock runs through the configured
    # `fxtr_nix_runner` backend; in a jj checkout the lib ref resolves to the
    # working-copy commit via the harness's bare mirror (see the harness
    # module docstring, "Working from a jj workspace").
    prepared = asyncio.run(prepare_workdir_repo(_TEMPLATE, dest))

    print()
    print(f"Scaffolded runtime workdir repo: {prepared.path}")
    print(f"  committed HEAD: {prepared.head_commit}")
    print(f"  fxtr lib pinned to: {prepared.lib_ref.flake_url}")
    print()
    print("Next:")
    print(f"  ./start_demo.sh {prepared.path}")
    print(f"  # (or: uv run python run_demo_workstation.py --workdir {prepared.path})")
    print(f"  # then `cp .fxtr_workstation.toml.example .fxtr_workstation.toml` in")
    print(f"  # {prepared.path} and point the MCP plugin / Claude there.")


if __name__ == "__main__":
    main()
