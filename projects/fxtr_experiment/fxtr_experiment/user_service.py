"""The user surface: `user_api.UserCapabilityProto`'s impl + the `Workflow` wrapper.

:class:`UserCapabilityImpl` is the top-level gateway service (the sibling
of `task_service.RunCapabilityImpl`, one layer up): the same calculus over
the same wire types, with **user semantics** — no op record, no
record-check, no observations, no poisoning; failures surface as ordinary
exceptions to handle interactively; writes and creates auto-upsert
user *sourcing records* (the revocable root facts of
``notes/grounding.md``), under the grid-before-cell invariant (cell-level
intent — writes, targets — requires the grid's sourcing record; grids the
user creates get one automatically). It deliberately does *not* reuse
`ops_engine.OpsEngine` (whose whole body is the determinism machinery the
user sits outside of); what it shares with the engine is the resolution
and canonicalization layer — `spec_registry.SpecRegistry`, `view_spec`'s
``open_child`` / ``canonicalize_open_task``, `ops_engine.resolve_templates`
— and the executor's :meth:`~fxtr_experiment.executor.Executor.demand` as
the one materialization path (user reads of task outputs follow the demand
policy exactly like task reads).

:class:`Workflow` is the unprivileged convenience wrapper over one
*resolved* ``ServiceRef[UserCapabilityProto]`` proxy — an in-process mount
or a remote one; it cannot tell the difference and adds no capability.
Calculus ops flow through the **shared view classes** (`view_runtime`'s,
over :class:`_UserViewBackend` — the explicit adapter carrying the small
divergence between the seam's task-shaped signatures and the wire
protocol, so neither constrains the other); privileged ops are wrapper
methods taking views. :func:`open_local` is the local bootstrap: a plain async CM
that opens the two-file session, builds the runner (scope + bindings +
executor), mounts the capability, and yields the wrapper. Deliberately
*not* a mount-lifespan bootstrap: the capability impl stays dumb glue over
already-running machinery, so the runner's lifetime is never hostage to
one mount (a "workflow server from db paths" helper can wrap this CM in a
mount lifespan later, without wire changes).

Scope discipline notes (`v2/notes/scope_discipline.md`): every scope here
is isolated with explicit includes; the only active-scope uses are the
documented service-method dispatch exception (values arriving through a
call) and the user-boundary capture in :func:`open_local`'s caller.
"""

from __future__ import annotations

import json
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator, Literal, Mapping, cast

if TYPE_CHECKING:
    import polars as pl

import anyio.abc
import anyio.to_thread
from frozendict import frozendict
from fxtr_sandbox_types import JSONValue

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import (
    GLOBAL_SCOPE,
    FxtrScope,
    ServiceRef,
    get_active_scope,
)
from fxtr2.data.system_entities import define_type
from fxtr2.services.service_cap_common import FxtrServiceData, UnknownService
from fxtr2.services.subscription_types import Subscriber, UnsubscribeHandle
from fxtr2.services.typed_service import TypedServiceImpl
from fxtr2.services.untyped_service import service_method

from fxtr_experiment_task._ns import WORKFLOWS3_NS
from fxtr_experiment.encoding import (
    GridConfigEntity,
    SliceTemplateEntity,
    canonical_json_text,
    decode_coords,
    decode_key_path,
    encode_key_path,
    store_config,
)
from fxtr_experiment_task.errors import (
    CellFaultError,
    EncodingError,
    typed_wire_call,
    GridConfigMismatchError,
    GridNotSourcedError,
    HandleAlreadyBoundError,
    NotTargetedError,
    SandboxDependencyError,
    TargetConflictError,
    ViewAxisError,
    WriteConflictError,
)
from fxtr_experiment.executor import Executor
from fxtr_experiment.watch_engine import WatchEngine
from fxtr_experiment.export import write_export
from fxtr_experiment.importer import read_layout, verify_and_decode
from fxtr_experiment_task.model import (
    AxisKind,
    AxisSpec,
    CoordValue,
    DataFrameGridConfig,
    DataGridConfig,
    FrameSchema,
    GridConfig,
    HandleGridConfig,
    InternalsStepTemplate,
    KeepAxis,
    LiteralCoord,
    OutputSpec,
    SandboxDefn,
    SliceMode,
    SliceTemplate,
    SubtreeGridConfig,
    TargetRecord,
    TaskGridConfig,
    rule_reference_paths,
)
from fxtr_experiment.ops_engine import _gather_failed, resolve_templates
from fxtr_experiment_task.runner_api import ViewGrant, ViewSel, ViewToken, WiredArg, WiredCoord
from fxtr_experiment.spec_registry import SpecRegistry
from fxtr_experiment.state_store import SandboxDepRow, WorkflowSession
from fxtr_experiment.bindings import BindingHost, BindingRelease
from fxtr_experiment.user_api import (
    AxisCorrespondence,
    BindingInfo,
    BindingReleaseProto,
    BindingState,
    CellInfo,
    CellLocation,
    CellReport,
    CheckpointInfo,
    DeleteReport,
    ExportReport,
    FailureInfo,
    GenerationReport,
    GraphEdge,
    GridGraph,
    GridSummary,
    ImportReport,
    InputGrantEdge,
    KeyInfo,
    LintReport,
    ObservationInfo,
    ObserverInfo,
    PinStatus,
    RequestInfo,
    SandboxBindingInfo,
    SandboxDepInfo,
    SourcingInfo,
    SourcingStatus,
    TargetFinding,
    TargetInfo,
    TargetStatus,
    UngroundedFact,
    UserCapabilityProto,
    WorkflowSummary,
)
from fxtr_experiment_task.view_runtime import ViewBackend, build_view
from fxtr_experiment_task.view_spec import (
    CoordViewSpec,
    GridKind,
    StoredViewSpec,
    SubtreeViewSpec,
    TaskViewSpec,
    ViewSpec,
    open_child,
)
from fxtr_experiment_task.views import CoordView, GridView, HandleView, SubtreeView

USER_CAPABILITY_TYPE_ID = define_type(WORKFLOWS3_NS, "user_capability")
BINDING_RELEASE_TYPE_ID = define_type(WORKFLOWS3_NS, "binding_release")

_ROOT_SPEC = SubtreeViewSpec(path=(), target_axes=(), axes=(), mode="write")


def _match_templates(spec: StoredViewSpec, coords_text: str) -> tuple[CoordValue, ...] | None:
    """Match one stored cell against a (possibly open) user selection.

    Args:
        spec: The sliced user spec (root frame: literals and keeps only).
        coords_text: The cell's canonical flattened-coords text.

    Returns:
        The cell's values at the spec's *open* positions, in positional
        order — or ``None`` if a literal position disagrees.
    """
    templates = [t for step in spec.path for t in step.cell] + list(spec.target_axes)
    values = decode_coords(coords_text)
    open_values: list[CoordValue] = []
    for template, value in zip(templates, values, strict=True):
        if isinstance(template, LiteralCoord):
            if template.value != value:
                return None
        elif isinstance(template, KeepAxis):
            open_values.append(value)
        else:  # Param cannot appear: the user's flattened frame is empty
            raise ViewAxisError("user selections cannot contain Param templates")
    return tuple(open_values)


class _BindingReleaseImpl(TypedServiceImpl[BindingReleaseProto]):
    """One binding's release capability (contract:
    `user_api.BindingReleaseProto`).

    Mounted on the *dispatch call scope* of the ``bind_handle`` call — the
    scope the framework keeps alive exactly as long as the caller holds
    the returned ref — with a mount lifespan whose teardown releases the
    binding. So: explicit ``release()`` is the immediate, graceful path;
    dropping the ref (or losing the scope that held it) auto-releases.
    **Intended semantics of the auto half**: drop-detection granularity is
    wherever the caller's ref bridge lives (in-process through
    ``open_local`` that is the runner scope; over RPC the client session
    scope; a caller wanting finer granularity resolves the capability in a
    narrower sub-scope and binds through that). The guarantee is that a
    release capability stranded in an already-closed scope can never leave
    a binding permanently stuck — NOT that teardown is prompt when
    everything shares one scope. The binding additionally stays bounded by
    the connector service's own lifetime.
    """

    def __init__(self, release: BindingRelease) -> None:
        """Wrap the in-runner release handle."""
        super().__init__()
        self._release = release

    @asynccontextmanager
    async def _fxtr_mount_lifespan(self, scope: FxtrScope) -> "AsyncIterator[None]":
        """Auto-release on mount teardown (see the class docstring).

        Teardown runs unauthorized after the mount scope drains —
        ``release()`` is a pure registry pop, so that is safe.
        """
        try:
            yield None
        finally:
            self._release.release()

    def _target_type_annotation(self) -> Any:
        """The proxy type this service resolves as."""
        return BindingReleaseProto

    def _service_type_id(self) -> FxtrID:
        """The binding-release service type id."""
        return BINDING_RELEASE_TYPE_ID

    @service_method
    async def release(self) -> None:
        """Contract on user_api.BindingReleaseProto.release (idempotent)."""
        self._release.release()


