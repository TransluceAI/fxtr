"""Repin the demo workdir to the current working copy and hot-reload the preset.

The renderer dev loop, one command::

    uv run python reload_renderers.py --workdir ~/my_demo_workdir

against a RUNNING demo (``run_experiment_demo.py --workdir <same>``).
What it does, in order:

1. **Repin** (:func:`fxtr_workdir_harness.repin_workdir`): resolves the
   fxtr repo's current working copy — in a jj workspace that is the
   ``@`` commit, so *uncommitted edits are included* (jj snapshots
   first) — pushes it to the harness's bare mirror, rewrites the
   workdir's marked flake-input line to the new rev, re-runs
   ``nix flake lock`` through the configured ``fxtr_nix_runner``
   backend (podman needs no host nix), and commits the workdir.
   The commit sweeps the WHOLE workdir tree, so pending workdir-local
   renderer edits ride along — this is the one command for both
   repo-lib and workdir-local renderer iteration. Pass ``--uv`` only
   when library *Python* the workdir venvs consume changed (it re-pins
   the uv git sources + re-runs ``uv lock``, which triggers venv
   rebuilds in the next preset build).
2. **Reload**: connects to the running workstation's ``/rpc`` endpoint
   (`fxtr_workstation.peer_rpc.open_workstation_rpc`), resolves the
   ``renderer-dispatcher`` external service, and calls
   ``set_preset(<new workdir HEAD>, "fxtr.uiPresets.default")``. The
   dispatcher nix-builds what changed (a renderer-only edit rebuilds
   just its esbuild slice — sources are content-addressed) and
   broadcasts a reload; open browser panels re-render in place with
   their ``ownStorage`` state intact. No browser refresh needed.

Preconditions / failure modes:

- The demo must be running against the SAME ``--workdir`` (connection
  refused on port 8643 → it isn't). Don't use ``--auto-workdir`` for
  iterative work — it wipes and re-scaffolds on every launch, so the
  repin commit would be discarded.
- The workdir must be a scaffolded copy (marked ``flake.nix``), not
  the in-repo template — ``repin_workdir`` refuses otherwise.
- A failing preset build surfaces as the ``set_preset`` call raising
  (an ``IRException`` naming the nix error); the dispatcher keeps
  serving the previous preset. Debug the failing slice with a direct
  ``nix build`` of the workdir flake attr.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

import anyio

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parents[1] / "nix" / "fxtr_workdir_harness" / "src"))

from fxtr_workdir_harness import repin_workdir  # noqa: E402
from fxtr_workstation.dynamic_dispatch.dynamic_dispatch_types import (
    NixDispatcherProto,
)  # noqa: E402
from fxtr_workstation.peer_rpc import open_workstation_rpc  # noqa: E402

from run_experiment_demo import _WORKSTATION_PORT, RENDERER_DISPATCHER_NAME  # noqa: E402

PRESET_ATTR = "fxtr.uiPresets.default"


async def _run(workdir: pathlib.Path, *, uv: bool, port: int) -> None:
    print(f"repinning {workdir} to the current working copy…", flush=True)
    repinned = await repin_workdir(workdir, uv=uv)
    state = "repinned" if repinned.changed else "already pinned"
    print(
        f"  {state}: lib rev {repinned.lib_ref.rev[:12]} → workdir HEAD "
        f"{repinned.head_commit[:12]}",
        flush=True,
    )

    rpc_url = f"ws://127.0.0.1:{port}/rpc"
    print(f"pushing preset {PRESET_ATTR!r} @ {repinned.head_commit[:12]} via {rpc_url}…")
    async with open_workstation_rpc(rpc_url, name="reload-renderers") as peer:
        ref = await (await peer.remote_service.resolve(peer.scope)).get_service_by_external_name(
            RENDERER_DISPATCHER_NAME
        )
        dispatcher = await ref.replace_service_type(NixDispatcherProto).resolve(peer.scope)
        await dispatcher.set_preset(repinned.head_commit, PRESET_ATTR)
    print("preset reloaded — open panels re-render in place.", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Repin the demo workdir + hot-reload the UI preset (see module docstring).",
    )
    parser.add_argument(
        "--workdir",
        type=pathlib.Path,
        required=True,
        help="The runtime workdir the RUNNING demo was launched with (--workdir, not --auto-workdir).",
    )
    parser.add_argument(
        "--uv",
        action="store_true",
        help="Also re-pin the uv git sources (library Python changed; rebuilds venvs).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=_WORKSTATION_PORT,
        help="The running demo's workstation port.",
    )
    args = parser.parse_args()
    anyio.run(lambda: _run(args.workdir.resolve(), uv=args.uv, port=args.port))


if __name__ == "__main__":
    main()
