"""Demo workstation for the assembly-stage demo.

Boots a :class:`WorkstationServer` configured with:

- A ``preserve_provenance`` persistence cap under external name
  ``agent_mcp_store`` (sqlite-backed at ``.data/agent_mcp_store.sqlite``).
  This is what the MCP plugin pins its event-log chain head to;
  reconnects + agent restarts pick up where they left off.
- A sandbox spawner + an HTTPS passthrough internet-authorizer
  exposed via ``external_services`` (so the agent can spin up
  sandboxed services with controlled network egress).
- A :class:`NixConfigurableDispatchImpl` renderer service under
  external name ``renderer-dispatcher``, configured against the
  ``--workdir`` repo and auto-loading the ``default`` UI preset at
  that repo's HEAD (so the agent panel renders on boot). The agent
  re-pushes the same preset by git ref via ``set_preset`` after
  editing a renderer / the flake.
- A report-builder service (:mod:`report_builder_service`) under
  external name ``report-builder``. The agent invokes
  ``build_root_report(...)`` / ``build_sub_report(...)`` to
  compile MDX + entity inputs into :class:`RootReport` /
  :class:`SubReport` entities, which the frontend then renders
  through the bundled RootReport / SubReport renderers.
- Optional per-API LLM authorizers (``--allow-llm-apis``) for
  Anthropic / OpenAI, gated behind matching env vars.
- Optional read-only Docent SDK proxy (``--enable-docent``) exposing
  DQL + metadata queries to the agent. Off by default; auth picks up
  ``docent.env`` from the launch CWD when enabled.

This process is the **orchestrator**: it boots the workstation, then
spawns the Vite frontend as a child (threading the workstation URLs in
as ``VITE_*`` env vars) and parks until Ctrl-C, terminating Vite on the
way out. ``--no-frontend`` skips Vite (headless / debug-UI-only). Run via:

    uv run python run_demo_workstation.py --workdir <scaffolded-repo>

or, more conveniently:

    ./start_demo.sh <scaffolded-repo>

where ``<scaffolded-repo>`` is a runtime workdir produced by
``scaffold_workdir.py`` (the spawner + dispatcher resolve git refs
against it).
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
from fxtr2.data.fxtr_context_api import SQLiteFxtrNamespace
from fxtr_sandboxing.proxy import TransportRoute
from fxtr_sandboxing.spawner import create_sandbox_environment
from fxtr_workstation import (
    PersistenceNamespaceConfig,
    WorkstationServer,
    expose_entity,
)
from fxtr_workstation.dynamic_dispatch import mount_nix_configurable_dispatch
from fxtr_workstation.external_services import ExternalServiceConfig
from fxtr_workstation.peer_rpc import open_workstation_rpc

# Make the agent's ``presentables``, ``transcripts``, and
# ``docent_fxtr_proxy`` packages importable from the host process.
# ``presentables`` / ``transcripts`` are used by the
# ``_seed_demo_entities`` helper below; ``docent_fxtr_proxy`` declares
# the entity dataclasses + ``DocentProto`` the host-side
# :mod:`docent_service` constructs and returns. Same ``src/`` layout
# the MCP sandbox bridge uses for the agent.
_AGENT_PACKAGES = pathlib.Path(__file__).resolve().parent / "agent_workdir" / "packages"
# ``demo_helper`` (+ its impl in ``demo_helper_service.py``) needs the helper
# protocol/type id plus the connect-config entity types it constructs host-side
# (``delayed_echo.DelayedEchoConfig``, ``simple_tasks.PromptTemplate``); all are
# host-importable marker-style packages.
for _pkg in (
    "presentables",
    "transcripts",
    "docent_fxtr_proxy",
    "demo_helper",
    "delayed_echo",
    "simple_tasks",
):
    sys.path.insert(0, str(_AGENT_PACKAGES / _pkg / "src"))

# ``fxtr_workdir_harness`` (the scaffolder behind ``scaffold_workdir.py``) is not
# a dependency of this demo — add its src to ``sys.path`` the same way
# ``scaffold_workdir.py`` does, so ``--auto-workdir`` can re-scaffold a throwaway
# runtime workdir on launch. ``_REPO_ROOT`` = examples/fxtr_workstation_demo ->
# repo root.
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "nix" / "fxtr_workdir_harness" / "src"))

# Demo-local services. Live alongside this script so they can be
# imported as sibling modules when run via
# ``uv run python run_demo_workstation.py`` (which puts the script
# dir on ``sys.path``). The docent-proxy service is imported after
# ``sys.path`` is extended above so its ``docent_fxtr_proxy`` import
# resolves.
from demo_helper_service import mount_demo_helper  # noqa: E402
from docent_service import mount_docent_service  # noqa: E402
from fxtr_workdir_harness import prepare_workdir_repo  # noqa: E402
from presentables import Note  # noqa: E402
from report_builder_service import mount_report_builder  # noqa: E402
from transcripts import Transcript, TranscriptMessage  # noqa: E402

# External names exposed to the agent.
AGENT_MCP_STORE_NAME = "agent_mcp_store"
SANDBOX_SPAWNER_NAME = "sandbox-spawner"
INTERNET_AUTHORIZER_NAME = "internet-authorizer"
ANTHROPIC_AUTHORIZER_NAME = "anthropic-authorizer"
OPENAI_AUTHORIZER_NAME = "openai-authorizer"
RENDERER_DISPATCHER_NAME = "renderer-dispatcher"
REPORT_BUILDER_NAME = "report-builder"
DOCENT_PROXY_NAME = "docent-fxtr-proxy"
DEMO_HELPER_NAME = "demo-helper"

_HERE = pathlib.Path(__file__).resolve().parent

# Template the runtime workdir is scaffolded from (same dir
# ``scaffold_workdir.py`` copies out of).
_TEMPLATE = _HERE / "agent_workdir"

# Fixed dest for ``--auto-workdir``: a throwaway runtime workdir
# re-scaffolded from ``_TEMPLATE`` on every launch (so each run gets a
# current workdir without a separate ``scaffold_workdir.py`` step).
_AUTO_WORKDIR_PATH = pathlib.Path("~/.cache/fxtr_demo_workdir").expanduser()

# Wheel cache for the sandbox spawner. Survives across runs so
# re-launches after the first don't re-download wheels.
_SANDBOX_CACHE_DIR = _HERE / ".cache" / "sandbox_wheels"

# SQLite-backed persistence namespace for the agent's event-log
# chain. Lives across runs so the agent's history survives
# restarts (the chain's contract).
_PERSISTENCE_DATA_DIR = _HERE / ".data"
_PERSISTENCE_DB_PATH = _PERSISTENCE_DATA_DIR / "agent_mcp_store.sqlite"

# Fixed workstation port. Matches the frontend's expectation;
# changing here requires editing
# ``agent_workdir/.fxtr_workstation.toml.example`` too.
_WORKSTATION_PORT = 8642


async def _build_llm_authorizers(sandbox_env: Any) -> list[tuple[str, Any]]:
    """Bridge in host-specific authorizers for any LLM API whose key
    is set in the environment. Same shape as the basic_test_demo's
    builder — pins the upstream URL + injects the API key as a
    header so sandboxed code never sees it directly.
    """
    registrations: list[tuple[str, Any]] = []

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if anthropic_key:
        ref = await sandbox_env.add_transport(
            TransportRoute(
                upstream_base_url="https://api.anthropic.com",
                injected_headers={
                    "x-api-key": anthropic_key,
                    "anthropic-version": "2023-06-01",
                },
                allowed_path_prefixes=None,
            )
        )
        registrations.append((ANTHROPIC_AUTHORIZER_NAME, ref))

    openai_key = os.environ.get("OPENAI_API_KEY")
    if openai_key:
        ref = await sandbox_env.add_transport(
            TransportRoute(
                upstream_base_url="https://api.openai.com",
                injected_headers={"Authorization": f"Bearer {openai_key}"},
                allowed_path_prefixes=None,
            )
        )
        registrations.append((OPENAI_AUTHORIZER_NAME, ref))

    if not registrations:
        print(
            "warning: --allow-llm-apis passed but neither ANTHROPIC_API_KEY "
            "nor OPENAI_API_KEY is set; no LLM authorizers bridged in.",
            file=sys.stderr,
            flush=True,
        )

    return registrations


@contextlib.asynccontextmanager
async def _seed_demo_entities(rpc_url: str):
    """Open a peer-RPC connection to the running workstation and
    expose sample :class:`Note` and :class:`Transcript` entities
    under the external names ``"demo-note"`` and
    ``"demo-transcript"``, so the dynamic-dispatch renderers can
    be exercised end-to-end against real types.

    Stays connected for the lifetime of the workstation so the
    registrations don't drop.
    """

    async with open_workstation_rpc(rpc_url, name="fxtr-workstation-demo/peer-rpc") as session:
        entities = [
            (
                "demo-note",
                Note(
                    title="Hello from the demo backend",
                    body=(
                        "This Note entity is exposed under the external name "
                        "'demo-note'. Open it from the workstation shell to "
                        "see the renderers/note/ module render through "
                        "FullPanel / BlockLink / InlineLink."
                    ),
                ),
            ),
            (
                "demo-transcript",
                Transcript(
                    transcript_id="00000000-0000-4000-8000-000000000001",
                    agent_run_id="00000000-0000-4000-8000-000000000002",
                    name="main",
                    description="Hello from the demo backend",
                    created_at="2026-05-22T12:00:00Z",
                    messages=(
                        TranscriptMessage(
                            role="user",
                            content=("Can you summarize what this workstation demo" " exposes?"),
                        ),
                        TranscriptMessage(
                            role="assistant",
                            content=(
                                "This demo exposes a Note entity under"
                                " 'demo-note' and a Transcript entity under"
                                " 'demo-transcript'. Open either from the"
                                " workstation shell to exercise the"
                                " dynamic-dispatch renderers for"
                                " renderers/note/ and renderers/transcript/."
                            ),
                        ),
                    ),
                ),
            ),
        ]
        refs = [(name, entity.store(session.scope)) for name, entity in entities]
        async with contextlib.AsyncExitStack() as stack:
            for name, ref in refs:
                await stack.enter_async_context(
                    expose_entity(
                        session.remote_service,
                        name,
                        ref.entity_id,
                        session.scope,
                    )
                )
            yield


def _workdir_head(workdir: pathlib.Path) -> str:
    """The scaffolded workdir repo's committed HEAD — the rev the dispatcher
    auto-loads the ``default`` preset from (and the rev the agent pins via
    ``current_commit()``)."""
    return subprocess.run(
        ["git", "-C", str(workdir), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@contextlib.asynccontextmanager
async def _maybe_mount_docent(
    enable: bool,
) -> AsyncGenerator[Any | None, None]:
    """Mount the docent-proxy service when ``enable`` is True."""
    if not enable:
        yield None
        return
    async with mount_docent_service() as docent_proxy:
        yield docent_proxy


@contextlib.asynccontextmanager
async def _maybe_run_vite(
    run_frontend: bool, client_url: str, debug_url: str | None
) -> AsyncGenerator[None]:
    """Spawn the Vite dev server as a child process, threading the workstation
    URLs in as the ``VITE_*`` env vars the frontend reads via
    ``import.meta.env``. The Python process is now the orchestrator (no TS-side
    spawner), so we own Vite's lifecycle: terminate (then kill) it on exit.
    A no-op when ``run_frontend`` is False (headless / debug-UI-only)."""
    if not run_frontend:
        yield
        return
    env = {
        **os.environ,
        "VITE_WORKSTATION_URL": client_url,
        "VITE_WORKSTATION_DEBUG_UI_URL": debug_url or "",
        "VITE_RENDERER_DISPATCHER_NAME": RENDERER_DISPATCHER_NAME,
    }
    # cwd = the demo dir (not frontend/) — mirrors the old start_demo.sh
    # invocation, so pnpm resolves the workspace + the --filter target.
    # stdout/stderr=None inherits the parent's terminal — anyio's
    # open_process defaults to PIPE, which would silently capture
    # Vite's banner (incl. the frontend URL) into a pipe nobody
    # drains (and block Vite once the buffer fills).
    proc = await anyio.open_process(
        ["pnpm", "--filter", "@fxtr-workstation-demo/frontend", "dev"],
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
    allow_llm_apis: bool,
    run_frontend: bool,
    enable_docent: bool,
) -> None:
    if auto_workdir:
        # Re-scaffold a fresh throwaway runtime workdir at the fixed cache
        # path, overwriting whatever was there — so each launch gets a
        # current copy of the in-repo template. (A few seconds of
        # ``uv lock`` + ``nix flake lock``; no staleness detection.)
        print(f"--auto-workdir: re-scaffolding {_AUTO_WORKDIR_PATH} from template…", flush=True)
        shutil.rmtree(_AUTO_WORKDIR_PATH, ignore_errors=True)
        prepared = await prepare_workdir_repo(_TEMPLATE, _AUTO_WORKDIR_PATH)
        workdir = prepared.path
        print(f"  scaffolded {workdir} @ {prepared.head_commit[:12]}", flush=True)
    assert workdir is not None  # argparse guarantees exactly one of the two modes

    _SANDBOX_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _PERSISTENCE_DATA_DIR.mkdir(parents=True, exist_ok=True)
    head = _workdir_head(workdir)

    # ``create_sandbox_environment`` and ``SQLiteFxtrNamespace`` are
    # both opened *outside* the ``WorkstationServer`` CM, per the
    # external_services / persistence_namespaces contract — the
    # workstation pins each scope to its ``root_scope`` for the
    # server's lifetime, so they can't drain until shutdown
    # completes. ``git_repo_path`` is the scaffolded workdir repo the
    # spawner resolves ``NixWasmServiceDefn`` git refs against.
    async with create_sandbox_environment(
        cache_dir=_SANDBOX_CACHE_DIR, git_repo_path=workdir
    ) as sandbox_env:
        async with SQLiteFxtrNamespace(
            str(_PERSISTENCE_DB_PATH),
            "agent_mcp_store",
        ) as agent_mcp_store_ns:
            # HTTPS passthrough authorizer (no host whitelist —
            # convenient for demos). Agents can bridge this into a
            # sandboxed service's connect config to give it
            # ``get_client``-shaped internet access.
            internet_authorizer_ref = await sandbox_env.add_passthrough_transport()

            # Optional per-API authorizers, gated on env vars.
            llm_authorizers: list[tuple[str, Any]] = (
                await _build_llm_authorizers(sandbox_env) if allow_llm_apis else []
            )
            # The anthropic authorizer ref (if any) — the demo-helper's LLM
            # scenarios connect spawned services to it.
            anthropic_authorizer_ref = next(
                (ref for name, ref in llm_authorizers if name == ANTHROPIC_AUTHORIZER_NAME),
                None,
            )

            # Renderer dispatcher: a NixConfigurableDispatchImpl over the
            # scaffolded workdir repo, auto-loading the ``default`` preset at
            # ``head`` so the agent panel renders on boot. The agent re-pushes
            # the same attr (after editing a renderer / the flake + committing)
            # via set_preset(current_commit(), …). Replaces the old
            # demo_dispatch fork. **First boot is slow** — the auto-load batch-
            # builds every renderer ESM + typeEntity layer (offloaded to the
            # nix builder); cached thereafter.
            #
            # Report builder service. Stateless: each invoke shells
            # out to the MDX TS subprocess and returns the resulting
            # RootReport / SubReport entity.
            #
            async with (
                mount_nix_configurable_dispatch(
                    repo=workdir,
                    initial_commit=head,
                    initial_attr="fxtr.uiPresets.default",
                ) as renderer_dispatcher,
                mount_report_builder() as report_builder,
                _maybe_mount_docent(enable_docent) as docent_proxy,
                mount_demo_helper(
                    workdir=workdir,
                    head_commit=head,
                    spawner_ref=sandbox_env.spawner_ref,
                    host_scope=sandbox_env.host_scope,
                    anthropic_authorizer=anthropic_authorizer_ref,
                ) as demo_helper,
            ):
                external_services = [
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
                    ExternalServiceConfig(
                        external_name=RENDERER_DISPATCHER_NAME,
                        service=renderer_dispatcher.service_ref,
                        scope=renderer_dispatcher.scope,
                    ),
                    ExternalServiceConfig(
                        external_name=REPORT_BUILDER_NAME,
                        service=report_builder.service_ref,
                        scope=report_builder.scope,
                    ),
                    ExternalServiceConfig(
                        external_name=DEMO_HELPER_NAME,
                        service=demo_helper.service_ref,
                        scope=demo_helper.scope,
                    ),
                    *(
                        ExternalServiceConfig(
                            external_name=name,
                            service=ref,
                            scope=sandbox_env.host_scope,
                        )
                        for name, ref in llm_authorizers
                    ),
                ]
                if docent_proxy is not None:
                    external_services.append(
                        ExternalServiceConfig(
                            external_name=DOCENT_PROXY_NAME,
                            service=docent_proxy.service_ref,
                            scope=docent_proxy.scope,
                        )
                    )

                persistence_namespaces = [
                    PersistenceNamespaceConfig(
                        external_name=AGENT_MCP_STORE_NAME,
                        namespace=agent_mcp_store_ns,
                        mode="preserve_provenance",
                    ),
                ]

                async with WorkstationServer(
                    port=_WORKSTATION_PORT,
                    debug_ui_dist_path=WorkstationServer.bundled_debug_ui_dist_path(),
                    external_services=external_services,
                    persistence_namespaces=persistence_namespaces,
                ) as ws:
                    # Seed Note + Transcript entities over peer-RPC
                    # under the external names ``"demo-note"`` and
                    # ``"demo-transcript"``. Lets you poke at the
                    # dynamic-dispatch renderers without first
                    # having to drive the agent into producing
                    # them. The CM stays open for the
                    # workstation's lifetime so the registrations
                    # stick.
                    async with _seed_demo_entities(ws.rpc_url):
                        # Human-readable summary (no machine protocol — this
                        # process is the orchestrator now, nothing parses
                        # stdout).
                        print()
                        print(f"  workstation client URL: {ws.client_url}")
                        print(f"  workstation debug UI:   {ws.debug_http_url}")
                        print(f"  runtime workdir repo:   {workdir} @ {head[:12]}")
                        print(
                            f"  persistence cap:        {AGENT_MCP_STORE_NAME!r} "
                            f"(preserve_provenance) at {_PERSISTENCE_DB_PATH}"
                        )
                        print(f"  external service:       {SANDBOX_SPAWNER_NAME!r}")
                        print(
                            f"  external service:       {INTERNET_AUTHORIZER_NAME!r} "
                            "(HTTPS passthrough)"
                        )
                        print(
                            f"  external service:       {RENDERER_DISPATCHER_NAME!r} "
                            "(nix preset 'default', auto-loaded by git ref)"
                        )
                        print(
                            f"  external service:       {REPORT_BUILDER_NAME!r} "
                            "(MDX → RootReport / SubReport)"
                        )
                        print(
                            f"  external service:       {DEMO_HELPER_NAME!r} "
                            "(self-contained driver: open it in the shell to spawn +"
                            " drive demo services)"
                        )
                        if docent_proxy is not None:
                            print(
                                f"  external service:       {DOCENT_PROXY_NAME!r} "
                                "(read-only Docent SDK proxy: DQL + metadata)"
                            )
                        print(
                            "  seeded entity:          'demo-note' (Note from"
                            " presentables/) — for testing the dynamic renderer"
                        )
                        print(
                            "  seeded entity:          'demo-transcript' (Transcript"
                            " from transcripts/) — for testing the dynamic renderer"
                        )
                        for name, _ref in llm_authorizers:
                            print(
                                f"  external service:       {name!r} (API key injected" " on host)"
                            )
                        print()
                        print(
                            f"Point the MCP plugin / Claude at {workdir} (cp"
                            " .fxtr_workstation.toml.example .fxtr_workstation.toml"
                            " there)."
                        )
                        print("Ctrl-C to shut down.")
                        print(flush=True)

                        # Spawn the Vite frontend (unless --no-frontend) +
                        # park until SIGINT — inside the seed CM so the demo
                        # entity registrations stay alive for the
                        # workstation's lifetime.
                        async with _maybe_run_vite(run_frontend, ws.client_url, ws.debug_http_url):
                            with anyio.open_signal_receiver(
                                signal.SIGINT, signal.SIGTERM
                            ) as signals:
                                async for sig in signals:
                                    print(
                                        f"Received signal {sig}; shutting down.",
                                        flush=True,
                                    )
                                    break


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Demo workstation for the assembly-stage demo.",
    )
    workdir_group = parser.add_mutually_exclusive_group(required=True)
    workdir_group.add_argument(
        "--workdir",
        type=pathlib.Path,
        help=(
            "The scaffolded runtime workdir repo (from scaffold_workdir.py). "
            "The sandbox spawner + the renderer dispatcher resolve git refs "
            "(services / the UI preset) against it."
        ),
    )
    workdir_group.add_argument(
        "--auto-workdir",
        action="store_true",
        help=(
            f"Re-scaffold a throwaway runtime workdir at {_AUTO_WORKDIR_PATH} "
            "from the in-repo template on every launch (overwriting whatever is "
            "there), instead of pointing at a pre-scaffolded --workdir. Lets you "
            "launch the demo with one command for self-contained UI testing."
        ),
    )
    parser.add_argument(
        "--no-frontend",
        action="store_true",
        help="Don't spawn the Vite dev server (headless / debug-UI-only).",
    )
    parser.add_argument(
        "--allow-llm-apis",
        action="store_true",
        help=(
            "Bridge in host-side authorizers for api.anthropic.com / "
            "api.openai.com when the corresponding ANTHROPIC_API_KEY / "
            "OPENAI_API_KEY env vars are set. Only the ones whose keys "
            "are present get registered; a warning is printed if you "
            "pass the flag with neither set."
        ),
    )
    parser.add_argument(
        "--enable-docent",
        action="store_true",
        help=(
            "Mount the read-only docent-fxtr-proxy service (DQL + metadata "
            "queries). Off by default. Auth picks up docent.env from the "
            "launch CWD via the Docent SDK."
        ),
    )
    args = parser.parse_args()

    anyio.run(
        _run,
        args.workdir.resolve() if args.workdir is not None else None,
        args.auto_workdir,
        args.allow_llm_apis,
        not args.no_frontend,
        args.enable_docent,
    )


if __name__ == "__main__":
    main()