class UserCapabilityImpl(TypedServiceImpl[UserCapabilityProto]):
    """THE user capability (contract: `user_api.UserCapabilityProto`).

    Holds its own :class:`~fxtr_experiment.spec_registry.SpecRegistry`
    (tokens are per-mount-session vocabulary, same as a run's), the open
    session, the executor (every task-output read is a demand), the
    binding host, and the runner scope.

    Attributes:
        _session: The open two-file durable session.
        _executor: The runner's executor (demand policy + runs).
        _bindings: The runner's binding host (`bindings.BindingHost`).
        _scope: The runner scope — long-lived home for mounted release
            capabilities; includes the session's entity namespace, so ids
            this impl serves resolve below it (see
            ``WorkflowSession.ensure_loadable``).
        _registry: This capability's token ↔ true-spec registry.
    """

    def __init__(
        self,
        session: WorkflowSession,
        executor: Executor,
        bindings: BindingHost,
        scope: FxtrScope,
    ) -> None:
        """Wire the capability to one runner's machinery (see class doc)."""
        super().__init__()
        self._session = session
        self._executor = executor
        self._bindings = bindings
        self._scope = scope
        self._registry = SpecRegistry()
        self._root_grant = self._registry.mint(_ROOT_SPEC)
        # Serializes the cross-class exclusivity check + mutate across both
        # binding classes: the ephemeral map is in-memory (the host), the
        # sandboxed records are in the db (session), so only a lock spanning
        # both binds keeps a concurrent bind_handle / bind_sandbox for one
        # key from double-binding. Removals free a key and need no lock.
        self._bind_lock = anyio.Lock()

    def _target_type_annotation(self) -> Any:
        """The proxy type this service resolves as."""
        return UserCapabilityProto

    def _service_type_id(self) -> FxtrID:
        """The user-capability service type id."""
        return USER_CAPABILITY_TYPE_ID

    @asynccontextmanager
    async def _fxtr_mount_lifespan(self, scope: FxtrScope) -> "AsyncIterator[WatchEngine]":
        """Per-mount watch machinery (``notes/event_system.md``): the
        recompute-and-diff engine + its worker, wired to the session's
        post-commit hint seam, the binding registry's change callback,
        and the run-log registry's append listener for the mount's
        lifetime. The ``watch_*`` methods reach it via
        ``_fxtr_get_lifespan_state()``.
        """
        async with anyio.create_task_group() as tg:
            engine = WatchEngine(tg, self)
            self._session.set_event_listener(engine.note_hints)
            self._bindings.set_change_listener(engine.note_bindings_changed)
            self._executor.run_log.set_listener(engine.note_log)
            try:
                yield engine
            finally:
                self._session.set_event_listener(None)
                self._bindings.set_change_listener(None)
                self._executor.run_log.set_listener(None)
                # Forceful teardown of every subscription (safe from the
                # unauthorized post-drain context), then end the worker.
                await engine.aclose()
                tg.cancel_scope.cancel()

    # -- shared resolution helpers ------------------------------------------------

    def _resolve_cell(self, spec: ViewSpec) -> tuple[str, str]:
        """One fully-bound selection → (grid path, coords) texts.

        The user's flattened frame is empty (root frame), so there are no
        holder coords to resolve Params against. Coord specs are rejected
        (defensive: the user registry never mints coord tokens — user-side
        coord views are local and token-less — so only a fabricated wire
        selection could arrive here).
        """
        if isinstance(spec, CoordViewSpec):
            raise ViewAxisError("coord views address no stored cells")
        return resolve_templates(spec.path, spec.target_axes, ())

    async def _require_grid_sourced(self, grid_path: str, doing: str) -> None:
        """The grid-before-cell invariant's user-surface pre-check.

        Raises:
            GridNotSourcedError: No grid-level sourcing record (grids the
                user created have one automatically; adopt others first).
        """
        if await self._session.get_user_sourcing(grid_path, "") is None:
            raise GridNotSourcedError(
                f"cannot {doing}: grid {grid_path} has no user sourcing record"
                " (source/adopt the grid first — user creates record one"
                " automatically)"
            )

    # -- calculus ops ----------------------------------------------------------------

    @service_method
    async def root(self) -> ViewGrant:
        """Contract on user_api.UserCapabilityProto.root."""
        return self._root_grant

    async def _demanded_output_id(self, spec: TaskViewSpec) -> FxtrID:
        """The shared task-output demand body of the two cell reads:
        target requirement, demand through the executor, output pick.
        Returns the raw result id (the caller serves it per its kind)."""
        assert spec.output is not None  # both callers checked
        grid_path, coords = self._resolve_cell(spec)
        # User demands require targets: the method declares the
        # intent, and state (e.g. wanted-via-another-reader) never
        # substitutes for a declaration the user didn't make.
        if not await self._session.has_attached_target(grid_path, coords):
            raise NotTargetedError(
                f"user demand of untargeted task cell {grid_path}{coords}"
                " (add a target — the Workflow demand wrapper does by"
                " default — or look via inspect_cell)"
            )
        results = await self._executor.demand(grid_path, coords)
        if spec.output not in results:
            raise CellFaultError(f"task cell {grid_path}{coords} has no output {spec.output!r}")
        return results[spec.output]

    @service_method
    async def read_cell_entity(self, sel: ViewSel) -> FxtrID:
        """Contract on user_api.UserCapabilityProto.read_cell_entity: data
        cells read live; task outputs are demands (the strict gate). Served
        ids are re-attached (`WorkflowSession.ensure_loadable`) so the
        return can cover them."""
        spec = self._registry.sliced(sel)
        if isinstance(spec, TaskViewSpec):
            if spec.output is None:
                raise ViewAxisError("task reads require an output selection")
            if self._registry.frame_schema(sel) is not None:
                # Frame ids never surface as entity values.
                raise ViewAxisError(
                    f"output {spec.output!r} is a frame output; read it as a"
                    " frame (read_table / read_cell_frame)"
                )
            return await self._session.ensure_loadable(await self._demanded_output_id(spec))
        if spec.kind != "data":
            raise ViewAxisError(f"cannot read a {spec.kind!r} view this way")
        grid_path, coords = self._resolve_cell(spec)
        # User reads follow the same gate (notes/grounding.md): serve-or-
        # fail from one snapshot under the executor's policy. inspect_cell
        # is the labeled escape for looking at ungrounded values.
        value = await self._session.gated_read_cell(
            grid_path, coords, strict=self._executor.grounding_strict
        )
        if value is None:
            raise CellFaultError(f"no value at {grid_path}{coords}")
        return await self._session.ensure_loadable(value)

    @service_method
    async def read_handle_key(self, sel: ViewSel) -> FxtrID:
        """Contract on user_api.UserCapabilityProto.read_handle_key (no
        record — top-level connect authority is the binding registry
        itself, not an observation set; the grounding read gate applies
        as to any user read)."""
        spec = self._registry.sliced(sel)
        if spec.kind != "handle":
            raise ViewAxisError(f"cannot read a {spec.kind!r} view as a handle")
        grid_path, coords = self._resolve_cell(spec)
        key = await self._session.gated_read_cell(
            grid_path, coords, strict=self._executor.grounding_strict
        )
        if key is None:
            raise CellFaultError(f"no handle at {grid_path}{coords}")
        return await self._session.ensure_loadable(key)

    @service_method
    async def read_cell_frame(self, sel: ViewSel) -> bytes:
        """Contract on user_api.UserCapabilityProto.read_cell_frame: the
        entity read's gates exactly (targets for task-output demands, the
        grounding read gate for dataframe cells), with the payload
        materialized through the session's projection — the frame entity
        id never crosses the wire."""
        spec = self._registry.sliced(sel)
        if isinstance(spec, TaskViewSpec):
            if spec.output is None:
                raise ViewAxisError("task reads require an output selection")
            if self._registry.frame_schema(sel) is None:
                raise ViewAxisError(
                    f"output {spec.output!r} is an entity output; read it as"
                    " an entity (read / read_cell_entity)"
                )
            return await self._session.frame_bytes(await self._demanded_output_id(spec))
        if spec.kind != "dataframe":
            raise ViewAxisError(f"cannot read a {spec.kind!r} view as a frame")
        grid_path, coords = self._resolve_cell(spec)
        # The same gate as entity data reads (notes/grounding.md).
        value = await self._session.gated_read_cell(
            grid_path, coords, strict=self._executor.grounding_strict
        )
        if value is None:
            raise CellFaultError(f"no value at {grid_path}{coords}")
        return await self._session.frame_bytes(value)

    @service_method
    async def write_cell_entity(
        self, sel: ViewSel, value: FxtrID, overwrite: bool = False, adopt: bool = False
    ) -> None:
        """Contract on user_api.UserCapabilityProto.write_cell_entity (data
        and handle grants; user-sourced — idempotent same-value writes
        included, since re-writing a value claims it; ``overwrite``
        replaces; ``adopt`` sources an unsourced grid first)."""
        # Active scope: the dispatch scope carrying the decoded value id's
        # arena (the documented service-method exception).
        values_scope = get_active_scope()
        spec = self._registry.sliced(sel)
        if spec.kind not in ("data", "handle"):
            raise ViewAxisError(f"cannot write a {spec.kind!r} view")
        if spec.mode != "write":
            raise ViewAxisError("view is not writable (read mode)")
        grid_path, coords = self._resolve_cell(spec)
        # Writing is sourcing, and cell sourcing requires grid sourcing —
        # checked before the write so a refusal leaves no orphan value.
        if adopt and await self._session.get_user_sourcing(grid_path, "") is None:
            await self._adopt_grid(grid_path)
        await self._require_grid_sourced(grid_path, "write a cell")
        existing = await self._session.read_cell(grid_path, coords)
        if existing is not None and existing != value and not overwrite:
            raise WriteConflictError(
                f"cell {grid_path}{coords} already holds a different value"
                " (pass overwrite=True to replace it)"
            )
        # Membership: `value` was decoded into the dispatch scope, so its
        # closure is pinned from there (persist-then-record inside).
        # (The write body is a no-op on an identical value; the sourcing
        # upsert still runs — writing is sourcing whether bytes changed.)
        await self._session.write_cell(grid_path, coords, value, from_scope=values_scope)
        await self._session.upsert_user_sourcing(grid_path, coords, value)

    async def _adopt_grid(self, grid_path: str) -> None:
        """Upsert the grid sourcing record pinned to the CURRENT config
        (adoption), refusing task-internals grids (a task's private state
        is never user-ownable — see `user_api.UserCapabilityProto.
        source_grid`). The internals check walks stored kinds: any proper
        prefix grid of kind ``task`` makes every deeper grid internals."""
        keys = decode_key_path(grid_path)
        for depth in range(1, len(keys)):
            prefix = await self._session.get_grid(encode_key_path(keys[:depth]))
            if prefix is not None and prefix[0] == "task":
                raise ViewAxisError(
                    f"grid {grid_path} lies in task internals; internals are"
                    " never user-sourceable"
                )
        row = await self._session.get_grid(grid_path)
        if row is None:
            raise CellFaultError(f"no grid at {grid_path}")
        await self._session.upsert_user_sourcing(grid_path, "", row[1])

    @service_method
    async def write_cell_frame(
        self, sel: ViewSel, frame: bytes, overwrite: bool = False, adopt: bool = False
    ) -> None:
        """Contract on user_api.UserCapabilityProto.write_cell_frame: the
        entity write's sourcing semantics with identity minted here,
        host-side, under the grid's declared schema — the sourcing record
        pins the canonical frame entity id."""
        # Active scope: the dispatch scope (the documented service-method
        # exception) — where the built frame entity is staged.
        values_scope = get_active_scope()
        spec = self._registry.sliced(sel)
        if spec.kind != "dataframe":
            raise ViewAxisError(f"cannot write a {spec.kind!r} view as a frame")
        if spec.mode != "write":
            raise ViewAxisError("view is not writable (read mode)")
        schema = self._registry.frame_schema(sel)
        if schema is None:  # mint invariant: dataframe grants carry one
            raise ViewAxisError("dataframe grant carries no declared schema")
        # Lazy import: reaches the sole eager-polars module (`tables`).
        from fxtr_experiment.frame_encoding import canonical_frame_from_ipc

        try:
            entity = canonical_frame_from_ipc(frame, schema)
        except EncodingError as exc:
            raise ViewAxisError(f"frame rejected by the declared schema: {exc}") from exc
        # Content addressing: store into the dispatch scope to learn the
        # id (persist-then-record runs inside write_cell below).
        value_id = entity.store(scope=values_scope).entity_id
        grid_path, coords = self._resolve_cell(spec)
        # Same sourcing order as the entity write: grid-before-cell,
        # checked before the write so a refusal leaves no orphan value.
        if adopt and await self._session.get_user_sourcing(grid_path, "") is None:
            await self._adopt_grid(grid_path)
        await self._require_grid_sourced(grid_path, "write a cell")
        existing = await self._session.read_cell(grid_path, coords)
        if existing is not None and existing != value_id and not overwrite:
            raise WriteConflictError(
                f"cell {grid_path}{coords} already holds a different value"
                " (pass overwrite=True to replace it)"
            )
        # Membership: the entity was staged into the dispatch scope above,
        # so its (trivial, leaf) closure persists from there.
        await self._session.write_cell(grid_path, coords, entity, from_scope=values_scope)
        await self._session.upsert_user_sourcing(grid_path, coords, value_id)

    @service_method
    async def gather(self, sel: ViewSel, keys: bytes) -> bytes:
        """Contract on user_api.UserCapabilityProto.gather: the run
        capability's wire dialect with the *user* per-cell rules — target
        + demand for task-output cells, the grounding read gate for
        dataframe cells — resolved concurrently, nothing recorded. Any
        per-cell failures surface as one synthesized
        :class:`~fxtr_experiment.errors.GatherFailedError` naming every
        failed cell (the run capability's signal shape, uniformly)."""
        spec = self._registry.sliced(sel)
        schema = self._registry.frame_schema(sel)
        if isinstance(spec, TaskViewSpec):
            if spec.output is None:
                raise ViewAxisError("task reads require an output selection")
            if schema is None:
                raise ViewAxisError(
                    f"output {spec.output!r} is an entity output; gather serves" " frames only"
                )
        elif spec.kind != "dataframe":
            raise ViewAxisError(f"cannot gather a {spec.kind!r} view")
        assert schema is not None  # both branches above guarantee it
        grant_axes = self._registry.spec_of(sel.token).axes
        open_positions = [p for p in range(len(grant_axes)) if p not in sel.bound]
        kinds: tuple[AxisKind, ...] = tuple(grant_axes[p].kind for p in open_positions)
        # Lazy import: the sole eager-polars module (chunk-2 doctrine).
        from fxtr_experiment_task import tables

        # (grid_path, coords) → (key tuple, the per-key sliced spec).
        cells: "dict[tuple[str, str], tuple[tuple[CoordValue, ...], object]]" = {}
        for key in tables.decode_key_table(keys, kinds):
            bound = dict(sel.bound) | dict(zip(open_positions, key))
            key_spec = self._registry.sliced(
                ViewSel(token=sel.token, bound=frozendict(bound), output=sel.output)
            )
            assert not isinstance(key_spec, CoordViewSpec)  # kind checked above
            cells[self._resolve_cell(key_spec)] = (key, key_spec)
        resolved: dict[tuple[str, str], FxtrID] = {}
        failures: "list[tuple[tuple[str, str, str], BaseException]]" = []
        out_name = (spec.output or "") if isinstance(spec, TaskViewSpec) else ""

        async def resolve_one(grid_path: str, coords: str, key_spec: object) -> None:
            try:
                if isinstance(key_spec, TaskViewSpec):
                    # The single-read rule, per cell: target required,
                    # demand through the executor (its gates check the
                    # snapshot).
                    resolved[(grid_path, coords)] = await self._demanded_output_id(key_spec)
                else:
                    value = await self._session.gated_read_cell(
                        grid_path, coords, strict=self._executor.grounding_strict
                    )
                    if value is None:
                        raise CellFaultError(f"no value at {grid_path}{coords}")
                    resolved[(grid_path, coords)] = value
            except Exception as exc:
                # Nothing records on this surface; collect the cause for
                # the one synthesized signal (cancellation passes through).
                failures.append(((grid_path, coords, out_name), exc))
                raise

        if cells:
            try:
                async with anyio.create_task_group() as tg:
                    for (grid_path, coords), (_, key_spec) in cells.items():
                        tg.start_soon(resolve_one, grid_path, coords, key_spec)
            except ExceptionGroup as eg:
                # Only all-Exception groups — exactly what resolve_one
                # collected; a cancellation-bearing BaseExceptionGroup
                # passes through untranslated (the engine op's group
                # discipline, same rationale).
                raise _gather_failed(failures) from eg
        parts = []
        for cell in sorted(cells):
            data = await self._session.frame_bytes(resolved[cell])
            parts.append((cells[cell][0], tables.ipc_to_frame(data)))
        return tables.frame_to_ipc(tables.stitch_gather(kinds, schema, parts))

    # -- opens -----------------------------------------------------------------------

    async def _open(
        self,
        subtree: ViewSel,
        key: str,
        axes: tuple[AxisSpec, ...],
        kind: GridKind,
        config: GridConfig,
        mode: 'SliceMode | Literal["inherit"]',
        *,
        create_handle: bool = False,
        schema: FrameSchema | None = None,
        outputs: "frozendict[str, OutputSpec] | None" = None,
        premade: ViewGrant | None = None,
    ) -> ViewGrant:
        """The user open path: create-or-attach on canonical config.

        The engine's `_open` minus record-check/frontier/observations,
        plus user rules: creates are user-sourced, and the user *may*
        create handle grids (the top-level authority-minting site) — but
        only deliberately, via ``open_handle``'s explicit ``create`` flag
        (an ordinary handle open of a missing grid faults like the
        task-side attach). ``mode`` is internal, never caller-chosen
        (opens follow the chain): ``"inherit"`` resolves to the subtree
        grant's own mode; ``open_task`` fixes read (task grants are
        read-shaped).
        """
        # Boundary capture: the dispatch scope stages the config entity.
        values_scope = get_active_scope()
        subtree_spec = self._registry.sliced(subtree)
        if mode == "inherit":
            # Non-subtree specs fall through as "write"; open_child rejects
            # them on subtree-ness before mode matters.
            mode = subtree_spec.mode if isinstance(subtree_spec, SubtreeViewSpec) else "write"
        child_spec = open_child(subtree_spec, key, axes, kind, mode=mode)  # validates
        grid_path = encode_key_path(tuple(step.key for step in child_spec.path))
        config_id = store_config(config, scope=values_scope)
        existing = await self._session.get_grid(grid_path)
        if existing is not None:
            if existing[1] != config_id:
                raise GridConfigMismatchError(
                    f"{grid_path} exists with a different canonical config"
                )
        else:
            if kind == "handle" and not create_handle:
                raise CellFaultError(
                    f"handle grid {grid_path} does not exist (authority"
                    " minting is deliberate: pass create=True to create it)"
                )
            if subtree_spec.mode != "write":
                raise ViewAxisError("subtree view does not carry create access")
            refs: tuple[str, ...] = ()
            if isinstance(config, TaskGridConfig):
                refs = tuple(encode_key_path(p) for p in rule_reference_paths(config.rule))
            await self._session.create_grid(
                grid_path,
                kind,
                GridConfigEntity(config=config),
                rule_refs=refs,
                from_scope=values_scope,
            )
            # User creates assert: auto-upsert the grid sourcing record.
            await self._session.upsert_user_sourcing(grid_path, "", config_id)
        if premade is not None:
            return premade
        return self._registry.mint(child_spec, schema=schema, outputs=outputs)

    @service_method
    async def open_data(self, subtree: ViewSel, key: str, axes: tuple[AxisSpec, ...]) -> ViewGrant:
        """Contract on user_api.UserCapabilityProto.open_data."""
        return await self._open(subtree, key, axes, "data", DataGridConfig(axes=axes), "inherit")

    @service_method
    async def open_dataframe(
        self,
        subtree: ViewSel,
        key: str,
        axes: tuple[AxisSpec, ...],
        schema: FrameSchema,
    ) -> ViewGrant:
        """Contract on user_api.UserCapabilityProto.open_dataframe."""
        return await self._open(
            subtree,
            key,
            axes,
            "dataframe",
            DataFrameGridConfig(axes=axes, schema=schema),
            "inherit",
            schema=schema,
        )

    @service_method
    async def open_handle(
        self, subtree: ViewSel, key: str, axes: tuple[AxisSpec, ...], create: bool = False
    ) -> ViewGrant:
        """Contract on user_api.UserCapabilityProto.open_handle (creation
        is deliberate: ``create=True``; the grant follows the chain)."""
        return await self._open(
            subtree,
            key,
            axes,
            "handle",
            HandleGridConfig(axes=axes),
            "inherit",
            create_handle=create,
        )

    @service_method
    async def open_subtree(
        self, subtree: ViewSel, key: str, axes: tuple[AxisSpec, ...]
    ) -> ViewGrant:
        """Contract on user_api.UserCapabilityProto.open_subtree. The child
        inherits the grant's mode (write grants open creation sites; read
        grants — e.g. an internals root — browse one level deeper)."""
        return await self._open(
            subtree, key, axes, "subtree", SubtreeGridConfig(axes=axes), "inherit"
        )

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
        """Contract on user_api.UserCapabilityProto.open_task (the shared
        canonicalizer runs first, exactly as on the run capability). Task
        grants are read-shaped, so the derivation asks for read — matching
        the run capability — which keeps the attach half legal through
        read-mode grants (creates still require create access)."""
        config, grant = self._registry.open_task(subtree, key, axes, task_fn, args, outputs)
        return await self._open(subtree, key, axes, "task", config, "read", premade=grant)

    @service_method
    async def attach(self, subtree: ViewSel, key: str) -> ViewGrant:
        """Contract on user_api.UserCapabilityProto.attach: the stored
        config is the whole truth — no proposed config, no record, no
        create. The grant follows the chain; under an internals root
        everything is read-mode (introspection, not intervention — see
        ``attach_internals``)."""
        subtree_spec = self._registry.sliced(subtree)
        if not isinstance(subtree_spec, SubtreeViewSpec):
            raise ViewAxisError(f"attach needs a subtree grant, got {subtree_spec.kind!r}")
        child_spec, config = await self._attached_child_spec(subtree_spec, key)
        return self._mint_config_grant(child_spec, config)

    async def _attached_child_spec(
        self, subtree_spec: SubtreeViewSpec, key: str
    ) -> "tuple[StoredViewSpec, GridConfig]":
        """The shared attach body (``attach`` / `_walk_path`): resolve
        ``key`` under the subtree against the stored config, refuse
        checkpoints, apply the chain-authority mode ceiling, and derive
        the child spec via ``open_child``."""
        in_internals = subtree_spec.internals or any(
            isinstance(s, InternalsStepTemplate) for s in subtree_spec.path
        )
        grid_path = encode_key_path(tuple(step.key for step in subtree_spec.path) + (key,))
        row = await self._session.get_grid(grid_path)
        if row is None:
            raise CellFaultError(f"no grid at {grid_path}")
        kind, config_id = row
        if kind == "checkpoint":
            raise ViewAxisError(
                "checkpoint entries are not attachable (step-history introspection"
                " is a separate affordance); list_keys shows them"
            )
        config = (await self._session.load_config(config_id)).config
        effective: SliceMode = (
            "read"
            # Internals are look-only from the top; task grants are
            # read-shaped; everything else follows the chain (child
            # authority never exceeds the grant's).
            if in_internals or kind == "task"
            else subtree_spec.mode
        )
        child_spec = open_child(subtree_spec, key, config.axes, cast(Any, kind), mode=effective)
        return child_spec, config

    def _mint_config_grant(self, child_spec: "StoredViewSpec", config: "GridConfig") -> ViewGrant:
        """Mint an attach-derived grant, threading the config's grant
        payload (dataframe schema / task outputs)."""
        return self._registry.mint(
            child_spec,
            schema=config.schema if isinstance(config, DataFrameGridConfig) else None,
            outputs=config.outputs if isinstance(config, TaskGridConfig) else None,
        )

    @staticmethod
    def _internals_subtree(spec: TaskViewSpec) -> SubtreeViewSpec:
        """Re-tag a task spec as its internals pseudo-subtree (the same
        layout `view_spec.internals_spec` grants a run): read-mode; any
        open task positions stay open — a read-sliced internals window
        across the selected cells."""
        return SubtreeViewSpec(
            path=spec.path,
            target_axes=spec.target_axes,
            axes=spec.axes,
            mode="read",
            internals=True,
        )

    @service_method
    async def attach_internals(self, task: ViewSel) -> ViewGrant:
        """Contract on user_api.UserCapabilityProto.attach_internals: the
        task selection re-tagged as its internals pseudo-subtree,
        read-mode. Bound selections browse one cell's key-space (the
        run-granted layout with literal coordinates in place of Params);
        open positions stay kept — the grid-level browsing window."""
        spec = self._registry.sliced(task)
        if not isinstance(spec, TaskViewSpec):
            raise ViewAxisError(f"attach_internals needs a task selection, got {spec.kind!r}")
        return self._registry.mint(self._internals_subtree(spec))

    async def _walk_path(self, keys: tuple[str, ...]) -> tuple[ViewGrant, ...]:
        """A chain of attaches from the root: every prefix grant in one
        walk, crossing task boundaries into internals transparently (the
        walk re-tags the task spec via `_internals_subtree`; the hop is
        not a returned prefix). The watch engine's grant-minting path —
        the grants a `watch_cells`/`watch_cell` payload carries come from
        exactly this walk, so their authority semantics match `attach`.
        Element 0 is the root grant; the LAST grant addresses ``keys``'
        grid and the second-to-last is its parent (grid-level mutation
        addressing)."""
        grants: list[ViewGrant] = [self._root_grant]
        spec: StoredViewSpec = _ROOT_SPEC
        for i, key in enumerate(keys):
            if isinstance(spec, TaskViewSpec):
                # The remaining keys live in this task grid's internals
                # (read-only from here down, exactly as attach_internals
                # grants it).
                spec = self._internals_subtree(spec)
            if not isinstance(spec, SubtreeViewSpec):
                raise ViewAxisError(
                    f"cannot walk into {encode_key_path(tuple(keys[:i]))}:"
                    f" a {spec.kind!r} grid has no children"
                )
            child_spec, config = await self._attached_child_spec(spec, key)
            grants.append(self._mint_config_grant(child_spec, config))
            spec = child_spec
        return tuple(grants)

    # -- sourcing (the editable root facts) ---------------------------------------

    @service_method
    async def source_grid(self, subtree: ViewSel, key: str) -> None:
        """Contract on user_api.UserCapabilityProto.source_grid (adoption:
        pin the current stored config; internals refused)."""
        subtree_spec = self._registry.sliced(subtree)
        if not isinstance(subtree_spec, SubtreeViewSpec):
            raise ViewAxisError(f"source_grid needs a subtree grant, got {subtree_spec.kind!r}")
        grid_path = encode_key_path(tuple(step.key for step in subtree_spec.path) + (key,))
        await self._adopt_grid(grid_path)

    @service_method
    async def source_cell(self, sel: ViewSel) -> None:
        """Contract on user_api.UserCapabilityProto.source_cell (adoption:
        pin the current value; the invariant requires the grid's record)."""
        spec = self._registry.sliced(sel)
        if spec.kind not in ("data", "handle"):
            raise ViewAxisError(f"cannot source a {spec.kind!r} cell")
        grid_path, coords = self._resolve_cell(spec)
        value = await self._session.read_cell(grid_path, coords)
        if value is None:
            raise CellFaultError(f"no value at {grid_path}{coords} to adopt")
        await self._session.upsert_user_sourcing(grid_path, coords, value)

    @service_method
    async def revoke_grid_sourcing(self, subtree: ViewSel, key: str, force: bool = False) -> bool:
        """Contract on user_api.UserCapabilityProto.revoke_grid_sourcing
        (the maintained invariant: refuses with live cell-level intent
        unless ``force`` cascades)."""
        subtree_spec = self._registry.sliced(subtree)
        if not isinstance(subtree_spec, SubtreeViewSpec):
            raise ViewAxisError(
                f"revoke_grid_sourcing needs a subtree grant, got {subtree_spec.kind!r}"
            )
        grid_path = encode_key_path(tuple(step.key for step in subtree_spec.path) + (key,))
        return await self._session.revoke_user_sourcing(grid_path, "", cascade=force)

    @service_method
    async def revoke_cell_sourcing(self, sel: ViewSel) -> bool:
        """Contract on user_api.UserCapabilityProto.revoke_cell_sourcing."""
        spec = self._registry.sliced(sel)
        if spec.kind not in ("data", "handle"):
            raise ViewAxisError(f"cannot revoke sourcing of a {spec.kind!r} cell")
        grid_path, coords = self._resolve_cell(spec)
        return await self._session.revoke_user_sourcing(grid_path, coords)

    @service_method
    async def list_sourcing(self) -> tuple[SourcingInfo, ...]:
        """Contract on user_api.UserCapabilityProto.list_sourcing."""
        infos = []
        for grid_path, coords, fingerprint in await self._session.list_user_sourcing():
            if coords == "":
                row = await self._session.get_grid(grid_path)
                live = None if row is None else row[1]
            else:
                live = await self._session.read_cell(grid_path, coords)
            status: SourcingStatus = (
                "dangling_missing"
                if live is None
                else "attached" if live == fingerprint else "dangling_mismatched"
            )
            infos.append(
                SourcingInfo(
                    grid_path=grid_path,
                    coords=coords,
                    # Served id: re-attach so the return can cover it.
                    fingerprint=await self._session.ensure_loadable(fingerprint),
                    status=status,
                )
            )
        return tuple(infos)

    async def _grid_sourcing_status(self, grid_path: str) -> "SourcingStatus | None":
        """One grid's grid-level sourcing attachment status (``None`` =
        no record) — `list_sourcing`'s derivation for a single grid; the
        watch engine's `GridMeta` field."""
        pin = await self._session.get_user_sourcing(grid_path, "")
        if pin is None:
            return None
        row = await self._session.get_grid(grid_path)
        if row is None:
            return "dangling_missing"
        return "attached" if row[1] == pin else "dangling_mismatched"

    # -- targets ---------------------------------------------------------------------

    def _slice_of(self, spec: ViewSpec) -> SliceTemplate:
        """A fully-resolved user spec as a root-frame slice template."""
        assert not isinstance(spec, CoordViewSpec)  # every caller resolved it first
        return SliceTemplate(
            path=spec.path,
            target_axes=spec.target_axes,
            output=spec.output if isinstance(spec, TaskViewSpec) else None,
        )

    async def _target_status(self, target: TargetRecord) -> TargetStatus:
        """Attachment status of one record against the live grids."""
        grid_path = encode_key_path(tuple(step.key for step in target.slice.path))
        row = await self._session.get_grid(grid_path)
        if row is None:
            return "dangling_missing"
        return "attached" if row[1] == target.expected_config else "dangling_mismatched"

    @service_method
    async def add_target(self, sel: ViewSel, adopt: bool = False) -> None:
        """Contract on user_api.UserCapabilityProto.add_target (fully
        bound; one record per slice identity; ``adopt`` sources an
        unsourced grid first — the explicit per-call opt-in)."""
        values_scope = get_active_scope()
        spec = self._registry.sliced(sel)
        grid_path, coords = self._resolve_cell(spec)  # raises on open positions
        if adopt and await self._session.get_user_sourcing(grid_path, "") is None:
            await self._adopt_grid(grid_path)
        await self._require_grid_sourced(grid_path, "target a cell")
        row = await self._session.get_grid(grid_path)
        if row is None:
            raise CellFaultError(f"no grid at {grid_path}; targets pin an existing config")
        expected_config = row[1]
        slice_entity = SliceTemplateEntity(template=self._slice_of(spec))
        slice_id = slice_entity.store(scope=values_scope).entity_id
        existing = await self._session.target_for_slice(slice_id)
        if existing == expected_config:
            return  # idempotent
        if existing is not None:
            raise TargetConflictError(
                f"a target for this slice pins a different config"
                f" ({existing.id_string()}); remove it first"
            )
        # Liveness: the state db will reference the slice id — pin first.
        await self._session.persist(slice_id, from_scope=values_scope)
        await self._session.add_target(slice_id, expected_config, grid_path, coords)

    @service_method
    async def remove_target(self, sel: ViewSel) -> None:
        """Contract on user_api.UserCapabilityProto.remove_target (matches
        slice identity, any config vintage)."""
        values_scope = get_active_scope()
        spec = self._registry.sliced(sel)
        self._resolve_cell(spec)  # fully-bound check
        slice_id = (
            SliceTemplateEntity(template=self._slice_of(spec)).store(scope=values_scope).entity_id
        )
        await self._session.remove_targets_for_slice(slice_id)

    @service_method
    async def list_targets(self) -> tuple[TargetInfo, ...]:
        """Contract on user_api.UserCapabilityProto.list_targets."""
        infos = []
        for slice_id, expected_config, grid_path, coords in await self._session.list_targets():
            entity = await self._session.load_slice_template(slice_id)
            record = TargetRecord(
                slice=entity.template,
                # Served id: re-attach so the return can cover it.
                expected_config=await self._session.ensure_loadable(expected_config),
            )
            status = await self._target_status(record)
            grid_grounding = None
            if status == "attached":
                report = await self._session.grid_grounding(grid_path)
                grid_grounding = report.verdict if report is not None else None
            infos.append(
                TargetInfo(
                    target=record,
                    status=status,
                    grid_path=grid_path,
                    coords=coords,
                    grid_grounding=grid_grounding,
                )
            )
        return tuple(infos)

    @service_method
    async def demand_target(self, target: TargetRecord) -> "frozendict[str, FxtrID]":
        """Contract on user_api.UserCapabilityProto.demand_target."""
        status = await self._target_status(target)
        if status != "attached":
            raise CellFaultError(f"target is {status}; only attached targets can be demanded")
        grid_path, coords = resolve_templates(target.slice.path, target.slice.target_axes, ())
        row = await self._session.get_grid(grid_path)
        assert row is not None  # attached ⇒ the grid exists
        if row[0] == "task":
            results = await self._executor.demand(grid_path, coords)
            return frozendict(
                {
                    name: await self._session.ensure_loadable(value)
                    for name, value in results.items()
                }
            )
        value = await self._session.read_cell(grid_path, coords)
        if value is None:
            raise CellFaultError(f"no value at targeted cell {grid_path}{coords}")
        return frozendict({"": await self._session.ensure_loadable(value)})

    # -- privileged mutation -----------------------------------------------------------

    @service_method
    async def clear_cells(self, sel: ViewSel) -> None:
        """Contract on user_api.UserCapabilityProto.clear_cells (atomic:
        any addressed cell running → the whole call refuses)."""
        spec = self._registry.sliced(sel)
        if spec.kind != "task":
            raise ViewAxisError(f"clear_cells needs a task grant, got {spec.kind!r}")
        grid_path = encode_key_path(tuple(step.key for step in spec.path))
        exec_ids = [
            exec_id
            for exec_id, coords, _status in await self._session.list_generations(grid_path)
            if _match_templates(spec, coords) is not None
        ]
        # The running re-check happens inside the one deleting transaction
        # (`WorkflowSession.clear_generations`) — this call cannot be
        # half-applied around a concurrent demand.
        await self._session.clear_generations(exec_ids)

    @service_method
    async def delete_cell(self, sel: ViewSel) -> bool:
        """Contract on user_api.UserCapabilityProto.delete_cell (a user
        settle event like overwrite: observers go stale, the sourcing
        record dangles, bindings are untouched; task cells refuse —
        clear_cells is their counterpart)."""
        spec = self._registry.sliced(sel)
        if spec.kind not in ("data", "dataframe", "handle"):
            raise ViewAxisError(
                f"cannot delete a {spec.kind!r} cell (task state is cleared via clear_cells)"
            )
        grid_path, coords = self._resolve_cell(spec)
        return await self._session.delete_cell(grid_path, coords)

    @service_method
    async def delete_grid(
        self, subtree: ViewToken, key: str, *, recursive: bool = False
    ) -> DeleteReport:
        """Contract on user_api.UserCapabilityProto.delete_grid (the
        session's guards refuse child grids [non-recursive] / running;
        ``recursive`` deletes the whole subtree; proceeds-and-reports the
        external referrers whose grounding flips)."""
        subtree_spec = self._registry.spec_of(subtree)
        if subtree_spec.kind != "subtree":
            raise ViewAxisError(f"delete_grid needs a subtree grant, got {subtree_spec.kind!r}")
        grid_path = encode_key_path(tuple(step.key for step in subtree_spec.path) + (key,))
        return DeleteReport(broken_referrers=await self._session.delete_grid(grid_path, recursive))

    # -- registration -------------------------------------------------------------------

    def _resolve_dep_sels(self, deps: "Mapping[str, ViewSel]") -> dict[str, tuple[str, str]]:
        """Resolve binding dependency selections to absolute handle cells
        (both bind methods' shared holder-side half: rejects coord/open/
        non-handle selections; the wire stays token-based)."""
        resolved: dict[str, tuple[str, str]] = {}
        for name, sel in deps.items():
            spec = self._registry.sliced(sel)
            if spec.kind != "handle":
                raise SandboxDependencyError(
                    f"dependency {name!r} is a {spec.kind!r} selection, not a handle cell"
                )
            resolved[name] = self._resolve_cell(spec)  # raises on open positions
        return resolved

    @service_method
    async def bind_handle(
        self,
        handle: FxtrID,
        connector: ServiceRef[UnknownService],
        deps: "Mapping[str, ViewSel]" = frozendict(),
        config: JSONValue = None,
    ) -> ServiceRef[BindingReleaseProto]:
        """Contract on user_api.UserCapabilityProto.bind_handle.

        Explicit ``release()`` is immediate; dropping the returned ref
        auto-releases via the release impl's mount-lifespan teardown (see
        `_BindingReleaseImpl` for the granularity semantics). The
        cross-class exclusivity check (vs a sandboxed binding) runs under
        the shared bind lock, so a concurrent ``bind_sandbox`` for the same
        key cannot slip past it. Dependencies pin exactly like
        ``bind_sandbox``'s (`WorkflowSession.pin_dependency_cells` — one
        snapshot, populated handle cells), stored on the in-memory entry.
        """
        # Active scope: the dispatch scope where `connector` was decoded —
        # the host bridges it into its own (runner) scope from there.
        call_scope = get_active_scope()
        resolved = self._resolve_dep_sels(deps)
        config_json = None if config is None else canonical_json_text(config)
        async with self._bind_lock:
            # Cross-class exclusivity: refuse if the key is sandbox-bound
            # (the same-class ephemeral double-bind is caught by the
            # host below). Both classes are checked-and-mutated under
            # one lock (see __init__).
            if await self._session.has_sandbox_binding(handle):
                raise HandleAlreadyBoundError(
                    f"handle {handle.id_string()} has a sandboxed binding;" " remove it first"
                )
            # Persist the handle entity (its closure rides this call's arena,
            # so the dispatch scope covers it): bindings stay ephemeral, but
            # `list_bindings` serves these ids back, and served ids must be
            # re-attachable — the liveness discipline extended to the one id
            # source the state db does not record.
            await self._session.persist(handle, from_scope=call_scope)
            pins = await self._session.pin_dependency_cells(resolved)
            release = await self._bindings.bind(
                handle,
                connector,
                connector_scope=call_scope,
                deps=pins,
                config_json=config_json,
            )
        # Membership: the release impl mounts on the *call scope*, which
        # the framework keeps alive while the caller holds the returned
        # ref — its mount-lifespan teardown is the auto-release (see
        # `_BindingReleaseImpl` for the intended semantics).
        return await call_scope.mount(_BindingReleaseImpl(release))

    @service_method
    async def bind_sandbox(
        self,
        handle: FxtrID,
        defn: SandboxDefn,
        deps: "Mapping[str, ViewSel]",
        config: JSONValue = None,
    ) -> None:
        """Contract on user_api.UserCapabilityProto.bind_sandbox.

        Resolves each dependency selection holder-side to an absolute
        handle cell (`_resolve_cell`, rejecting coord/open/non-handle
        selections), persists the bound key's closure (liveness), then —
        under the shared bind lock, so the cross-class check is atomic vs
        a concurrent ``bind_handle`` — records the durable binding. The
        session body reads + pins each dependency cell.
        """
        # Active scope: the dispatch scope where `handle`'s closure was
        # decoded — the source for the liveness persist below.
        call_scope = get_active_scope()
        resolved = self._resolve_dep_sels(deps)
        config_json = None if config is None else canonical_json_text(config)
        async with self._bind_lock:
            if self._bindings.is_bound(handle):
                raise HandleAlreadyBoundError(
                    f"handle {handle.id_string()} has an ephemeral binding;" " release it first"
                )
            # Liveness: the state db will reference the bound key — pin its
            # closure (from the dispatch arena) first. Dependency pins come
            # from cells, already persisted. `add_sandbox_binding` re-checks
            # the sandbox class under the session lock (+ the key_id PK).
            await self._session.persist(handle, from_scope=call_scope)
            await self._session.add_sandbox_binding(handle, defn, resolved, config_json)

    @service_method
    async def remove_sandbox_binding(self, handle: FxtrID) -> None:
        """Contract on user_api.UserCapabilityProto.remove_sandbox_binding
        (a plain state mutation; no lock — removal only frees a key). The
        cached process, if any, is dropped release-and-forget style after
        the record delete (already-connected guests drain naturally)."""
        await self._session.remove_sandbox_binding(handle)
        await self._bindings.drop_process(handle)

    @service_method
    async def shutdown_sandbox(self, handle: FxtrID) -> None:
        """Contract on user_api.UserCapabilityProto.shutdown_sandbox
        (release-and-forget — `bindings.BindingHost.shutdown_sandbox`)."""
        await self._bindings.shutdown_sandbox(handle)

    @service_method
    async def watch_workflow(
        self, subscriber: "ServiceRef[Subscriber[FxtrServiceData]]"
    ) -> "ServiceRef[UnsubscribeHandle]":
        """Contract on user_api.UserCapabilityProto.watch_workflow."""
        # Active scope: the dispatch scope the subscriber ref arrived in
        # — the hub bridges the subscriber from (and the unsubscribe
        # handle into) it, per SubscriberHub.subscribe's contract.
        engine = cast(WatchEngine, self._fxtr_get_lifespan_state())
        return await engine.add_workflow_watch(subscriber, get_active_scope())

    @service_method
    async def watch_cells(
        self, subscriber: "ServiceRef[Subscriber[FxtrServiceData]]", *, path: str
    ) -> "ServiceRef[UnsubscribeHandle]":
        """Contract on user_api.UserCapabilityProto.watch_cells."""
        decode_key_path(path)  # malformed paths fail loudly at subscribe
        # Active scope: as in watch_workflow.
        engine = cast(WatchEngine, self._fxtr_get_lifespan_state())
        return await engine.add_cells_watch(path, subscriber, get_active_scope())

    @service_method
    async def watch_cell(
        self, subscriber: "ServiceRef[Subscriber[FxtrServiceData]]", *, path: str, coords: str
    ) -> "ServiceRef[UnsubscribeHandle]":
        """Contract on user_api.UserCapabilityProto.watch_cell."""
        decode_key_path(path)  # malformed addressing fails loudly
        decode_coords(coords)
        # Active scope: as in watch_workflow.
        engine = cast(WatchEngine, self._fxtr_get_lifespan_state())
        return await engine.add_cell_watch(path, coords, subscriber, get_active_scope())

    @service_method
    async def watch_graph(
        self, subscriber: "ServiceRef[Subscriber[FxtrServiceData]]"
    ) -> "ServiceRef[UnsubscribeHandle]":
        """Contract on user_api.UserCapabilityProto.watch_graph."""
        # Active scope: as in watch_workflow.
        engine = cast(WatchEngine, self._fxtr_get_lifespan_state())
        return await engine.add_graph_watch(subscriber, get_active_scope())

    @service_method
    async def list_bindings(self) -> tuple[BindingInfo, ...]:
        """Contract on user_api.UserCapabilityProto.list_bindings.

        Merges the ephemeral entries (``external``) and the durable
        sandboxed records into one key-id-sorted list. A key is bound in at
        most one class, so no dedup is needed. Both classes carry per-dep
        pin status derived against the live cells (one session snapshot);
        sandbox rows additionally reflect the runtime launch state (the
        host's process cache) folded with the repo-configured state.
        """
        ephemeral = self._bindings.bound_handles()
        sandbox_rows = await self._session.list_sandbox_bindings()
        all_keys = tuple(ephemeral) + tuple(row.key_id for row in sandbox_rows)
        # One snapshot for every key's grid-side context (both classes).
        holding = await self._session.handle_cells_holding(all_keys)
        repo_configured = self._bindings.sandbox_repo is not None

        async def dep_infos(deps: "tuple[SandboxDepRow, ...]") -> tuple[SandboxDepInfo, ...]:
            out: list[SandboxDepInfo] = []
            for dep in deps:
                live = await self._session.read_cell(dep.grid_path, dep.coords)
                pin_status: PinStatus = (
                    "missing"
                    if live is None
                    else "attached" if live == dep.pinned_key_id else "mismatched"
                )
                out.append(
                    SandboxDepInfo(
                        name=dep.name,
                        grid_path=dep.grid_path,
                        coords=dep.coords,
                        # Served id: re-attach (pinned key ids are persisted
                        # — the liveness invariant; both classes' pins come
                        # from cells).
                        pinned_key_id=await self._session.ensure_loadable(dep.pinned_key_id),
                        pin_status=pin_status,
                    )
                )
            return tuple(out)

        infos: list[BindingInfo] = []
        for handle in ephemeral:
            detail = self._bindings.ephemeral_entry(handle)
            assert detail is not None  # just listed from the same map
            deps, config_json = detail
            infos.append(
                BindingInfo(
                    # Served id: re-attach so the return can cover it —
                    # total for capability-made bindings (bind_handle
                    # persists the handle at bind time). A host-direct
                    # bind (in-process library use) that skipped persisting
                    # raises here, naming the violation.
                    key=await self._session.ensure_loadable(handle),
                    state="external",
                    cells=tuple(
                        CellLocation(grid_path=grid_path, coords=coords)
                        for grid_path, coords in holding[handle]
                    ),
                    deps=await dep_infos(deps),
                    config=None if config_json is None else json.loads(config_json),
                )
            )
        for row in sandbox_rows:
            if not repo_configured:
                state: BindingState = "sandbox_repo_missing"
                fault_message: str | None = None
            else:
                launch_state, fault_message = self._bindings.sandbox_launch_state(row.key_id)
                state = {
                    "unlaunched": "sandbox_launchable",
                    "launching": "sandbox_launching",
                    "running": "sandbox_running",
                    "faulted": "sandbox_faulted",
                }[
                    launch_state
                ]  # type: ignore[assignment]
            infos.append(
                BindingInfo(
                    key=await self._session.ensure_loadable(row.key_id),
                    state=state,
                    cells=tuple(
                        CellLocation(grid_path=grid_path, coords=coords)
                        for grid_path, coords in holding[row.key_id]
                    ),
                    deps=await dep_infos(row.deps),
                    config=None if row.config_json is None else json.loads(row.config_json),
                    sandbox=SandboxBindingInfo(defn=row.defn, fault_message=fault_message),
                )
            )
        # Documented order: by key id string across both classes.
        infos.sort(key=lambda b: b.key.id_string())
        return tuple(infos)

    # -- inspection -----------------------------------------------------------------------

    @service_method
    async def inspect_cell(self, sel: ViewSel) -> CellReport:
        """Contract on user_api.UserCapabilityProto.inspect_cell (the
        content + both axes' verdicts are one session snapshot —
        `WorkflowSession.cell_snapshot`)."""
        spec = self._registry.sliced(sel)
        grid_path, coords = self._resolve_cell(spec)
        currency, grounding, value = await self._session.cell_snapshot(grid_path, coords)
        g_verdict = grounding.verdict if grounding is not None else None
        g_reason = grounding.reason if grounding is not None else None
        if spec.kind == "task":
            wanted = grounding.wanted if grounding is not None else None
            wanted_reason = grounding.wanted_reason if grounding is not None else None
            if currency is None:
                return CellReport(
                    grounding=g_verdict,
                    grounding_reason=g_reason,
                    wanted=wanted,
                    wanted_reason=wanted_reason,
                )
            reason = currency.reason
            if reason is None and currency.status in ("errored", "interrupted"):
                reason = await self._failure_summary(grid_path, coords)
            results = None
            if currency.results is not None:
                results = frozendict(
                    {
                        name: await self._session.ensure_loadable(value)
                        for name, value in currency.results.items()
                    }
                )
            return CellReport(
                status=cast(Any, currency.status),
                currency=currency.currency,
                reason=reason,
                results=results,
                grounding=g_verdict,
                grounding_reason=g_reason,
                wanted=wanted,
                wanted_reason=wanted_reason,
            )
        if value is None:
            return CellReport()
        return CellReport(
            value=await self._session.ensure_loadable(value),
            grounding=g_verdict,
            grounding_reason=g_reason,
            # Boundness is ephemeral runner state (handle cells only) —
            # reported beside the snapshot, never part of it.
            bound=self._bindings.is_bound(value) if spec.kind == "handle" else None,
        )

    async def _failure_summary(self, grid_path: str, coords: str) -> str:
        """One generation's failure rows as a display string."""
        row = await self._session.get_generation(grid_path, coords)
        if row is None:
            return "no generation"
        rows = await self._session.load_failures(row[0])
        return "; ".join(f"{cls}: {msg}" for _, cls, msg in rows) or "no failure recorded"

    @service_method
    async def inspect_generation(self, sel: ViewSel) -> GenerationReport:
        """Contract on user_api.UserCapabilityProto.inspect_generation
        (the snapshot is the session op's one transaction; this method
        only maps rows and re-attaches served ids — all pinned by the
        liveness invariant at their original recording sites, so every
        re-attach is total)."""
        spec = self._registry.sliced(sel)
        if spec.kind != "task":
            raise ViewAxisError(f"inspect_generation needs a task selection, got {spec.kind!r}")
        grid_path, coords = self._resolve_cell(spec)
        detail = await self._session.generation_report(grid_path, coords)
        if detail is None:
            return GenerationReport()
        observations = tuple(
            [
                ObservationInfo(
                    subject_path=obs.subject_path,
                    subject_coords=obs.subject_coords,
                    subject_output=obs.subject_output,
                    kind=obs.kind,
                    # Served id: re-attach so the return can cover it.
                    fingerprint=await self._session.ensure_loadable(obs.fingerprint),
                    current=obs.current,
                )
                for obs in detail.observations
            ]
        )
        checkpoints = tuple(
            [
                CheckpointInfo(
                    grid_path=ck.grid_path,
                    coords=ck.coords,
                    # Served ids: re-attach (pinned by put_checkpoint).
                    step_type_id=await self._session.ensure_loadable(ck.step_type_id),
                    args_id=await self._session.ensure_loadable(ck.args_id),
                    value_id=await self._session.ensure_loadable(ck.value_id),
                )
                for ck in detail.checkpoints
            ]
        )
        return GenerationReport(
            generation=detail.generation,
            status=cast(Any, detail.status),
            # Served id: re-attach (pinned before the generation insert).
            executed_fn=await self._session.ensure_loadable(detail.executed_fn),
            failures=tuple(
                FailureInfo(kind=f.kind, error_class=f.error_class, message=f.message, ts=f.ts)
                for f in detail.failures
            ),
            observations=observations,
            unanswered_requests=tuple(
                RequestInfo(grid_path=r.grid_path, coords=r.coords, kind=r.kind)
                for r in detail.unanswered_requests
            ),
            checkpoints=checkpoints,
        )

    @service_method
    async def list_observers(
        self, sel: ViewSel, output: str | None = None
    ) -> tuple[ObserverInfo, ...]:
        """Contract on user_api.UserCapabilityProto.list_observers."""
        spec = self._registry.sliced(sel)
        grid_path, coords = self._resolve_cell(spec)
        return tuple(
            ObserverInfo(
                grid_path=row.grid_path,
                coords=row.coords,
                status=cast(Any, row.status),
                currency=cast(Any, row.currency),
            )
            for row in await self._session.list_observers(grid_path, coords, output)
        )

    # -- presence queries -------------------------------------------------------------------

    @service_method
    async def summarize(self) -> WorkflowSummary:
        """Contract on user_api.UserCapabilityProto.summarize (the
        snapshot is the session op's single lock hold; this method maps
        rows, decodes axes from the stored configs, and re-attaches the
        config ids — pinned by create_grid's persist, so both the load
        and the re-attach are total)."""
        grids = []
        for row in await self._session.summarize():
            config = (await self._session.load_config(row.config_id)).config
            grids.append(
                GridSummary(
                    path=row.path,
                    kind=cast(Any, row.kind),
                    axes=config.axes,
                    # Served id: re-attach so the return can cover it.
                    config_id=await self._session.ensure_loadable(row.config_id),
                    grounding=cast(Any, row.grounding_verdict),
                    grounding_reason=row.grounding_reason,
                    sourcing=row.sourcing,
                    populated=row.populated,
                    n_pending=row.n_pending,
                    n_running=row.n_running,
                    n_complete=row.n_complete,
                    n_errored=row.n_errored,
                    n_interrupted=row.n_interrupted,
                    n_stale=row.n_stale,
                    n_unsettled=row.n_unsettled,
                    targets_attached=row.targets_attached,
                    targets_dangling=row.targets_dangling,
                )
            )
        return WorkflowSummary(grids=tuple(grids))

    async def _graph_edges(self) -> tuple[GraphEdge, ...]:
        """The edge overlay half in wire form (id-free identity map over the
        session's :meth:`~fxtr_experiment.state_store.WorkflowSession.graph_edges`
        rows). Impl-private: the watch engine recomputes this half alone on
        ``cells`` hints."""
        return tuple(
            GraphEdge(
                observer_path=row.observer_path,
                subject_path=row.subject_path,
                kind=row.kind,
                count=row.count,
            )
            for row in await self._session.graph_edges()
        )

    async def _graph_structure(
        self,
    ) -> tuple[tuple[InputGrantEdge, ...], tuple[AxisCorrespondence, ...]]:
        """The structural overlay half (input grants + axis correspondences)
        in wire form. Impl-private: the watch engine recomputes this half
        alone on ``grids`` hints (rules are fixed at grid creation)."""
        grants, corrs = await self._session.graph_structure()
        return (
            tuple(
                InputGrantEdge(
                    task_path=g.task_path,
                    target_path=g.target_path,
                    arg_name=g.arg_name,
                    is_task_fn=g.is_task_fn,
                    mode=g.mode,
                    kept=g.kept,
                )
                for g in grants
            ),
            tuple(
                AxisCorrespondence(
                    grid_a=c.grid_a, axis_a=c.axis_a, grid_b=c.grid_b, axis_b=c.axis_b
                )
                for c in corrs
            ),
        )

    @service_method
    async def grid_graph(self) -> GridGraph:
        """Contract on user_api.UserCapabilityProto.grid_graph."""
        edges = await self._graph_edges()
        grants, correspondences = await self._graph_structure()
        return GridGraph(edges=edges, input_grants=grants, correspondences=correspondences)

    @service_method
    async def list_keys(self, subtree: ViewToken) -> tuple[KeyInfo, ...]:
        """Contract on user_api.UserCapabilityProto.list_keys."""
        subtree_spec = self._registry.spec_of(subtree)
        if subtree_spec.kind != "subtree":
            raise ViewAxisError(f"list_keys needs a subtree grant, got {subtree_spec.kind!r}")
        parent = encode_key_path(tuple(step.key for step in subtree_spec.path))
        infos = []
        for child_path, kind in await self._session.list_child_grids(parent):
            row = await self._session.get_grid(child_path)
            assert row is not None  # just listed
            config = (await self._session.load_config(row[1])).config
            grounding = await self._session.grid_grounding(child_path)
            infos.append(
                KeyInfo(
                    key=decode_key_path(child_path)[-1],
                    kind=cast(Any, kind),
                    axes=config.axes,
                    grounding=grounding.verdict if grounding is not None else None,
                    grounding_reason=grounding.reason if grounding is not None else None,
                    # Served id: re-attach so the return can cover it
                    # (pinned by create_grid's persist — the liveness
                    # invariant — so the re-attach is total).
                    config_id=await self._session.ensure_loadable(row[1]),
                )
            )
        return tuple(infos)

    @service_method
    async def list_cells(self, sel: ViewSel) -> tuple[CellInfo, ...]:
        """Contract on user_api.UserCapabilityProto.list_cells (presence,
        not bounds; no values)."""
        spec = self._registry.sliced(sel)
        if isinstance(spec, CoordViewSpec):
            raise ViewAxisError("coord views address no stored cells")
        grid_path = encode_key_path(tuple(step.key for step in spec.path))
        infos: list[CellInfo] = []
        if spec.kind == "task":
            for _exec_id, coords, status in await self._session.list_generations(grid_path):
                open_values = _match_templates(spec, coords)
                if open_values is None:
                    continue
                report = await self._session.cell_currency(grid_path, coords)
                grounding = await self._session.cell_grounding(grid_path, coords)
                reason = report.reason if report is not None else None
                if reason is None and status in ("errored", "interrupted"):
                    reason = await self._failure_summary(grid_path, coords)
                infos.append(
                    CellInfo(
                        coords=open_values,
                        status=cast(Any, status),
                        currency=report.currency if report is not None else None,
                        reason=reason,
                        grounding=grounding.verdict if grounding is not None else None,
                        wanted=grounding.wanted if grounding is not None else None,
                    )
                )
            return tuple(infos)
        for coords, _value in await self._session.list_grid_cells(grid_path):
            open_values = _match_templates(spec, coords)
            if open_values is not None:
                grounding = await self._session.cell_grounding(grid_path, coords)
                infos.append(
                    CellInfo(
                        coords=open_values,
                        grounding=grounding.verdict if grounding is not None else None,
                    )
                )
        return tuple(infos)

    @service_method
    async def lint(self) -> LintReport:
        """Contract on user_api.UserCapabilityProto.lint (the snapshot is
        the session op's single lock hold; this method only maps rows to
        the report types — slice-template loads and id re-attach happen
        here, after the snapshot, on immutable pinned entities)."""
        state = await self._session.lint()
        dangling_sourcing = []
        for srow in state.dangling_sourcing:
            dangling_sourcing.append(
                SourcingInfo(
                    grid_path=srow.grid_path,
                    coords=srow.coords,
                    # Served id: re-attach so the return can cover it.
                    fingerprint=await self._session.ensure_loadable(srow.fingerprint),
                    status=srow.status,
                )
            )
        dangling_targets: list[TargetInfo] = []
        target_findings: list[TargetFinding] = []
        for trow in state.targets:
            entity = await self._session.load_slice_template(trow.slice_id)
            record = TargetRecord(
                slice=entity.template,
                # Served id: re-attach so the return can cover it.
                expected_config=await self._session.ensure_loadable(trow.expected_config),
            )
            if trow.status != "attached":
                dangling_targets.append(
                    TargetInfo(
                        target=record,
                        status=trow.status,
                        grid_path=trow.grid_path,
                        coords=trow.coords,
                        grid_grounding=None,
                    )
                )
            else:
                assert trow.verdict is not None  # attached rows carry the verdict
                target_findings.append(
                    TargetFinding(
                        target=record,
                        grid_path=trow.grid_path,
                        coords=trow.coords,
                        verdict=trow.verdict,
                        reason=trow.reason,
                    )
                )
        ungrounded = tuple(
            UngroundedFact(
                kind=urow.kind,
                grid_path=urow.grid_path,
                coords=urow.coords,
                verdict=urow.verdict,
                reason=urow.reason,
            )
            for urow in state.ungrounded
        )
        return LintReport(
            dangling_sourcing=tuple(dangling_sourcing),
            dangling_targets=tuple(dangling_targets),
            target_findings=tuple(target_findings),
            ungrounded=ungrounded,
        )

    @service_method
    async def export_state(self, directory: str) -> ExportReport:
        """Contract on user_api.UserCapabilityProto.export_state (the
        snapshot is one locked session op; rendering runs in a worker
        thread outside the lock — determinism and dir-ownership are the
        writer's contract, `export.write_export`)."""
        snapshot = await self._session.export_snapshot()
        return await anyio.to_thread.run_sync(write_export, snapshot, Path(directory))

    @service_method
    async def import_state(self, directory: str, force: bool = False) -> ImportReport:
        """Contract on user_api.UserCapabilityProto.import_state (parse in
        a worker thread, verify against the store — both outside the
        lock, nothing written; the destructive half is the session's one
        locked transaction, `WorkflowSession.apply_import`)."""
        layout = await anyio.to_thread.run_sync(read_layout, Path(directory))
        bundle = await verify_and_decode(self._session, layout)
        return await self._session.apply_import(bundle, force)


