"""Scaffold a runnable copy of ``publishing_workdir/`` into its own git repo.

The in-repo ``publishing_workdir/`` is the **template**; the runnable workdir is a
copy-out into a fresh git repo (so ``current_commit()``'s clean-tree guard checks
this workdir, not the fxtr monorepo, and so builds reference a committed rev).

Wraps :func:`fxtr_workdir_harness.prepare_workdir_repo` (copy the template,
substitute the ``fxtr`` lib ref, ``uv lock`` / ``nix flake lock``, ``git init`` +
commit), then adds the publishing-specific steps:

1. **seed** — :func:`seed_data.seed` writes the ``Note`` bytes into the workdir's
   repo entity store (``.fxtr/``, gitignored) so the preload FODs can fetch them
   via the manager's rehost. (The committed ``root.fxtr.tsv`` already pins them.)
2. **relock** — :func:`fxtr_nix_runner.update_hashes.update_hashes` builds the
   ``fxtr.preloads.note_<k>`` slots and writes their hashes into ``fod_lock.toml``.
   The seed must precede this (the FODs fetch the seeded bytes).
3. **commit** the relocked ``fod_lock.toml``.

Requires a configured nix backend (the relock builds the preload FODs). Run from
inside ``examples/fxtr_publishing_demo/``::

    uv run python scaffold_workdir.py <dest-path> [--force]
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from pathlib import Path

import anyio

from seed_data import seed

_HERE = Path(__file__).resolve().parent
_TEMPLATE = _HERE / "publishing_workdir"
# examples/fxtr_publishing_demo -> repo root.
_REPO_ROOT = _HERE.parents[1]
# `fxtr_workdir_harness` is not a dependency of this demo — add its src to
# sys.path (its own `fxtr_nix_runner` import resolves from this demo's venv).
sys.path.insert(0, str(_REPO_ROOT / "nix" / "fxtr_workdir_harness" / "src"))

from fxtr_workdir_harness import prepare_workdir_repo  # noqa: E402


async def _scaffold(dest: Path) -> Path:
    prepared = await prepare_workdir_repo(_TEMPLATE, dest)
    await seed(prepared.path)

    # Relock the preload FOD hashes (builds the slots against the seeded store),
    # then commit the updated lock so the served live workdir is reproducible.
    from fxtr_nix_runner.update_hashes import update_hashes

    await update_hashes(prepared.path)
    await anyio.run_process(
        ["git", "-C", str(prepared.path), "commit", "-am", "seed notes + relock preload hashes"],
        check=False,
    )
    return prepared.path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scaffold a runnable publishing_workdir copy into its own git repo.",
    )
    parser.add_argument(
        "dest",
        type=Path,
        help="Where to create the runtime workdir repo (must not exist unless --force).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Remove dest first if it already exists.",
    )
    args = parser.parse_args()

    dest = args.dest.resolve()
    if dest.exists():
        if not args.force:
            parser.error(f"{dest} already exists (pass --force to replace it).")
        shutil.rmtree(dest)

    path = asyncio.run(_scaffold(dest))

    print()
    print(f"Scaffolded + seeded runtime workdir repo: {path}")
    print()
    print("Next: build the viewer once, then serve (build + UI on one origin):")
    print("  pnpm --filter @fxtr-ui/published-app build")
    print(f"  uv run fxtr_nix serve {path} --port 8080 \\")
    print(
        "    --web-dist ../../projects/fxtr_workstation/frontend_lib/packages/fxtr-ui-published-app/dist"
    )
    print("  # then open http://127.0.0.1:8080")


if __name__ == "__main__":
    main()
