"""Dialect fixture backend for the dashboard's round-trip harness.

A tiny stateless HTTP backend (the `fxtr2.experimental.http.service_*`
layer, patterned on `examples/fxtr_http_demo/server/demo_backend.py`)
serving **representative values of the real `fxtr_experiment` wire
dataclasses** through the real service-dialect encode — the Python half
of the live python↔typescript dialect test in
`projects/fxtr_experiment/frontend/tests/dialect.test.ts` (see the
chunk-4 plan for the design). The vitest spawns this script, connects
with `@fxtr-ui/connector-http`, decodes every served shape into the TS
wire types, and drives the **encode direction** through the echo
methods (a TS-built `ViewSel` / round-tripped `TargetRecord` decoded
here through the same per-annotation dialect codec typed dispatch
uses — the dispatch *binding* layer itself is exercised by the live
demo, not this harness).

Handlers are hand-rolled POST fns (the http service layer doesn't host
`TypedServiceImpl`s): each wraps `encode_to_service_data` /
`decode_from_service_data` — the exact codec the typed-service layer
uses — around canned instances. Entity-id leaves reference entities
pinned in this backend's store, so the TS side receives real, loadable
`EntityRefStub`s.

Run from this package's env: ``uv run python dialect_fixture_backend.py``
(prints ``READY <baseUrl>``, exits gracefully on SIGTERM/SIGINT).
"""

from __future__ import annotations

import signal
import socket
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import anyio
import fxtr2.data.system_entities  # noqa: F401 — registers the `ctx` builtin handler
import hypercorn.asyncio
import hypercorn.config
from frozendict import frozendict
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import GLOBAL_SCOPE, FxtrScope
from fxtr2.experimental.http.asgi_app import create_fxtr_app
from fxtr2.experimental.http.service_registry import ServiceRegistry
from fxtr2.services.service_dialect import decode_from_service_data, encode_to_service_data
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.routing import Route

from fxtr_experiment import encoding as enc
from fxtr_experiment_task import model as m
from fxtr_experiment import user_api as ua
from fxtr_experiment import watch_api as wa
from fxtr_experiment_task.runner_api import ViewGrant, ViewSel, ViewToken


def _fixture_entities(scope: FxtrScope) -> dict[str, FxtrID]:
    """A few tiny pinned entities so id leaves are real + loadable."""
    ids: dict[str, FxtrID] = {}
    for name in ("config-a", "config-b", "value-a"):
        ids[name] = (
            enc.SliceTemplateEntity(template=m.SliceTemplate(path=(), target_axes=(), output=name))
            .store(scope)
            .entity_id
        )
    return ids


