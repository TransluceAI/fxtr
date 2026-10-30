"""The runner-side half of the task-fn service boundary: the run capability.

A task fn executes strictly *behind a ServiceRef*
(`runner_api.WorkflowTaskFnProto`), and every effectful grid op proxies
back through one mounted run capability (`runner_api.RunCapabilityProto`)
per run. This module is that capability's wire skin —
:class:`RunCapabilityImpl` mounts one
:class:`~fxtr_experiment.ops_engine.OpsEngine` (one generation) as the
run-capability service; every method forwards. Both sides of the boundary
are fxtr2 ``TypedServiceImpl``\\ s over the decorated wire protocols, so
all dataclass/frozendict conversion is codec-driven and nothing assumes
same-process execution.

The boundary's other halves live in their own modules: the **task-side**
adapter (`task_adapter.py` — ``@task_fn``, the context, output
normalization; importable by sandbox guests, so it never imports this
runner side), and the **binding authority** (`bindings.py` — what serves
a handle key; the executor and steps spawn through it). Run
*orchestration* — the per-run scope, the prologue fn read, invoking the
task fn, persisting results, terminal-status derivation — is the
executor's job (`executor.py`).

**Error transit.** Workflow errors raised runner-side (engine ops) cross
each service boundary as fxtr2 ``IRException`` with the original class
name as ``category``. That transit form is *informational only* — for
task-visible tracebacks and test assertions. Nothing durable may be
classified from it: poisoning persistence and the errored/interrupted
split happen runner-side, where the original typed exception (or the
engine's own record of it) is available (see `errors.py` and
`notes/execution_semantics.md`).
"""

from __future__ import annotations

from typing import Any, Mapping

from frozendict import frozendict

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import ServiceRef
from fxtr2.data.system_entities import define_type
from fxtr2.services.service_cap_common import UnknownService
from fxtr2.services.typed_service import TypedServiceImpl
from fxtr2.services.untyped_service import service_method

from fxtr_experiment_task._ns import WORKFLOWS3_NS
from fxtr_experiment_task.model import (
    AxisSpec,
    FrameSchema,
    OutputSpec,
)
from fxtr_experiment.ops_engine import OpsEngine
from fxtr_experiment_task.runner_api import (
    RunCapabilityProto,
    ViewGrant,
    ViewSel,
    WiredArg,
    WiredCoord,
)

RUN_CAPABILITY_TYPE_ID = define_type(WORKFLOWS3_NS, "run_capability")


class RunCapabilityImpl(TypedServiceImpl[RunCapabilityProto]):
    """One generation's ops engine, mounted as the run-capability service.

    Pure forwarding: the engine owns all semantics (record-check,
    integrity, recording); this class is the wire skin. Mounted into the
    per-run scope by the executor.
    """

    def __init__(self, engine: OpsEngine) -> None:
        """Wrap ``engine`` (one generation) for mounting."""
        super().__init__()
        self._engine = engine

    def _target_type_annotation(self) -> Any:
        """The proxy type this service resolves as."""
        return RunCapabilityProto

    def _service_type_id(self) -> FxtrID:
        """The run-capability service type id (a registrant-controlled
        `define_type` id; the type entity lives in GLOBAL_SCOPE)."""
        return RUN_CAPABILITY_TYPE_ID

    @service_method
    async def read_cell_entity(self, sel: ViewSel) -> FxtrID:
        """Forward to :meth:`OpsEngine.read_cell_entity`."""
        return await self._engine.read_cell_entity(sel)

    @service_method
    async def read_cell_frame(self, sel: ViewSel) -> bytes:
        """Forward to :meth:`OpsEngine.read_cell_frame`."""
        return await self._engine.read_cell_frame(sel)

    @service_method
    async def read_handle_key(self, sel: ViewSel) -> FxtrID:
        """Forward to :meth:`OpsEngine.read_handle_key`."""
        return await self._engine.read_handle_key(sel)

    @service_method
    async def write_cell_entity(self, sel: ViewSel, value: FxtrID) -> None:
        """Forward to :meth:`OpsEngine.write_cell_entity`."""
        await self._engine.write_cell_entity(sel, value)

    @service_method
    async def write_cell_frame(self, sel: ViewSel, frame: bytes) -> None:
        """Forward to :meth:`OpsEngine.write_cell_frame`."""
        await self._engine.write_cell_frame(sel, frame)

    @service_method
    async def gather(self, sel: ViewSel, keys: bytes) -> bytes:
        """Forward to :meth:`OpsEngine.gather`."""
        return await self._engine.gather(sel, keys)

    @service_method
    async def open_data(self, subtree: ViewSel, key: str, axes: tuple[AxisSpec, ...]) -> ViewGrant:
        """Forward to :meth:`OpsEngine.open_data`."""
        return await self._engine.open_data(subtree, key, axes)

    @service_method
    async def open_dataframe(
        self,
        subtree: ViewSel,
        key: str,
        axes: tuple[AxisSpec, ...],
        schema: FrameSchema,
    ) -> ViewGrant:
        """Forward to :meth:`OpsEngine.open_dataframe`."""
        return await self._engine.open_dataframe(subtree, key, axes, schema)

    @service_method
    async def open_handle(
        self, subtree: ViewSel, key: str, axes: tuple[AxisSpec, ...]
    ) -> ViewGrant:
        """Forward to :meth:`OpsEngine.open_handle`."""
        return await self._engine.open_handle(subtree, key, axes)

    @service_method
    async def open_subtree(
        self, subtree: ViewSel, key: str, axes: tuple[AxisSpec, ...]
    ) -> ViewGrant:
        """Forward to :meth:`OpsEngine.open_subtree`."""
        return await self._engine.open_subtree(subtree, key, axes)

    @service_method
    async def open_checkpoint(
        self, subtree: ViewSel, key: str, axes: tuple[AxisSpec, ...]
    ) -> ViewGrant:
        """Forward to :meth:`OpsEngine.open_checkpoint`."""
        return await self._engine.open_checkpoint(subtree, key, axes)

    @service_method
    async def open_task(
        self,
        subtree: ViewSel,
        key: str,
        axes: tuple[AxisSpec, ...],
        task_fn: WiredArg,
        args: "Mapping[str, WiredArg | WiredCoord]",
        outputs: "frozendict[str, OutputSpec]",
    ) -> ViewGrant:
        """Forward to :meth:`OpsEngine.open_task`."""
        return await self._engine.open_task(subtree, key, axes, task_fn, args, outputs)

    @service_method
    async def run_step(
        self, checkpoint: ViewSel, step: ServiceRef[UnknownService], args: Mapping[str, Any]
    ) -> Any:
        """Forward to :meth:`OpsEngine.run_step`."""
        return await self._engine.run_step(checkpoint, step, args)

    @service_method
    async def log(self, message: str) -> None:
        """Forward to :meth:`OpsEngine.log` (the outer-level task log)."""
        await self._engine.log(message)
