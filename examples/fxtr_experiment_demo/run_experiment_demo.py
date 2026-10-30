"""Demo workstation for the persistent experiment backend.

Boots a :class:`WorkstationServer` configured with:

- The **experiment workflow runner** under external name
  ``experiment-workflow`` (:mod:`experiment_runner`): a persistent
  :mod:`fxtr_experiment` session (two sqlite files under the runtime
  workdir's ``data/`` dir — workflow state shares the workdir's
  lifetime), the executor, and the mounted user capability, with the
  demo's standard task fns auto-bound. Relaunching against the same
  workdir demonstrates the startup running→interrupted flip and
  record-check resume.
- The **demo driver** under external name ``experiment-demo-helper``
  (:mod:`experiment_demo_helper_service`): scenario methods that build
  real workflow state through the same capability — open its panel in
  the browser and press buttons.
- The **behavior-research runner** under external name
  ``behavior-workflow`` (:mod:`behavior_runner`; its own supervisor +
  sqlite pair at ``data_behavior/``) and its **driver** under
  ``behavior-demo-helper`` (:mod:`behavior_demo_helper_service`): the
  staged car-sales scenario (subject models x user sims x judges).
  LLM services bind per ``--allow-llm-apis`` (real Anthropic; needs
  ``ANTHROPIC_API_KEY``) / ``--mocked-llm-apis`` (deterministic fakes
  under distinct handle keys) / neither (unbound; demands fault).
- A :class:`NixConfigurableDispatchImpl` renderer service under
  external name ``renderer-dispatcher``, auto-loading the ``default``
  UI preset from the ``--workdir`` repo's HEAD (the exp-note + driver
  renderers; the experiment *dashboard* slice joins the preset in
  phase-4 chunk 4).
- **Optionally** (``--enable-sandbox``), the sandbox spawner + an HTTPS
  passthrough internet-authorizer + the agent-MCP persistence
  namespace, exactly as in the workstation demo — for MCP-agent-driven
  sandboxed task fns. Off by default; the standard scenarios are all
  host-side.

This process is the **orchestrator**: it boots the workstation, then
spawns the Vite frontend as a child (threading the workstation URLs in
as ``VITE_*`` env vars) and parks until Ctrl-C. ``--no-frontend`` skips
Vite. Run via:

    uv run python run_experiment_demo.py --auto-workdir

or point ``--workdir`` at a workdir you scaffolded yourself (see
``agent_workdir/README.md``) to keep workflow state across relaunches.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import pathlib
import shutil
import signal
import subprocess
import sys
from collections.abc import AsyncGenerator
from typing import Any

import anyio

_HERE = pathlib.Path(__file__).resolve().parent

# Make the workdir's marker packages + fxtr_workdir_harness importable, the
# same way the workstation demo's orchestrator does (the packages are
# host-importable src/ layouts; the harness backs --auto-workdir).
_PACKAGES = _HERE / "agent_workdir" / "packages"
for _pkg in (
    "experiment_notes",
    "experiment_demo_helper",
    "behavior_research_types",
    "behavior_demo_helper",
):
    sys.path.insert(0, str(_PACKAGES / _pkg / "src"))
sys.path.insert(0, str(_HERE / "behavior"))
_REPO_ROOT = _HERE.parents[1]
sys.path.insert(0, str(_REPO_ROOT / "nix" / "fxtr_workdir_harness" / "src"))

from fxtr_workdir_harness import prepare_workdir_repo  # noqa: E402
from fxtr_workstation import PersistenceNamespaceConfig, WorkstationServer  # noqa: E402
from fxtr_workstation.dynamic_dispatch import mount_nix_configurable_dispatch  # noqa: E402
from fxtr_workstation.external_services import ExternalServiceConfig  # noqa: E402
from fxtr_workstation.peer_rpc import open_workstation_rpc  # noqa: E402

from behavior_demo_helper_service import mount_behavior_demo_helper  # noqa: E402
from behavior_runner import behavior_mount_factory  # noqa: E402
from experiment_demo_helper_service import mount_experiment_demo_helper  # noqa: E402
from experiment_runner import RunnerSupervisor  # noqa: E402

# External names exposed to clients.
EXPERIMENT_WORKFLOW_NAME = "experiment-workflow"
EXPERIMENT_DEMO_HELPER_NAME = "experiment-demo-helper"
BEHAVIOR_WORKFLOW_NAME = "behavior-workflow"
BEHAVIOR_DEMO_HELPER_NAME = "behavior-demo-helper"
RENDERER_DISPATCHER_NAME = "renderer-dispatcher"
SANDBOX_SPAWNER_NAME = "sandbox-spawner"
INTERNET_AUTHORIZER_NAME = "internet-authorizer"
AGENT_MCP_STORE_NAME = "agent_mcp_store"

# Template the runtime workdir is scaffolded from.
_TEMPLATE = _HERE / "agent_workdir"

# Fixed dest for ``--auto-workdir``: a throwaway runtime workdir
# re-scaffolded (WIPED — including the workflow's data/ dir) on every
# launch.
_AUTO_WORKDIR_PATH = pathlib.Path("~/.cache/fxtr_experiment_demo_workdir").expanduser()

# Wheel cache for the (flag-gated) sandbox spawner.
_SANDBOX_CACHE_DIR = _HERE / ".cache" / "sandbox_wheels"

# SQLite-backed persistence namespace for the agent's event-log chain
# (flag-gated with the sandbox).
_PERSISTENCE_DATA_DIR = _HERE / ".data"
_PERSISTENCE_DB_PATH = _PERSISTENCE_DATA_DIR / "agent_mcp_store.sqlite"

# Fixed workstation port (the workstation demo uses 8642). Matches
# ``agent_workdir/.fxtr_workstation.toml.example``.
_WORKSTATION_PORT = 8643


def _workdir_head(workdir: pathlib.Path) -> str:
    """The scaffolded workdir repo's committed HEAD (the preset rev)."""
    return subprocess.run(
        ["git", "-C", str(workdir), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@contextlib.asynccontextmanager
async def _maybe_sandbox(enable: bool, workdir: pathlib.Path) -> AsyncGenerator[Any | None]:
    """The flag-gated sandbox environment (spawner + authorizer host)."""
    if not enable:
        yield None
        return
    from fxtr_sandboxing.spawner import create_sandbox_environment

    _SANDBOX_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    async with create_sandbox_environment(
        cache_dir=_SANDBOX_CACHE_DIR, git_repo_path=workdir
    ) as sandbox_env:
        yield sandbox_env


@contextlib.asynccontextmanager
async def _maybe_agent_store(enable: bool) -> AsyncGenerator[Any | None]:
    """The flag-gated agent-MCP persistence namespace."""
    if not enable:
        yield None
        return
    from fxtr2.data.fxtr_context_api import SQLiteFxtrNamespace

    _PERSISTENCE_DATA_DIR.mkdir(parents=True, exist_ok=True)
    async with SQLiteFxtrNamespace(str(_PERSISTENCE_DB_PATH), "agent_mcp_store") as ns:
        yield ns


@contextlib.asynccontextmanager
async def _maybe_run_vite(
    run_frontend: bool, client_url: str, debug_url: str | None
) -> AsyncGenerator[None]:
    """Spawn the Vite dev server as a child process (see the workstation
    demo's orchestrator for the stdout=None rationale)."""
    if not run_frontend:
        yield
        return
    env = {
        **os.environ,
        "VITE_WORKSTATION_URL": client_url,
        "VITE_WORKSTATION_DEBUG_UI_URL": debug_url or "",
        "VITE_RENDERER_DISPATCHER_NAME": RENDERER_DISPATCHER_NAME,
    }
    proc = await anyio.open_process(
        ["pnpm", "--filter", "@fxtr-experiment-demo/frontend", "dev"],
        cwd=str(_HERE),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=None,
        stderr=None,
    )
    try:
        yield
    finally:
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        with anyio.move_on_after(5, shield=True):
            await proc.wait()
        with contextlib.suppress(ProcessLookupError):
            proc.kill()


async def _run(
    workdir: pathlib.Path | None,
    auto_workdir: bool,
    run_frontend: bool,
    enable_sandbox: bool,
    llm_mode: str,
) -> None:
    anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY")
    if llm_mode == "real" and not anthropic_api_key:
        raise SystemExit("--allow-llm-apis requires ANTHROPIC_API_KEY to be set")
    if auto_workdir:
        print(
            f"--auto-workdir: re-scaffolding {_AUTO_WORKDIR_PATH} from template "
            "(wipes previous workflow state)…",
            flush=True,
        )
        shutil.rmtree(_AUTO_WORKDIR_PATH, ignore_errors=True)
        prepared = await prepare_workdir_repo(_TEMPLATE, _AUTO_WORKDIR_PATH)
        workdir = prepared.path
        print(f"  scaffolded {workdir} @ {prepared.head_commit[:12]}", flush=True)
    assert workdir is not None  # argparse guarantees exactly one of the two modes

    head = _workdir_head(workdir)

    async with (
        _maybe_sandbox(enable_sandbox, workdir) as sandbox_env,
        _maybe_agent_store(enable_sandbox) as agent_store_ns,
        # The (crashable) runner lives on the supervisor; the *stable*
        # driver + dispatcher open OUTSIDE the WorkstationServer CM (the
        # external_services contract: the workstation pins each scope to
        # its root_scope for the server's lifetime). The runner's
        # capability is NOT a boot-time external service — the
        # supervisor registers it per revival over the peer-RPC link,
        # so the name comes and goes with the runner.
        RunnerSupervisor(workdir / "data") as supervisor,
        mount_experiment_demo_helper(supervisor) as helper,
        RunnerSupervisor(
            workdir / "data_behavior",
            mount=behavior_mount_factory(
                llm_mode=llm_mode,  # type: ignore[arg-type]  # argparse-constrained
                anthropic_api_key=anthropic_api_key,
            ),
            external_name=BEHAVIOR_WORKFLOW_NAME,
        ) as behavior_supervisor,
        mount_behavior_demo_helper(behavior_supervisor) as behavior_helper,
        mount_nix_configurable_dispatch(
            repo=workdir,
            initial_commit=head,
            initial_attr="fxtr.uiPresets.default",
        ) as renderer_dispatcher,
    ):
        external_services = [
            ExternalServiceConfig(
                external_name=EXPERIMENT_DEMO_HELPER_NAME,
                service=helper.service_ref,
                scope=helper.scope,
            ),
            ExternalServiceConfig(
                external_name=BEHAVIOR_DEMO_HELPER_NAME,
                service=behavior_helper.service_ref,
                scope=behavior_helper.scope,
            ),
            ExternalServiceConfig(
                external_name=RENDERER_DISPATCHER_NAME,
                service=renderer_dispatcher.service_ref,
                scope=renderer_dispatcher.scope,
            ),
        ]
        if sandbox_env is not None:
            internet_authorizer_ref = await sandbox_env.add_passthrough_transport()
            external_services += [
                ExternalServiceConfig(
                    external_name=SANDBOX_SPAWNER_NAME,
                    service=sandbox_env.spawner_ref,
                    scope=sandbox_env.host_scope,
                ),
                ExternalServiceConfig(
                    external_name=INTERNET_AUTHORIZER_NAME,
                    service=internet_authorizer_ref,
                    scope=sandbox_env.host_scope,
                ),
            ]
        persistence_namespaces = []
        if agent_store_ns is not None:
            persistence_namespaces.append(
                PersistenceNamespaceConfig(
                    external_name=AGENT_MCP_STORE_NAME,
                    namespace=agent_store_ns,
                    mode="preserve_provenance",
                )
            )

        async with WorkstationServer(
            port=_WORKSTATION_PORT,
            debug_ui_dist_path=WorkstationServer.bundled_debug_ui_dist_path(),
            external_services=external_services,
            persistence_namespaces=persistence_namespaces,
        ) as ws:
            # A loopback peer-RPC session to our own workstation (the
            # supervisors register their capabilities through it per
            # revival; same plumbing as the workstation demo's seeder).
            async with open_workstation_rpc(ws.rpc_url, name="experiment-demo/peer-rpc") as peer:
                try:
                    supervisor.set_registrar(peer.remote_service, peer.scope)
                    await supervisor.revive()
                    behavior_supervisor.set_registrar(peer.remote_service, peer.scope)
                    await behavior_supervisor.revive()
                    await _park(ws, workdir, head, sandbox_env, run_frontend, llm_mode)
                finally:
                    # Crash the runners while the peer link is still up,
                    # so their expose_service releases can reach the
                    # workstation (the supervisor CMs' own exits would
                    # run after the link closed).
                    await behavior_supervisor.crash()
                    await supervisor.crash()


async def _park(
    ws: WorkstationServer,
    workdir: pathlib.Path,
    head: str,
    sandbox_env: Any | None,
    run_frontend: bool,
    llm_mode: str,
) -> None:
    print()
    print(f"  workstation client URL: {ws.client_url}")
    print(f"  workstation debug UI:   {ws.debug_http_url}")
    print(f"  runtime workdir repo:   {workdir} @ {head[:12]}")
    print(f"  workflow state:         {workdir / 'data'} (survives relaunch)")
    print(
        f"  external service:       {EXPERIMENT_WORKFLOW_NAME!r} "
        "(the user capability — registered per runner revival)"
    )
    print(
        f"  external service:       {EXPERIMENT_DEMO_HELPER_NAME!r} "
        "(open it in the shell and press scenario buttons)"
    )
    print(
        f"  external service:       {BEHAVIOR_WORKFLOW_NAME!r} "
        f"(behavior-research workflow, state at {workdir / 'data_behavior'})"
    )
    llm_note = {
        "off": "LLM services UNBOUND — behavior runs fault until relaunched with a binding flag",
        "mock": "MOCKED LLM services (deterministic fakes under the :mock keys)",
        "real": "real Anthropic LLM services",
    }[llm_mode]
    print(f"  external service:       {BEHAVIOR_DEMO_HELPER_NAME!r} ({llm_note})")
    print(
        f"  external service:       {RENDERER_DISPATCHER_NAME!r} "
        "(nix preset 'default', auto-loaded by git ref)"
    )
    if sandbox_env is not None:
        print(f"  external service:       {SANDBOX_SPAWNER_NAME!r}")
        print(f"  external service:       {INTERNET_AUTHORIZER_NAME!r} " "(HTTPS passthrough)")
        print(
            f"  persistence cap:        {AGENT_MCP_STORE_NAME!r} "
            f"(preserve_provenance) at {_PERSISTENCE_DB_PATH}"
        )
    print()
    print(
        "Ctrl-C to shut down (use the driver panel's 'Crash runner' for the"
        " interruption arc; a hard `kill -9` + relaunch exercises the"
        " startup flip)."
    )
    print(flush=True)

    async with _maybe_run_vite(run_frontend, ws.client_url, ws.debug_http_url):
        with anyio.open_signal_receiver(signal.SIGINT, signal.SIGTERM) as signals:
            async for sig in signals:
                print(f"Received signal {sig}; shutting down.", flush=True)
                break


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Demo workstation for the persistent experiment backend.",
    )
    workdir_group = parser.add_mutually_exclusive_group(required=True)
    workdir_group.add_argument(
        "--workdir",
        type=pathlib.Path,
        help=(
            "A scaffolded runtime workdir repo (see agent_workdir/README.md). "
            "The renderer dispatcher resolves the UI preset against it, and "
            "the workflow's sqlite state lives under its data/ dir."
        ),
    )
    workdir_group.add_argument(
        "--auto-workdir",
        action="store_true",
        help=(
            f"Re-scaffold a throwaway runtime workdir at {_AUTO_WORKDIR_PATH} "
            "on every launch — WIPING previous workflow state. One-command "
            "self-contained testing; use --workdir for durable state."
        ),
    )
    parser.add_argument(
        "--no-frontend",
        action="store_true",
        help="Don't spawn the Vite dev server (headless / debug-UI-only).",
    )
    parser.add_argument(
        "--enable-sandbox",
        action="store_true",
        help=(
            "Mount the sandbox spawner + internet authorizer + agent-MCP "
            "persistence namespace (for MCP-agent-driven sandboxed task "
            "fns, as in the workstation demo). Off by default."
        ),
    )
    llm_group = parser.add_mutually_exclusive_group()
    llm_group.add_argument(
        "--allow-llm-apis",
        action="store_true",
        help=(
            "Bind the behavior scenario's real Anthropic-backed LLM "
            "services (requires ANTHROPIC_API_KEY)."
        ),
    )
    llm_group.add_argument(
        "--mocked-llm-apis",
        action="store_true",
        help=(
            "Bind deterministic MOCK LLM services (distinct handle keys — "
            "obviously fake data, no API key needed). Without either flag "
            "the behavior handles stay unbound and demands fault."
        ),
    )
    args = parser.parse_args()
    llm_mode = "real" if args.allow_llm_apis else ("mock" if args.mocked_llm_apis else "off")

    anyio.run(
        _run,
        args.workdir.resolve() if args.workdir is not None else None,
        args.auto_workdir,
        not args.no_frontend,
        args.enable_sandbox,
        llm_mode,
    )


if __name__ == "__main__":
    main()