def _fixture_values(ids: dict[str, FxtrID]) -> dict[str, tuple[Any, Any]]:
    """Canned ``(value, annotation)`` per fixture method.

    Deliberately covers both the populated and the all-``None``-optional
    form of every dataclass family the dashboard consumes, plus the
    interesting strategies (results frozendicts, nested records,
    tuple-of-dataclass returns).
    """
    axes = (m.AxisSpec("x", "str"), m.AxisSpec("i", "int"))
    target = m.TargetRecord(
        slice=m.SliceTemplate(path=(), target_axes=(), output=None),
        expected_config=ids["config-a"],
    )
    key_infos = (
        ua.KeyInfo(
            key="t",
            kind="task",
            axes=axes,
            grounding="grounded",
            grounding_reason=None,
            config_id=ids["config-a"],
        ),
        ua.KeyInfo(key="d", kind="data", axes=()),
    )
    cell_infos = (
        ua.CellInfo(
            coords=("a", 3),
            status="errored",
            currency="stale",
            reason="boom",
            grounding="orphaned",
            wanted=False,
        ),
        ua.CellInfo(coords=()),
    )
    cell_reports = (
        ua.CellReport(
            status="complete",
            currency="fresh",
            results=frozendict({"result": ids["value-a"], "extra": ids["config-b"]}),
            grounding="grounded",
            wanted=True,
        ),
        ua.CellReport(value=ids["value-a"], bound=True),
        ua.CellReport(),
    )
    summary = ua.WorkflowSummary(
        grids=(
            ua.GridSummary(
                path='["t"]',
                kind="task",
                axes=axes,
                config_id=ids["config-a"],
                grounding="unanchored",
                grounding_reason="no claimant",
                sourcing="attached",
                populated=3,
                n_pending=0,
                n_running=1,
                n_complete=1,
                n_errored=1,
                n_interrupted=0,
                n_stale=2,
                n_unsettled=1,
                targets_attached=1,
                targets_dangling=0,
            ),
        )
    )
    generation = ua.GenerationReport(
        generation="0" * 32,
        status="interrupted",
        executed_fn=ids["config-b"],
        failures=(ua.FailureInfo(kind="fault", error_class="RunnerCrash", message="mid", ts="t0"),),
        observations=(
            ua.ObservationInfo(
                subject_path='["d"]',
                subject_coords='["a"]',
                subject_output="",
                kind="cell_value",
                fingerprint=ids["value-a"],
                current="changed",
            ),
        ),
        unanswered_requests=(ua.RequestInfo(grid_path='["u"]', coords="[]", kind="task_output"),),
        checkpoints=(
            ua.CheckpointInfo(
                grid_path='["t","steps"]',
                coords="[0]",
                step_type_id=ids["config-a"],
                args_id=ids["config-b"],
                value_id=ids["value-a"],
            ),
        ),
    )
    lint = ua.LintReport(
        dangling_sourcing=(
            ua.SourcingInfo(
                grid_path='["gone"]',
                coords="",
                fingerprint=ids["config-b"],
                status="dangling_missing",
            ),
        ),
        dangling_targets=(
            ua.TargetInfo(
                target=target, status="dangling_mismatched", grid_path='["gone"]', coords="[]"
            ),
        ),
        target_findings=(
            ua.TargetFinding(
                target=target,
                grid_path='["t"]',
                coords="[]",
                verdict="unanchored",
                reason="chain break",
            ),
        ),
        ungrounded=(
            ua.UngroundedFact(
                kind="task_cell",
                grid_path='["t"]',
                coords='["a",3]',
                verdict="orphaned",
                reason=None,
            ),
        ),
    )
    grants = (
        ViewGrant(
            token=ViewToken("tok-task"),
            kind="task",
            axes=axes,
            mode="read",
            outputs=None,
        ),
        ViewGrant(token=ViewToken("tok-coord"), kind="coord", axes=(), value=7),
    )
    observer_infos = (
        ua.ObserverInfo(grid_path='["t"]', coords='["a",3]', status="running", currency=None),
    )
    target_infos = (
        ua.TargetInfo(
            target=target,
            status="attached",
            grid_path='["t"]',
            coords='["a",3]',
            grid_grounding="grounded",
        ),
    )
    # TODO(chunk 5 — plans/phase6_sandboxing/chunk5_monitoring.md): both
    # canned bindings are ephemeral-class minimums; nothing here exercises
    # the sandboxed class (`deps` / `config` / `sandbox`, the `sandbox_*`
    # states). Widen when the panel renders them — and note the harness
    # cannot flag the gap on its own: it decodes structurally, so fields
    # the wire carries but the TS mirror omits pass silently.
    binding_infos = (
        ua.BindingInfo(
            key=ids["value-a"],
            state="external",
            cells=(ua.CellLocation(grid_path='["fns"]', coords='["solo"]'),),
        ),
        ua.BindingInfo(key=ids["config-b"], state="external", cells=()),
    )
    sourcing_infos = (
        ua.SourcingInfo(
            grid_path='["d"]', coords='["a"]', fingerprint=ids["value-a"], status="attached"
        ),
    )
    # Watch events: one canned value per union VARIANT per family, so the
    # TS mirrors of `watch_api.py` are pinned like every other family.
    workflow_events = (
        wa.WorkflowBatch(
            patches=(
                wa.SummaryUpsert(rows=summary.grids),
                wa.SummaryRemove(paths=('["gone"]',)),
                wa.LintUpdate(report=lint),
                wa.TargetsUpdate(targets=target_infos),
                wa.SourcingUpdate(records=sourcing_infos),
                wa.BindingsUpdate(bindings=binding_infos),
            )
        ),
        wa.WorkflowRecomputing(),
        wa.WorkflowBatch(patches=()),  # the settle marker form
    )
    cells_events = (
        wa.CellsBatch(
            patches=(
                wa.GridMeta(
                    path='["t"]',
                    config_id=ids["config-a"],
                    grounding="grounded",
                    grounding_reason=None,
                    sourcing="attached",
                    grant=ViewGrant(
                        token=ViewToken("tok-grid"),
                        kind="task",
                        axes=axes,
                        mode="read",
                        outputs=m.DEFAULT_TASK_OUTPUTS,
                    ),
                    parent_token=ViewToken("tok-parent"),
                ),
                wa.CellsUpsert(cells=cell_infos),
                wa.CellsRemove(coords=(("a", 3), ())),
            )
        ),
        wa.CellsBatch(
            # The root-grid meta form (no parent to address through).
            patches=(
                wa.GridMeta(
                    path='["root"]',
                    config_id=ids["config-b"],
                    grounding="unanchored",
                    grounding_reason="no claimant",
                    sourcing=None,
                    grant=ViewGrant(token=ViewToken("tok-root"), kind="subtree", axes=()),
                    parent_token=None,
                ),
            )
        ),
        wa.CellsRecomputing(),
        wa.CellsGone(),
    )
    cell_events = (
        wa.CellBatch(
            patches=(
                wa.CellReportUpdate(report=cell_reports[0]),
                wa.CellGenerationUpdate(report=generation),
                wa.CellObserversUpdate(observers=observer_infos),
            )
        ),
        wa.CellRecomputing(),
        wa.CellGone(),
        wa.CellLogReset(generation="1" * 32),
        wa.CellLogAppend(
            entries=(
                wa.LogEntry(
                    ts="t0",
                    entry_kind="step_started",
                    step=wa.StepRef(grid_path='["t","steps"]', coords="[0]"),
                    message="step started",
                ),
                wa.LogEntry(
                    ts="t1",
                    entry_kind="log",
                    step=wa.StepRef(grid_path='["t","steps"]', coords="[0]"),
                    message="hi from the step",
                ),
                wa.LogEntry(ts="t2", entry_kind="wrote_cell", step=None, message='wrote ["d"]'),
                wa.LogEntry(
                    ts="t3",
                    entry_kind="step_errored",
                    step=wa.StepRef(grid_path='["t","steps"]', coords="[1]"),
                    message="step raised: boom",
                ),
            )
        ),
    )
    grid_graph = ua.GridGraph(
        edges=(
            ua.GraphEdge(observer_path='["t"]', subject_path='["d"]', kind="read", count=2),
            ua.GraphEdge(observer_path='["t"]', subject_path='["fns"]', kind="read", count=1),
            ua.GraphEdge(observer_path='["t"]', subject_path='["out"]', kind="wrote_cell", count=1),
            ua.GraphEdge(
                observer_path='["t"]', subject_path='["made"]', kind="grid_assertion", count=1
            ),
        ),
        input_grants=(
            ua.InputGrantEdge(
                task_path='["t"]',
                target_path='["fns"]',
                arg_name="",
                is_task_fn=True,
                mode="read",
                kept=False,
            ),
            ua.InputGrantEdge(
                task_path='["t"]',
                target_path='["d"]',
                arg_name="inp",
                is_task_fn=False,
                mode="read",
                kept=True,
            ),
        ),
        correspondences=(
            ua.AxisCorrespondence(grid_a='["d"]', axis_a="x", grid_b='["t"]', axis_b="x"),
        ),
    )
    graph_events = (
        wa.GraphBatch(patches=(wa.GraphUpdate(graph=grid_graph),)),
        wa.GraphRecomputing(),
        wa.GraphBatch(patches=()),  # the settle marker form
    )
    return {
        "get_view_grants": (grants, tuple[ViewGrant, ...]),
        "get_key_infos": (key_infos, tuple[ua.KeyInfo, ...]),
        "get_cell_infos": (cell_infos, tuple[ua.CellInfo, ...]),
        "get_cell_reports": (cell_reports, tuple[ua.CellReport, ...]),
        "get_workflow_summary": (summary, ua.WorkflowSummary),
        "get_generation_report": (generation, ua.GenerationReport),
        "get_observer_infos": (observer_infos, tuple[ua.ObserverInfo, ...]),
        "get_target_infos": (target_infos, tuple[ua.TargetInfo, ...]),
        "get_binding_infos": (binding_infos, tuple[ua.BindingInfo, ...]),
        "get_sourcing_infos": (sourcing_infos, tuple[ua.SourcingInfo, ...]),
        "get_lint_report": (lint, ua.LintReport),
        "get_delete_report": (
            ua.DeleteReport(broken_referrers=('["t"]', '["u"]')),
            ua.DeleteReport,
        ),
        # Each event encoded EXACTLY as the watch engine publishes it
        # (per-event `encode_to_service_data(event, <family union>)`);
        # the tuple wrapper is only this fixture's transport.
        "get_workflow_watch_events": (workflow_events, tuple[wa.WorkflowWatchEvent, ...]),
        "get_cells_watch_events": (cells_events, tuple[wa.CellsWatchEvent, ...]),
        "get_cell_watch_events": (cell_events, tuple[wa.CellWatchEvent, ...]),
        "get_grid_graph": (grid_graph, ua.GridGraph),
        "get_graph_watch_events": (graph_events, tuple[wa.GraphWatchEvent, ...]),
    }


