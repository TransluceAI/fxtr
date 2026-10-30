"""CLI for :func:`fxtr_workdir_harness.repin_workdir`.

    python -m fxtr_workdir_harness.repin <workdir> [--uv]
        [--lib-repo PATH] [--lib-rev REV]

Re-pins an existing scaffolded workdir's fxtr lib ref to the current
working copy (jj ``@`` via the harness mirror; plain-git HEAD
otherwise), relocks, and commits — see the package docstring's
"In-place repin" section for exactly what is rewritten and why the
commit sweeps pending workdir edits along. Prints the new workdir HEAD
(what a dispatcher ``set_preset`` call wants).

Runs the ``nix flake lock`` step through the configured
``fxtr_nix_runner`` backend (``FXTR_NIX_BACKEND`` /
``~/.config/fxtr/nix_runtime.toml``), so a podman-configured machine
needs no host nix.
"""

from __future__ import annotations

import argparse
import pathlib

import anyio

from . import repin_workdir


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Re-pin a scaffolded workdir's fxtr lib ref in place (see module docstring).",
    )
    parser.add_argument("workdir", type=pathlib.Path, help="The scaffolded workdir git repo.")
    parser.add_argument(
        "--uv",
        action="store_true",
        help=(
            "Also re-pin the uv git sources + re-run `uv lock` (needed only "
            "when library Python the workdir venvs consume changed; triggers "
            "venv rebuilds on the next preset build)."
        ),
    )
    parser.add_argument(
        "--lib-repo", type=pathlib.Path, default=None, help="Override the library repo."
    )
    parser.add_argument("--lib-rev", default=None, help="Override the library rev.")
    args = parser.parse_args()

    result = anyio.run(
        lambda: repin_workdir(
            args.workdir.resolve(), lib_repo=args.lib_repo, lib_rev=args.lib_rev, uv=args.uv
        )
    )
    state = "repinned" if result.changed else "already pinned (no commit)"
    print(f"{state}: lib rev {result.lib_ref.rev[:12]} → workdir HEAD {result.head_commit}")


if __name__ == "__main__":
    main()
