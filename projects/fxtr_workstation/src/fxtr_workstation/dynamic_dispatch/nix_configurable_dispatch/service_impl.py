"""``NixDispatcherProto`` impl backed by a git-referenced UI preset.

A preset is a plain nix attrset (``fxtr.uiPresets.<name>``,
keyed by type id_string) referenced by git commit.
:class:`NixConfigurableDispatchImpl` resolves it against a configured git
repo, **eagerly** builds the renderer ESM derivations + validates each
entry's ``typeEntity`` against its key, mints a :class:`RendererEntity` per
renderer in the workstation store at serve time, and serves the unchanged
:class:`WorkstationDispatcherProto` surface (so the frontend's
``createDynamicDispatcher`` works without changes).

Both renderer builds **and** key↔``typeEntity`` validation happen in
:meth:`set_preset` (eager); :meth:`resolve` is then a pure dict lookup. See
``plans/phase0/part5_nix_workdir/design_notes/06_ui_presets.md`` for why
validation is eager (lazy resolve-time validation can't catch a stale key —
the mis-keyed entry never resolves).

The subscription / hot-reload machinery mirrors
:class:`fxtr_workstation.dynamic_dispatch.basic_dynamic_dispatch.service_impl.BasicDynamicDispatchImpl`
one-for-one — a coalesce-mode
:class:`~fxtr2.services.subscription_util.SubscriberHub` behind the
``EventSource.subscribe`` inherited by
:class:`WorkstationDispatcherProto`.
"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
from collections.abc import AsyncGenerator
from pathlib import Path

import anyio
import anyio.abc
from fxtr2.conversion.converters.unknown_entity import UnknownEntity
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import (
    GLOBAL_SCOPE,
    EntityRef,
    FxtrScope,
    ServiceRef,
    get_active_scope,
)
from fxtr2.data.layered_file_store import ParentPathRemap
from fxtr2.data.layered_file_store.reader import LayeredFileStoreReader
from fxtr2.leases.leased_cap_ir import IRException
from fxtr2.services.subscription_types import Subscriber, UnsubscribeHandle
from fxtr2.services.subscription_util import SubscriberHub
from fxtr2.services.typed_service import TypedServiceImpl
from fxtr2.services.untyped_service import service_method
from fxtr_nix_runner import Backend, Materialized, resolve_backend
from fxtr_nix_runner.renderers import build_drv_outputs, eval_preset_table

from ..dynamic_dispatch_types import (
    WORKSTATION_DISPATCHER_TYPE_ID,
    NixDispatcherProto,
    ReloadEverythingNotification,
    RendererEntity,
    RendererResolution,
    WorkstationDispatcherProto,
)

_log = logging.getLogger(__name__)

#: Bound on the mount harness's teardown flush of live subscriptions
#: (the hub's own ``aclose`` is deliberately unbounded; the bound is
#: call-site policy). On timeout the harness cancels ``impl_tg``,
#: force-closing every subscription.
SUBSCRIPTION_FLUSH_TIMEOUT_SECONDS = 5.0


class NixConfigurableDispatchImpl(TypedServiceImpl[NixDispatcherProto]):
    """Dispatcher impl that loads a UI preset by git reference.

    **Construction.** ``NixConfigurableDispatchImpl(scope, repo, impl_tg, *,
    initial_commit=None, initial_attr=None, initial_flake_path=None)``.
    ``repo`` (the git repo preset refs resolve against — locally the agent's
    workdir repo) is **required**; the optional initial ``(commit, attr)`` is
    auto-loaded by :meth:`initialize` so restarts come up with a UI.
    ``scope`` hosts the minted :class:`RendererEntity`s + subscription
    workers and must make :data:`RENDERER_ENTITY_TYPE_ID`'s descriptor
    reachable (``include=[GLOBAL_SCOPE]`` or a parent that does);
    ``impl_tg`` is a caller-owned task group the per-subscription
    workers run on.

    **Wire identity.** :meth:`_service_type_id` emits
    :data:`WORKSTATION_DISPATCHER_TYPE_ID` so the frontend's
    ``replace_target_type(WorkstationDispatcherProto)`` flow is unchanged;
    the agent re-types to :class:`NixDispatcherProto` for ``set_preset``
    (wire dispatch is by method name).

    TODO(daniel): improve type-ids to express subclassing relationships so this
    is less awkward.
    """

    def __init__(
        self,
        scope: FxtrScope,
        repo: str | Path,
        *,
        impl_tg: anyio.abc.TaskGroup,
        initial_commit: str | None = None,
        initial_attr: str | None = None,
        initial_flake_path: str | None = None,
    ) -> None:
        super().__init__()
        self._scope = scope
        self._repo = repo
        # The nix backend (host nix, or in-podman builds — FXTR_NIX_BACKEND +
        # ~/.config/fxtr/nix_runtime.toml). Resolved **lazily** on first
        # set_preset (see `_backend`), so constructing a dispatcher needs no
        # backend config. One instance per dispatcher, so its per-repo builder
        # is refcount-shared across this dispatcher's set_presets.
        self._nix_backend: Backend | None = None
        self._initial: tuple[str, str, str | None] | None = (
            (initial_commit, initial_attr, initial_flake_path)
            if initial_commit and initial_attr
            else None
        )
        # Resolution table: type_id_string -> {dispatch_key_uuid ->
        # RendererResolution}. Empty until `set_preset` (or `initialize`).
        self._table: dict[str, dict[str, RendererResolution]] = {}
        # Minted renderer entities, keyed by (commit, renderer drv path), so
        # a repeat `set_preset` of the same commit reuses them.
        self._renderer_cache: dict[tuple[str, str], EntityRef[RendererEntity]] = {}
        # The last preset ref we tried to load (loaded or attempted) — the
        # empty-table debug surface reads this so an unloaded dispatcher is
        # diagnosable. None until the first `set_preset` / `initialize`.
        self._current_ref: tuple[str, str, str | None] | None = None
        # Subscription machinery for renderer-table change events
        # (coalesce: every notification is identical, latest-value
        # skip-ahead is the right semantics). The hub has no scope of
        # its own; notification entities ride per-event origin leases
        # on `scope`.
        self._hub = SubscriberHub[EntityRef[ReloadEverythingNotification]](
            task_group=impl_tg,
            mode="coalesce",
        )

    # -- TypedServiceImpl boilerplate ---------------------------------

    def _target_type_annotation(self):  # type: ignore[no-untyped-def]
        return NixDispatcherProto

    def _service_type_id(self) -> FxtrID:
        # Wire-emit the workstation dispatcher type id (see class docstring).
        return WORKSTATION_DISPATCHER_TYPE_ID

    def _backend(self) -> Backend:
        """The nix backend, resolved lazily on first use (cached). Resolution
        requires a configured backend, so it only fails for a dispatcher that
        actually runs a set_preset build on an unconfigured machine."""
        if self._nix_backend is None:
            self._nix_backend = resolve_backend()
        return self._nix_backend

    # -- startup auto-load --------------------------------------------

    async def initialize(self) -> None:
        """Apply the configured initial preset, if any.

        **Always warn, never crash:** any failure (missing/stale ref,
        eval/build error, malformed preset) logs a warning and comes up with
        an empty table — the agent recovers via :meth:`set_preset`, and
        :attr:`current_ref` makes the unloaded/attempted state visible. (An
        agent-invoked ``set_preset`` is separate and always raises.)
        """
        if self._initial is None:
            return
        commit, attr, flake_path = self._initial
        try:
            await self.set_preset(commit, attr, flake_path)
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "NixConfigurableDispatchImpl: initial preset auto-load failed; coming"
                " up with an empty table. ref=(commit=%r, attr=%r, flake_path=%r): %r",
                commit,
                attr,
                flake_path,
                exc,
            )
            # Record the attempted ref for the empty-table debug surface.
            self._current_ref = (commit, attr, flake_path)

    @property
    def current_ref(self) -> tuple[str, str, str | None] | None:
        """The last preset ref loaded (or attempted). ``None`` means no
        preset has ever been pushed — every dispatch falls back. Surfaced
        for the empty-table debug view (open-concerns #5)."""
        return self._current_ref

    # -- typed-service methods ----------------------------------------

    @service_method
    async def set_preset(
        self,
        git_commit: str,
        flake_attr: str,
        flake_path: str | None = None,
    ) -> None:
        """Eval the preset table, eagerly build + validate every entry, swap
        the resolution table atomically, and broadcast a reload.

        All renderer ESMs + ``typeEntity`` layers are realized in **one**
        ``nix build`` (so nix schedules them in parallel); the cheap
        read/validate/assemble work runs after. ``self._table`` /
        ``self._current_ref`` are only written once every entry's renderer is
        minted + ``typeEntity`` validated, so a failure leaves the previous
        table intact (whole-preset reject). Raises on any failure.
        """
        # One builder spans the eval + batched build; the outputs are
        # materialized (host-readable), so the validate/mint work below runs
        # after the builder is released.
        async with self._backend().builder_for(Path(self._repo)) as h:
            table_json = await eval_preset_table(h, git_commit, flake_attr, flake_path)

            # Realize every drv we need in one batched, nix-parallelized build:
            # every typeEntity layer (validation, uncached) + every renderer not
            # already cached for this commit.
            uncached_renderers = {
                route["rendererDrv"]
                for entry in table_json.values()
                for route in entry["byDispatchKey"].values()
                if (git_commit, route["rendererDrv"]) not in self._renderer_cache
            }
            # Builtin-typeEntity entries (primitive FxtrIDs, e.g. `!basic_type`)
            # have no layer to build — they're validated against their carried
            # idString below.
            type_entity_drvs = {
                entry["typeEntityLayerDrv"]
                for entry in table_json.values()
                if not entry.get("builtin")
            }
            outs = await build_drv_outputs(h, sorted(uncached_renderers | type_entity_drvs))

        # Eager key <-> typeEntity validation: assert each entry's type id
        # matches its key. (Cheap; kept sequential so a mismatch raises a clean
        # IRException rather than an anyio ExceptionGroup.) Layer entities read
        # the built layer; builtin entities just compare the carried idString.
        for type_id_str, entry in table_json.items():
            if entry.get("builtin"):
                self._validate_builtin(type_id_str, entry["idString"], flake_attr)
            else:
                await self._validate_type_entity(
                    type_id_str,
                    outs[entry["typeEntityLayerDrv"]],
                    entry["typeEntityLayerKey"],
                    flake_attr,
                )

        # Mint a RendererEntity per newly-built renderer (cache by
        # (commit, drv)); `.store` is sync so no concurrency concern.
        for drv in uncached_renderers:
            module_bytes = outs[drv].host_path.read_bytes()
            self._renderer_cache[(git_commit, drv)] = RendererEntity(module=module_bytes).store(
                self._scope
            )

        # Assemble the table from the renderer cache.
        new_table: dict[str, dict[str, RendererResolution]] = {
            type_id_str: {
                key_uuid: RendererResolution(
                    subtype_key=route["subtypeKey"],
                    renderer=self._renderer_cache[(git_commit, route["rendererDrv"])],
                )
                for key_uuid, route in entry["byDispatchKey"].items()
            }
            for type_id_str, entry in table_json.items()
        }

        # Atomic swap — past this line, the new preset is live.
        self._table = new_table
        self._current_ref = (git_commit, flake_attr, flake_path)
        self.trigger_reload()

    async def _validate_type_entity(
        self, type_id_str: str, layer: Materialized, layer_key: str, flake_attr: str
    ) -> None:
        """Read the (already-built, materialized) ``typeEntity`` layer + assert
        its id_string == the entry's key. Mismatch → raise
        ``RendererMisconfigured`` (loud, rejects the whole preset). Catches a
        stale/wrong hand-pasted key."""
        remap = ParentPathRemap(*layer.remap) if layer.remap is not None else None
        async with LayeredFileStoreReader(layer.host_path, parent_path_remap=remap) as reader:
            type_fid = await reader.retrieve(layer_key)
        actual = type_fid.id_string()
        if actual != type_id_str:
            raise IRException(
                f"renderer misconfigured: preset key {type_id_str!r} does not match its"
                f" typeEntity id {actual!r} (flake_attr={flake_attr!r}). Re-paste the"
                " type's id_string: print(MyType.fxtr_type.id_string()).",
                category="RendererMisconfigured",
            )

    def _validate_builtin(self, type_id_str: str, builtin_id: str, flake_attr: str) -> None:
        """A ``{ type = "builtin"; idString; }`` entity has no layer to read —
        its id_string IS the type. Assert the carried idString matches the
        preset key (catches a hand-authored ``forType`` whose pasted key and
        builtin idString disagree). Sync — no build, no I/O."""
        if builtin_id != type_id_str:
            raise IRException(
                f"renderer misconfigured: preset key {type_id_str!r} does not match its"
                f" builtin entity idString {builtin_id!r} (flake_attr={flake_attr!r}).",
                category="RendererMisconfigured",
            )

    @service_method
    async def resolve(
        self,
        type_ref: EntityRef[UnknownEntity],
        dispatch_key_uuid: str,
    ) -> RendererResolution | None:
        """Pure lookup: ``_table[type_id_string][dispatch_key_uuid]``. A miss
        on either layer returns ``None`` (the frontend surfaces it as the
        dispatch key's ``NoMatchFallback``). No build, no validation — both
        are done eagerly in :meth:`set_preset`. An empty table (no preset
        pushed yet) is a valid state that resolves everything to ``None``."""
        by_key = self._table.get(type_ref.entity_id.id_string())
        if by_key is None:
            return None
        return by_key.get(dispatch_key_uuid)

    @service_method
    async def subscribe(
        self, subscriber: ServiceRef[Subscriber[EntityRef[ReloadEverythingNotification]]]
    ) -> ServiceRef[UnsubscribeHandle]:
        """Subscribe ``subscriber`` to renderer-table change events.
        Delegates to the internal
        :class:`~fxtr2.services.subscription_util.SubscriberHub`; see
        :meth:`WorkstationDispatcherProto.subscribe` for the contract.
        Mirrors ``BasicDynamicDispatchImpl.subscribe``."""
        return await self._hub.subscribe(subscriber, get_active_scope())

    # -- subscription machinery (mirrors BasicDynamicDispatchImpl) -----

    def trigger_reload(self) -> None:
        """Broadcast a reload to every subscriber. Sync; mints a fresh
        (content-deduped) :class:`ReloadEverythingNotification` in
        ``self._scope`` and publishes its ref through the hub
        (coalesce-mode skip-ahead)."""
        self._hub.publish(
            ReloadEverythingNotification().store(self._scope), origin_scope=self._scope
        )

    async def aclose_subscriptions(self) -> None:
        """Flush + tear down every live subscription (the hub's
        ``aclose``). Called by the mount harness on exit, before the
        impl scope drains."""
        await self._hub.aclose()


# --- ergonomic mount helper ----------------------------------------


@dataclasses.dataclass(frozen=True)
class MountedNixConfigurableDispatch:
    """Handle yielded by :func:`mount_nix_configurable_dispatch`. Mirrors
    :class:`MountedBasicDynamicDispatch`."""

    scope: FxtrScope
    service_ref: ServiceRef[WorkstationDispatcherProto]
    impl: NixConfigurableDispatchImpl


@contextlib.asynccontextmanager
async def mount_nix_configurable_dispatch(
    repo: str | Path,
    *,
    parent_scope: FxtrScope | None = None,
    initial_commit: str | None = None,
    initial_attr: str | None = None,
    initial_flake_path: str | None = None,
    scope_name: str = "nix_configurable_dispatch",
) -> AsyncGenerator[MountedNixConfigurableDispatch, None]:
    """Open a scope + task group, construct a
    :class:`NixConfigurableDispatchImpl`, run the startup auto-load, mount
    the impl, and yield a :class:`MountedNixConfigurableDispatch`.

    The yielded ``service_ref`` is typed ``ServiceRef[WorkstationDispatcherProto]``
    (the contract the workstation's external-service bridge + the frontend
    enforce); the agent's typed access to ``set_preset`` rides on
    ``replace_service_type(NixDispatcherProto)`` on the looked-up ref.

    On exit, the internal task group cancels (tearing down subscriptions),
    then the impl scope drains.
    """
    include = [parent_scope] if parent_scope is not None else [GLOBAL_SCOPE]
    async with FxtrScope(
        isolated=True,
        include=include,
        name=scope_name,
    ) as scope:
        async with anyio.create_task_group() as impl_tg:
            impl = NixConfigurableDispatchImpl(
                scope=scope,
                repo=repo,
                impl_tg=impl_tg,
                initial_commit=initial_commit,
                initial_attr=initial_attr,
                initial_flake_path=initial_flake_path,
            )
            await impl.initialize()
            service_ref = (await scope.mount(impl)).replace_service_type(WorkstationDispatcherProto)
            try:
                yield MountedNixConfigurableDispatch(
                    scope=scope,
                    service_ref=service_ref,
                    impl=impl,
                )
            finally:
                # Flush + tear down every subscription. The flush is
                # bounded here (the hub's aclose itself is unbounded):
                # on timeout, cancelling impl_tg force-closes every
                # subscription (a no-op when the flush completed and
                # the TG is already empty).
                with anyio.move_on_after(SUBSCRIPTION_FLUSH_TIMEOUT_SECONDS):
                    await impl.aclose_subscriptions()
                impl_tg.cancel_scope.cancel()