# -- the wrapper ---------------------------------------------------------------------


class _UserViewBackend:
    """The explicit `view_runtime.ViewBackend` adapter over the capability.

    The shared view classes call the seam's *task-shaped* signatures; this
    adapter carries the (small) divergence explicitly instead of punning
    the wire protocol into the seam's shape: the seam's attach-only
    ``open_handle`` maps to the capability's default (non-``create``)
    handle open (creation stays a deliberate wrapper affordance), and the
    task-internals ops the
    user capability deliberately lacks fail with intentional errors rather
    than raw unknown-method transit failures. Every method the seam and
    the wire protocol share forwards untouched (``__getattr__``), so the
    two surfaces stay free to diverge without touching each other.
    """

    def __init__(self, capability: UserCapabilityProto) -> None:
        """Adapt ``capability`` (a resolved proxy or in-process impl)."""
        self._capability = capability

    def __getattr__(self, name: str) -> Any:
        """Forward every shared-shape method to the capability."""
        return getattr(self._capability, name)

    async def open_handle(
        self, subtree: ViewSel, key: str, axes: tuple[AxisSpec, ...]
    ) -> ViewGrant:
        """The seam's attach-only shape: a non-creating handle open."""
        return await self._capability.open_handle(subtree, key, axes)

    async def open_checkpoint(
        self, subtree: ViewToken, key: str, axes: tuple[AxisSpec, ...]
    ) -> ViewGrant:
        """Deliberately absent at the top level."""
        raise ViewAxisError(
            "checkpoint grids belong to task internals; there is no top-level open_checkpoint"
        )

    async def run_step(self, checkpoint: ViewSel, step: object, args: Mapping[str, Any]) -> Any:
        """Deliberately absent at the top level."""
        raise ViewAxisError("steps belong to task internals; there is no top-level run_step")