async def _amain() -> None:
    async with FxtrScope(name="dialect-fixture", isolated=True, include=[GLOBAL_SCOPE]) as scope:
        ids = _fixture_entities(scope)
        values = _fixture_values(ids)

        impl: dict[str, Any] = {}
        for name, (value, annotation) in values.items():

            async def handler(
                args: list[Any],
                kwargs: dict[str, Any],
                ctx: Any,
                *,
                _v: Any = value,
                _a: Any = annotation,
            ) -> Any:
                return encode_to_service_data(_v, _a)

            impl[name] = handler

        async def echo_sel(args: list[Any], kwargs: dict[str, Any], ctx: Any) -> Any:
            # The encode direction: decode the TS-built selection through
            # the same per-annotation dialect codec typed dispatch uses,
            # then re-encode — a wrong TS encoding fails the decode here.
            sel = decode_from_service_data(args[0], ViewSel)
            assert isinstance(sel, ViewSel)
            return encode_to_service_data(sel, ViewSel)

        async def echo_target(args: list[Any], kwargs: dict[str, Any], ctx: Any) -> Any:
            record = decode_from_service_data(args[0], m.TargetRecord)
            assert isinstance(record, m.TargetRecord)
            return encode_to_service_data(record, m.TargetRecord)

        impl["echo_sel"] = echo_sel
        impl["echo_target"] = echo_target

        reg = ServiceRegistry(scope)
        reg.add_rest_service(
            path="/svc/dialect",
            impl=impl,
            method="POST",
            discovery_name="dialect-fixture",
        )

        entity_app = create_fxtr_app(scope, ids)
        port = _free_port()
        done = anyio.Event()

        @asynccontextmanager
        async def _lifespan(_app: Starlette) -> AsyncGenerator[None, None]:
            print(f"READY http://127.0.0.1:{port}", flush=True)
            yield

        app = Starlette(
            routes=[
                *entity_app.routes,
                *reg.routes(),
                Route("/discovery", reg.discovery_handler),
            ],
            middleware=[
                Middleware(
                    CORSMiddleware,
                    allow_origins=["*"],
                    allow_methods=["*"],
                    allow_headers=["*"],
                )
            ],
            lifespan=_lifespan,
        )

        config = hypercorn.config.Config()
        config.bind = [f"127.0.0.1:{port}"]
        config.loglevel = "WARNING"

        async with anyio.create_task_group() as tg:

            async def _serve() -> None:
                await hypercorn.asyncio.serve(
                    app,  # type: ignore[arg-type]
                    config,
                    shutdown_trigger=done.wait,  # type: ignore[arg-type]
                )

            tg.start_soon(_serve)
            with anyio.open_signal_receiver(signal.SIGTERM, signal.SIGINT) as signals:
                async for _ in signals:
                    break
            done.set()


def _free_port() -> int:
    """Pick a currently-free localhost TCP port (small bind race window)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


def main() -> None:
    try:
        anyio.run(_amain)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