class Workflow:
    """Unprivileged convenience wrapper over one :class:`UserCapabilityProto`.

    Pure glue: holds a resolved capability proxy — in-process or remote,
    it cannot tell the difference — and re-exposes it ergonomically,
    adding **no capability**. Calculus ops flow through the shared view
    classes rooted at :attr:`root`; privileged ops are methods taking
    views (a UX split, not a security boundary — the capability checks
    every op either way, and keeping the view surface exactly the task
    contract means top-level-only affordances fail at the method level
    instead of poisoning inside a task).

    Construct via :meth:`connect` (any resolved proxy) or
    :func:`open_local` (the local bootstrap). Methods addressing cells
    take a view plus ``**coords`` binding its remaining axes, mirroring
    the view ops.
    """

    def __init__(self, capability: UserCapabilityProto, root_grant: ViewGrant) -> None:
        """Wrap a resolved capability (use :meth:`connect`, which fetches
        the root grant)."""
        self._capability = capability
        self._backend = _UserViewBackend(capability)
        self._root = cast(SubtreeView, build_view(cast(ViewBackend, self._backend), root_grant))

    @classmethod
    async def connect(cls, capability: UserCapabilityProto) -> "Workflow":
        """Wrap a resolved capability proxy (fetches the root grant)."""
        return cls(capability, await capability.root())

    @property
    def root(self) -> SubtreeView:
        """The root namespace as a write-mode, zero-axis subtree view."""
        return self._root

    @property
    def capability(self) -> UserCapabilityProto:
        """The raw wire capability (escape hatch / remote symmetry)."""
        return self._capability

    @staticmethod
    def _sel(view: GridView, coords: Mapping[str, CoordValue] | None = None) -> ViewSel:
        """A view + coords → the wire selection (the views' own encoding).

        Raises:
            ViewAxisError: ``view`` is a coord view — coord views address
                no stored cells, so no privileged op applies to them (and
                a *local* one has no wire selection to emit at all).
        """
        if isinstance(view, CoordView):
            raise ViewAxisError("coord views address no stored cells")
        return view._shadow.sel(coords)  # type: ignore[attr-defined]  # package-internal seam

    @staticmethod
    def _sel_partial(view: GridView, coords: Mapping[str, CoordValue] | None = None) -> ViewSel:
        """Like :meth:`_sel` but positions may stay open (multi-cell ops
        and token-only uses)."""
        if isinstance(view, CoordView):
            raise ViewAxisError("coord views address no stored cells")
        return view._shadow.sel_partial(coords)  # type: ignore[attr-defined]

    # -- materialization ---------------------------------------------------------

    async def demand(
        self, view: GridView, *, target: bool = True, adopt: bool = False, **coords: CoordValue
    ) -> FxtrID:
        """Materialize one cell now.

        User demands require attached targets (the capability's rule —
        the method declares the intent), so this wrapper **adds the
        target first by default**: demanding *is* declaring the cell a
        wanted DAG root. ``target=False`` skips the add and faults
        (`NotTargetedError`) unless the cell is already targeted. The
        auto-add composes strictly with the grid-before-cell invariant —
        demanding into a grid you haven't sourced faults with "adopt the
        grid first", never silently adopts.

        Args:
            view: The view to demand through (a task-output or data view).
            target: Durably target the cell first (default on; the add is
                an ordinary, idempotent ``add_target``).
            adopt: With ``target``: also source the grid first if it has
                no user sourcing record (the explicit per-call opt-in —
                never the default; adoption is a grid-level ownership
                claim).
            **coords: Coordinate per remaining axis name.

        Returns:
            The cell's value id.
        """
        sel = self._sel(view, coords)
        if target:
            await typed_wire_call(self._capability.add_target(sel, adopt=adopt))
        return await typed_wire_call(self._capability.read_cell_entity(sel))

    async def demand_table(
        self, view: GridView, *, target: bool = True, adopt: bool = False, **coords: CoordValue
    ) -> "pl.DataFrame":
        """Materialize one frame cell now — :meth:`demand`'s frame sibling.

        Same auto-target semantics (see :meth:`demand`; ``demand`` itself
        stays entity-only, since frame ids never surface as values). The
        view addresses a dataframe cell or a task's declared frame output.

        Returns:
            The cell's rows as a ``polars.DataFrame`` (canonical dtypes,
            canonical row order).
        """
        sel = self._sel(view, coords)
        if target:
            await typed_wire_call(self._capability.add_target(sel, adopt=adopt))
        data = await typed_wire_call(self._capability.read_cell_frame(sel))
        # Lazy import: polars loads on first frame op (the chunk-2 doctrine).
        from fxtr_experiment_task.tables import ipc_to_frame

        return ipc_to_frame(data)

    # -- sourcing ------------------------------------------------------------------

    async def source_grid(self, subtree: SubtreeView, key: str) -> None:
        """Adopt the grid at ``key``: pin its current config as user
        intent (unlocks targets/writes inside; internals refused)."""
        await typed_wire_call(self._capability.source_grid(self._sel_partial(subtree), key))

    async def source_cell(self, view: GridView, **coords: CoordValue) -> None:
        """Adopt the addressed cell's current value as user data."""
        await typed_wire_call(self._capability.source_cell(self._sel(view, coords)))

    async def revoke_grid_sourcing(
        self, subtree: SubtreeView, key: str, *, force: bool = False
    ) -> bool:
        """Remove the grid's sourcing record (refuses with live cell-level
        intent unless ``force`` sweeps it — the maintained invariant)."""
        return await typed_wire_call(
            self._capability.revoke_grid_sourcing(self._sel_partial(subtree), key, force=force)
        )

    async def revoke_cell_sourcing(self, view: GridView, **coords: CoordValue) -> bool:
        """Remove the addressed cell's sourcing record (value stays)."""
        return await typed_wire_call(self._capability.revoke_cell_sourcing(self._sel(view, coords)))

    async def sourcing(self) -> tuple[SourcingInfo, ...]:
        """All sourcing records with attachment status."""
        return await typed_wire_call(self._capability.list_sourcing())

    async def delete_grid(
        self, subtree: SubtreeView, key: str, *, recursive: bool = False
    ) -> DeleteReport:
        """Delete the child grid at ``key`` (rmdir-style, or ``recursive``
        for the whole subtree), returning the consequence report (external
        referrer task grids whose grounding flips)."""
        return await typed_wire_call(
            self._capability.delete_grid(self._sel_partial(subtree).token, key, recursive=recursive)
        )

    async def run_targets(self) -> tuple[TargetInfo, ...]:
        """Demand every currently-attached target (the optional "run the
        workflow" affordance).

        Returns:
            The dangling targets (reported, never demanded — and never
            silently rebound).
        """
        dangling = []
        for info in await typed_wire_call(self._capability.list_targets()):
            if info.status == "attached":
                await typed_wire_call(self._capability.demand_target(info.target))
            else:
                dangling.append(info)
        return tuple(dangling)

    # -- targets -------------------------------------------------------------------

    async def add_target(
        self, view: GridView, *, adopt: bool = False, **coords: CoordValue
    ) -> None:
        """Durably mark the addressed cell as a wanted DAG root
        (``adopt=True`` sources an unsourced grid first — the one-call
        recovery from `GridNotSourcedError`)."""
        await typed_wire_call(self._capability.add_target(self._sel(view, coords), adopt=adopt))

    async def remove_target(self, view: GridView, **coords: CoordValue) -> None:
        """Remove the addressed cell's target record (slice identity)."""
        await typed_wire_call(self._capability.remove_target(self._sel(view, coords)))

    async def targets(self) -> tuple[TargetInfo, ...]:
        """All target records with attachment status."""
        return await typed_wire_call(self._capability.list_targets())

    # -- privileged mutation ----------------------------------------------------------

    async def attach(self, subtree: SubtreeView, key: str) -> GridView:
        """Open whatever grid exists at ``key``, from its stored config
        alone (nothing proposed, recorded, or created) — the browsing
        counterpart of the ``open_*`` calls; the view follows the chain's
        authority. Pair with :meth:`list_keys`
        to reconstruct views after a reopen without re-supplying configs
        or rules (task grids included)."""
        grant = await typed_wire_call(self._capability.attach(self._sel_partial(subtree), key))
        return build_view(cast(ViewBackend, self._backend), grant)

    async def internals(self, task: GridView, **coords: CoordValue) -> SubtreeView:
        """Browse a task's internals (read-only; see the wire op). Fully
        bound coords browse one cell's key-space; open positions stay
        kept — the grid-level window across the selected cells. Pair
        with :meth:`list_keys` / :meth:`attach` to drill down."""
        grant = await typed_wire_call(
            self._capability.attach_internals(self._sel_partial(task, coords))
        )
        return cast(SubtreeView, build_view(cast(ViewBackend, self._backend), grant))

    async def open_handle(
        self, subtree: SubtreeView, key: str, axes: tuple[AxisSpec, ...]
    ) -> HandleView:
        """Create-or-attach a handle grid (the top-level authority-minting
        affordance — this wrapper passes the capability's explicit
        ``create`` flag; the shared view classes' own ``open_handle``
        stays attach-only, matching the task contract).
        Populate cells with :meth:`write_handle`."""
        grant = await typed_wire_call(
            self._capability.open_handle(self._sel_partial(subtree), key, axes, True)
        )
        view = build_view(cast(ViewBackend, self._backend), grant)
        assert isinstance(view, HandleView)  # a handle grant builds a handle view
        return view

    async def overwrite(self, view: GridView, value: FxtrID, **coords: CoordValue) -> None:
        """Replace a cell's existing value (direct observers go stale,
        their dependents unsettled — the machinery working as intended)."""
        await typed_wire_call(
            self._capability.write_cell_entity(self._sel(view, coords), value, overwrite=True)
        )

    async def overwrite_table(
        self, view: GridView, table: "pl.DataFrame", **coords: CoordValue
    ) -> None:
        """:meth:`overwrite`'s frame sibling: replace a dataframe cell's
        existing multiset (validated against the declared schema)."""
        # Lazy import: polars loads on first frame op (the chunk-2 doctrine).
        from fxtr_experiment_task.tables import frame_to_ipc

        await typed_wire_call(
            self._capability.write_cell_frame(
                self._sel(view, coords), frame_to_ipc(table), overwrite=True
            )
        )

    async def write_handle(self, view: GridView, value: FxtrID, **coords: CoordValue) -> None:
        """Populate a handle cell (handle views carry no ``write`` — the
        task-facing contract is attach-only; populating is top-level)."""
        await typed_wire_call(self._capability.write_cell_entity(self._sel(view, coords), value))

    async def clear(self, view: GridView, **coords: CoordValue) -> None:
        """Drop the generation state of the addressed task cells (open
        positions clear every addressed cell; atomic — see the wire op)."""
        await typed_wire_call(self._capability.clear_cells(self._sel_partial(view, coords)))

    async def delete(self, subtree: SubtreeView, key: str, *, recursive: bool = False) -> None:
        """Delete the child grid at ``key`` (rmdir-style: refuses if child
        grids exist or a generation inside is running; ``recursive``
        deletes the whole subtree instead)."""
        await typed_wire_call(
            self._capability.delete_grid(self._sel_partial(subtree).token, key, recursive=recursive)
        )

    # -- inspection / presence queries ---------------------------------------------------

    async def inspect(self, view: GridView, **coords: CoordValue) -> CellReport:
        """The addressed cell's content with its currency verdict, one
        snapshot — never executes; the way to look at stale/unsettled
        results, honestly labeled."""
        return await typed_wire_call(self._capability.inspect_cell(self._sel(view, coords)))

    async def inspect_generation(self, view: GridView, **coords: CoordValue) -> GenerationReport:
        """The addressed task cell's current generation, itemized:
        failures, observations with per-edge current-match flags,
        unanswered requests, checkpoint entries — the debug view (empty
        form when the cell has no generation)."""
        return await typed_wire_call(self._capability.inspect_generation(self._sel(view, coords)))

    async def observers(
        self, view: GridView, *, output: str | None = None, **coords: CoordValue
    ) -> tuple[ObserverInfo, ...]:
        """The observer cells of the addressed subject (reverse edges),
        each with status + currency — "what depends on this"."""
        return await typed_wire_call(
            self._capability.list_observers(self._sel(view, coords), output)
        )

    async def summarize(self) -> WorkflowSummary:
        """Every grid with rollup counts and verdicts, one snapshot — the
        dashboard tree's one-call backing (currency force-evaluates; the
        stamps memoize)."""
        return await typed_wire_call(self._capability.summarize())

    async def grid_graph(self) -> GridGraph:
        """The plate view's relational overlay, one snapshot — grid-level
        edges, input grants, and ``Param`` axis correspondences (design
        ``notes/plate_view.md``)."""
        return await typed_wire_call(self._capability.grid_graph())

    async def bindings(self) -> tuple[BindingInfo, ...]:
        """The bound handle keys of both classes (ephemeral + sandboxed),
        each with its :class:`~fxtr_experiment.user_api.BindingState` and
        the handle cells holding its key (with the handle grids, the
        complete external-authority surface)."""
        return await typed_wire_call(self._capability.list_bindings())

    async def delete_cell(self, view: GridView, **coords: CoordValue) -> bool:
        """Remove the addressed data/handle cell's value (a user settle
        event: observers go stale; the sourcing record dangles; task
        cells refuse — use :meth:`clear`)."""
        return await typed_wire_call(self._capability.delete_cell(self._sel(view, coords)))

    async def list_keys(self, subtree: SubtreeView) -> tuple[KeyInfo, ...]:
        """List the child keys of a subtree."""
        return await typed_wire_call(self._capability.list_keys(self._sel_partial(subtree).token))

    async def list_cells(self, view: GridView) -> tuple[CellInfo, ...]:
        """List the nonempty cells the view addresses, with status."""
        return await typed_wire_call(self._capability.list_cells(self._sel_partial(view)))

    async def lint(self) -> LintReport:
        """The whole-workflow structural inventory, one snapshot (dangling
        intent, red targets, the ungrounded-fact inventory — grounding
        only, never staleness; see :class:`~fxtr_experiment.user_api.
        LintReport` for the sound/clean levels)."""
        return await typed_wire_call(self._capability.lint())

    async def export_state(self, directory: str) -> ExportReport:
        """Rebuild ``directory`` (a server-side path) as the at-rest
        layout of ``notes/at_rest_format.md`` — deterministic,
        export-owned (ownership proved before anything is touched),
        refused while anything runs."""
        return await typed_wire_call(self._capability.export_state(directory))

    async def import_state(self, directory: str, force: bool = False) -> ImportReport:
        """**Destructively** set the current state to the exported layout
        at ``directory`` (a server-side path; same entity store).
        Verified before anything is written; refuses a non-empty state
        db unless ``force``; everything imports unstamped; re-bind
        connectors afterwards (as after a reopen)."""
        return await typed_wire_call(self._capability.import_state(directory, force=force))

    # -- registration ---------------------------------------------------------------------

    async def bind_handle(
        self,
        handle: FxtrID,
        connector: ServiceRef[UnknownService],
        deps: "Mapping[str, GridView] | None" = None,
        config: JSONValue = None,
    ) -> ServiceRef[BindingReleaseProto]:
        """Register a live connector for a handle entity (forwards to the
        capability; works over RPC — connectors are ServiceRefs).

        ``deps`` / ``config`` follow :meth:`bind_sandbox`'s semantics
        exactly (the two classes are uniform in what the bound
        implementation receives)."""
        sels = {name: self._sel(view) for name, view in (deps or {}).items()}
        return await typed_wire_call(self._capability.bind_handle(handle, connector, sels, config))

    async def bind_sandbox(
        self,
        handle: FxtrID,
        defn: SandboxDefn,
        deps: "Mapping[str, GridView] | None" = None,
        config: JSONValue = None,
    ) -> None:
        """Register a durable sandboxed binding (forwards to the
        capability).

        ``deps`` maps each dependency name to a **fully-bound handle-cell
        view** (``.at(...)`` it down to a single cell first); ``config`` is
        optional plain JSON. See
        :meth:`~fxtr_experiment.user_api.UserCapabilityProto.bind_sandbox`.
        """
        sels = {name: self._sel(view) for name, view in (deps or {}).items()}
        await typed_wire_call(self._capability.bind_sandbox(handle, defn, sels, config))

    async def remove_sandbox_binding(self, handle: FxtrID) -> None:
        """Remove a sandboxed binding (forwards to the capability)."""
        await typed_wire_call(self._capability.remove_sandbox_binding(handle))

    async def shutdown_sandbox(self, handle: FxtrID) -> None:
        """Tear down a sandboxed binding's cached process — release-and-
        forget; the record stays and the next demand relaunches (forwards
        to the capability)."""
        await typed_wire_call(self._capability.shutdown_sandbox(handle))

    async def watch_workflow(
        self, subscriber: "ServiceRef[Subscriber[FxtrServiceData]]"
    ) -> "ServiceRef[UnsubscribeHandle]":
        """Subscribe to the workflow-level watch (see the wire op for the
        event vocabulary and delivery contract)."""
        return await typed_wire_call(self._capability.watch_workflow(subscriber))

    async def watch_cells(
        self, subscriber: "ServiceRef[Subscriber[FxtrServiceData]]", *, path: str
    ) -> "ServiceRef[UnsubscribeHandle]":
        """Subscribe to one grid's watch (see the wire op)."""
        return await typed_wire_call(self._capability.watch_cells(subscriber, path=path))

    async def watch_cell(
        self, subscriber: "ServiceRef[Subscriber[FxtrServiceData]]", *, path: str, coords: str
    ) -> "ServiceRef[UnsubscribeHandle]":
        """Subscribe to one cell's watch (see the wire op)."""
        return await typed_wire_call(
            self._capability.watch_cell(subscriber, path=path, coords=coords)
        )

    async def watch_graph(
        self, subscriber: "ServiceRef[Subscriber[FxtrServiceData]]"
    ) -> "ServiceRef[UnsubscribeHandle]":
        """Subscribe to the grid-graph watch (see the wire op)."""
        return await typed_wire_call(self._capability.watch_graph(subscriber))


@asynccontextmanager
async def open_local(
    state_db: str,
    entity_db: str,
    *,
    grounding_policy: 'Literal["strict", "off"]' = "strict",
    sandbox_repo: Path | None = None,
    sandbox_cache_dir: Path | None = None,
) -> AsyncIterator[Workflow]:
    """The local bootstrap: open a workflow in this process.

    Opens the two-file session, builds the runner (an isolated runner
    scope over the entity namespace + ``GLOBAL_SCOPE``, the binding host,
    the sandbox launcher when a repo is configured, the executor), mounts
    the user capability on the runner scope, resolves it, and yields the
    :class:`Workflow` wrapper. Teardown in reverse on exit — launched
    sandbox guests first, then the sandbox environment, then the holder
    task group, the runner scope, the session. A plain async CM by
    design — see the module docstring for why this is not a
    mount-lifespan bootstrap.

    Args:
        state_db: Path of the workflow state database.
        entity_db: Path of the fxtr entity store.
        grounding_policy: The grounding gate's mode (``strict`` default /
            ``off`` — see `Executor` and the gate table in
            ``notes/grounding.md``).
        sandbox_repo: The runner's sandbox repo — ephemeral, machine-local
            configuration for resolving sandboxed bindings' commits
            (``notes/sandboxed_bindings.md``). Set once here (not
            wire-settable, not exported); ``None`` = unconfigured, so
            sandboxed bindings report ``sandbox_repo_missing`` and fault
            on demand. Validated lazily (first spawn). Configuring it is
            what constructs the sandbox launcher — the `fxtr_sandboxing`
            machinery is imported and wired only then.
        sandbox_cache_dir: The sandbox environment's cache directory —
            machine-local like ``sandbox_repo`` (created if absent).
            Default: ``~/.cache/fxtr_experiment/sandboxes``.
    """
    async with WorkflowSession.open(state_db, entity_db) as session:
        async with FxtrScope(
            name="wf3-runner",
            isolated=True,
            # GLOBAL_SCOPE: service type entities (define_type) live there.
            include=[session.entity_scope, GLOBAL_SCOPE],
        ) as runner_scope:
            async with AsyncExitStack() as stack:
                launcher = None
                if sandbox_repo is not None:
                    # Lazy import: the sole fxtr_sandboxing importer —
                    # a repo-less runner never pays for it.
                    from fxtr_experiment.sandbox_launcher import FxtrSandboxLauncher

                    cache_dir = (
                        Path.home() / ".cache" / "fxtr_experiment" / "sandboxes"
                        if sandbox_cache_dir is None
                        else sandbox_cache_dir
                    )
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    # Stack order (LIFO): launcher.aclose runs before the
                    # task group exits (so holder tasks are told to tear
                    # down, then awaited), before the runner scope drains.
                    tg = await stack.enter_async_context(anyio.create_task_group())
                    launcher = FxtrSandboxLauncher(
                        runner_scope,
                        sandbox_repo=sandbox_repo,
                        cache_dir=cache_dir,
                        task_group=tg,
                    )
                    stack.push_async_callback(launcher.aclose)
                bindings = BindingHost(
                    runner_scope, session, launcher=launcher, sandbox_repo=sandbox_repo
                )
                executor = Executor(
                    session, bindings, runner_scope, grounding_policy=grounding_policy
                )
                impl = UserCapabilityImpl(session, executor, bindings, runner_scope)
                cap_ref = await runner_scope.mount(impl)
                # Membership: mounted on (thus resolvable in) the runner scope.
                proxy = await cap_ref.replace_service_type(UserCapabilityProto).resolve(
                    runner_scope
                )
                yield await Workflow.connect(proxy)
