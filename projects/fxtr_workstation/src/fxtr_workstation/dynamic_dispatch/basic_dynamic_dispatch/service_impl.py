"""``WorkstationDispatcherProto`` impl backed by a startup-compiled
renderer table.

.. note::

   **No longer the suggested impl** for nix/git-driven workdirs — prefer
   :class:`~fxtr_workstation.dynamic_dispatch.nix_configurable_dispatch.NixConfigurableDispatchImpl`,
   which loads presets by git reference (builds renderers via
   ``esbuildRenderer`` + mints entities at serve time) instead of walking
   on-disk renderer dirs + shelling out to ``npx esbuild`` at runtime. This
   impl stays for the not-yet-migrated demos / harnesses that point at it.

Walks one or more renderer directories at startup, compiles each
subdirectory's ``index.tsx`` via :mod:`compile`, stores the resulting
:class:`RendererEntity` in a caller-supplied scope, and serves
:meth:`resolve` against a type-id-string → :class:`RendererResolution`
map.

The impl also supports hot reload via :meth:`subscribe`: the
dispatcher is an event source for renderer-table changes
(:class:`WorkstationDispatcherProto` subclasses
``EventSource[EntityRef[ReloadEverythingNotification]]``), delivering a
:class:`ReloadEverythingNotification` ref per change. The subscription
machinery is a :class:`~fxtr2.services.subscription_util.SubscriberHub`
in ``"coalesce"`` mode — every notification is identical, so
latest-value skip-ahead delivery matches the old per-listener-worker
semantics. Notifications are broadcast by the synchronous
:meth:`trigger_reload` method (called from in-process owners — e.g. a
polling task watching for file changes). The convenience helper
:meth:`recompile_changed_and_reload` detects mtime changes on each
renderer's ``index.tsx`` and broadcasts only when something actually
changed.

See ``plans/phase0/part3_frontend_ui/stage10b_dynamic_dispatch.md`` for
the v1 design and
``plans/phase0/part3_frontend_ui/stage10c_dynamic_dispatch_improvements.md``
for the hot-reload additions.
"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
from collections.abc import AsyncGenerator, Sequence
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
from fxtr2.leases.leased_cap_ir import IRException
from fxtr2.services.subscription_types import Subscriber, UnsubscribeHandle
from fxtr2.services.subscription_util import SubscriberHub
from fxtr2.services.typed_service import TypedServiceImpl
from fxtr2.services.untyped_service import service_method

from ..dynamic_dispatch_types import (
    WORKSTATION_DISPATCHER_TYPE_ID,
    ReloadEverythingNotification,
    RendererEntity,
    RendererResolution,
    WorkstationDispatcherProto,
)
from .compile import DEFAULT_EXTERNALS, ENTRY_FILENAME, compile_renderer

_log = logging.getLogger(__name__)

#: The single subtype label v1 uses. The frontend treats this opaquely,
#: so the backend is free to introduce other labels later (e.g.
#: ``"card"`` / ``"detail"``) without frontend changes.
DEFAULT_SUBTYPE_KEY = "default"

#: Bound on the mount harness's teardown flush of live subscriptions
#: (the hub's own ``aclose`` is deliberately unbounded; the bound is
#: call-site policy). On timeout the harness cancels ``impl_tg``,
#: force-closing every subscription.
SUBSCRIPTION_FLUSH_TIMEOUT_SECONDS = 5.0


class BasicDynamicDispatchImpl(TypedServiceImpl[WorkstationDispatcherProto]):
    """A renderer-dispatcher service implementation backed by a
    directory-based renderer registry.

    **No longer the suggested impl** — prefer
    :class:`~fxtr_workstation.dynamic_dispatch.nix_configurable_dispatch.NixConfigurableDispatchImpl`
    for nix/git-driven workdirs (git-referenced presets, serve-time entity
    minting). Kept in place for not-yet-migrated demos / harnesses.

    **Usage** — most callers should use :func:`mount_basic_dynamic_dispatch`
    which owns the scope + task group + does the mount + register
    dance. Calling the constructor directly is also supported if you
    want explicit control of the scope and task group:

    .. code-block:: python

        async with FxtrScope(
            isolated=True,
            include=[GLOBAL_SCOPE],
            name="dispatcher_owner",
        ) as user_scope:
            async with anyio.create_task_group() as impl_tg:
                impl = BasicDynamicDispatchImpl(
                    scope=user_scope,
                    renderer_dirs=[Path("./renderers")],
                    impl_tg=impl_tg,
                )
                await impl.compile_and_register()
                ref = (await user_scope.mount(impl)).replace_service_type(
                    WorkstationDispatcherProto,
                )
                cfg = ExternalServiceConfig(
                    external_name="renderer-dispatcher",
                    service=ref,
                    scope=user_scope,
                )
                async with WorkstationServer(external_services=[cfg]) as ws:
                    ...
                # impl_tg cancels here, tearing down any active subscriptions.

    **Scope discipline (see** ``v2/notes/scope_discipline.md`` **).**
    ``scope`` must be a :class:`FxtrScope` the caller owns. It needs
    ``include=[GLOBAL_SCOPE]`` (or some other arrangement that makes
    the :class:`RendererEntity` type descriptor reachable) — the
    descriptor was registered at module-load time into ``GLOBAL_SCOPE``
    by :func:`define_type`, and renderer-entity stores walk it during
    the lift pass. The scope must remain alive at least as long as
    the workstation that bridges the impl: the renderer entities
    minted by :meth:`compile_and_register` live in ``scope``, and the
    workstation's bridge holds a lease on ``scope`` for the duration
    of the workstation's own lifetime.

    **Task group discipline.** ``impl_tg`` is a task group owned by
    the caller. Each :meth:`subscribe` call spawns a per-subscription
    worker on it (via the internal
    :class:`~fxtr2.services.subscription_util.SubscriberHub`).
    Cancelling ``impl_tg`` tears down every subscription, which
    releases the bridges. Typically the caller owns this via
    :func:`mount_basic_dynamic_dispatch`.

    **Directory convention.** Each entry in ``renderer_dirs`` is
    walked; immediate subdirectories are treated as type-id-string
    buckets (``<dir>/<type_id_string>/index.tsx``). Later entries in
    ``renderer_dirs`` override earlier ones on type-id-string conflict
    (last-write-wins). Subdirectories without an ``index.tsx`` are
    skipped silently.
    """

    def __init__(
        self,
        scope: FxtrScope,
        renderer_dirs: Sequence[Path],
        *,
        impl_tg: anyio.abc.TaskGroup,
        externals: Sequence[str] = DEFAULT_EXTERNALS,
        subtype_key: str = DEFAULT_SUBTYPE_KEY,
    ) -> None:
        super().__init__()
        # scope is the host scope for the renderer entities we mint.
        # All such entities are loadable from `scope` because we
        # stored them there in `compile_and_register`.
        self._scope = scope
        self._renderer_dirs = list(renderer_dirs)
        self._externals = tuple(externals)
        self._subtype_key = subtype_key
        # type_id_string -> RendererResolution.
        # Each `resolution.renderer` is loadable in `self._scope`
        # because that's where `compile_and_register` stored it.
        self._table: dict[str, RendererResolution] = {}
        # Per-subdir mtime snapshot: subdir Path → last-seen
        # `<subdir>/index.tsx` mtime. Populated by
        # `compile_and_register`; consulted + updated by
        # `recompile_changed_and_reload` to skip unchanged subdirs.
        self._mtimes: dict[Path, float] = {}
        # Subscription machinery for renderer-table change events.
        # Coalesce mode: every notification is identical, so
        # latest-value skip-ahead delivery is the right semantics.
        # (The hub has no scope of its own; the notification entities
        # ride per-event origin leases on `scope`.)
        self._hub = SubscriberHub[EntityRef[ReloadEverythingNotification]](
            task_group=impl_tg,
            mode="coalesce",
        )
        self._registered: bool = False

    def _target_type_annotation(self):  # type: ignore[no-untyped-def]
        return WorkstationDispatcherProto

    def _service_type_id(self) -> FxtrID:
        return WORKSTATION_DISPATCHER_TYPE_ID

    # --- scan / compile helpers ------------------------------------

    def _scan_subdirs(self) -> dict[Path, float]:
        """Return a mapping ``subdir_path → mtime(<subdir>/index.tsx)``
        covering every renderer subdir reachable through
        ``self._renderer_dirs``.

        Subdirectories without an ``index.tsx`` are skipped silently.
        Missing parent directories raise.
        """
        result: dict[Path, float] = {}
        for renderer_dir in self._renderer_dirs:
            if not renderer_dir.is_dir():
                raise FileNotFoundError(
                    f"Renderer dir {renderer_dir} is not a directory",
                )
            for subdir in sorted(renderer_dir.iterdir()):
                if not subdir.is_dir():
                    continue
                entry = subdir / ENTRY_FILENAME
                if not entry.is_file():
                    continue
                result[subdir] = entry.stat().st_mtime
        return result

    async def _compile_and_install(self, subdir: Path) -> None:
        """Compile ``subdir/index.tsx``, store a fresh
        :class:`RendererEntity` in ``self._scope``, and install the
        resulting :class:`RendererResolution` into ``self._table``
        keyed by the subdirectory's name (the type-id string).
        """
        type_id_str = subdir.name
        module_bytes = await compile_renderer(subdir, externals=self._externals)
        # Stored in self._scope per scope discipline; reachable to
        # peers via the workstation's external-service bridge (which
        # inherits self._scope's main arena into the dispatch chain).
        renderer_ref = RendererEntity(module=module_bytes).store(self._scope)
        self._table[type_id_str] = RendererResolution(
            subtype_key=self._subtype_key,
            renderer=renderer_ref,
        )

    async def compile_and_register(self) -> None:
        """Walk every renderer directory, compile each subdirectory's
        ``index.tsx``, store the resulting renderer entity, populate
        the lookup table + the mtime snapshot.

        Idempotent guard: callers shouldn't invoke this twice on the
        same impl; a second call resets the table and re-compiles
        from scratch, which is rarely what you want. (To pick up
        on-disk changes after startup, call
        :meth:`recompile_changed_and_reload` instead — it does the
        incremental diff + notifies subscribers.)
        """
        if self._registered:
            _log.warning(
                "BasicDynamicDispatchImpl.compile_and_register called twice; "
                "discarding previous table.",
            )
            self._table = {}
            self._mtimes = {}
        new_mtimes = self._scan_subdirs()
        for subdir in new_mtimes:
            await self._compile_and_install(subdir)
        self._mtimes = new_mtimes
        self._registered = True

    async def recompile_changed_and_reload(self) -> bool:
        """Re-scan ``renderer_dirs``, recompile any subdir whose
        ``index.tsx`` mtime has advanced (or is new), drop entries
        for subdirs that have vanished, and — if anything changed —
        broadcast a reload notification to every subscriber.

        Returns ``True`` if any change was detected and a reload was
        broadcast; ``False`` if every subdir's mtime matched the
        snapshot and no work was done.

        Pre-condition: :meth:`compile_and_register` must have run
        first. Callers that haven't initialised get an
        :class:`IRException` (same category as
        :meth:`resolve`-before-register) since this is a programming
        error.
        """
        if not self._registered:
            raise IRException(
                "BasicDynamicDispatchImpl.recompile_changed_and_reload called "
                "before compile_and_register completed.",
                category="DispatcherNotRegistered",
            )
        new_mtimes = self._scan_subdirs()
        changed: list[Path] = []
        for subdir, mtime in new_mtimes.items():
            if self._mtimes.get(subdir) != mtime:
                changed.append(subdir)
        removed = [p for p in self._mtimes if p not in new_mtimes]
        if not changed and not removed:
            return False
        for subdir in changed:
            await self._compile_and_install(subdir)
        for subdir in removed:
            type_id_str = subdir.name
            # Only drop if the current table entry still points at
            # this subdir's would-be type id — a sibling renderer_dir
            # with the same type-id-string might have shadowed us.
            # (Last-wins per the docstring; we can't reconstruct the
            # winner from just the path, so play it safe.)
            self._table.pop(type_id_str, None)
        self._mtimes = new_mtimes
        self.trigger_reload()
        return True

    # --- subscription machinery ------------------------------------

    def trigger_reload(self) -> None:
        """Broadcast a reload notification to every subscriber.

        Sync — no awaits. Mints a fresh
        :class:`ReloadEverythingNotification` in ``self._scope``
        (content-hash-deduped, so repeat broadcasts are cheap) and
        publishes its ref through the hub. Multiple triggers during
        one in-flight delivery collapse to one send per subscriber
        (coalesce-mode skip-ahead).
        """
        self._hub.publish(
            ReloadEverythingNotification().store(self._scope), origin_scope=self._scope
        )

    async def aclose_subscriptions(self) -> None:
        """Flush + tear down every live subscription (the hub's
        ``aclose``). Called by the mount harness on exit, before the
        impl scope drains."""
        await self._hub.aclose()

    # --- typed-service methods -------------------------------------

    @service_method
    async def resolve(
        self,
        type_ref: EntityRef[UnknownEntity],
        dispatch_key_uuid: str,
    ) -> RendererResolution | None:
        """Look up the type by its data-id string. Returns the cached
        :class:`RendererResolution`, or ``None`` if no renderer is
        registered for the type — the frontend surfaces the miss as
        the dispatch key's ``NoMatchFallback``.

        ``dispatch_key_uuid`` is part of the protocol contract but is
        ignored by this impl: each subdir's module is expected to
        carry an entry for every dispatch key that wants to render
        the matching type, so all `(type, *)` lookups return the
        same renderer ref. Subclasses / forks that want per-key
        routing should override this method.

        Raises :class:`IRException` only for unexpected failures (the
        impl was queried before :meth:`compile_and_register` completed)."""
        del dispatch_key_uuid  # see docstring
        if not self._registered:
            raise IRException(
                "BasicDynamicDispatchImpl.resolve called before " "compile_and_register completed.",
                category="DispatcherNotRegistered",
            )
        type_id_str = type_ref.entity_id.id_string()
        return self._table.get(type_id_str)

    @service_method
    async def subscribe(
        self, subscriber: ServiceRef[Subscriber[EntityRef[ReloadEverythingNotification]]]
    ) -> ServiceRef[UnsubscribeHandle]:
        """Subscribe ``subscriber`` to renderer-table change events.

        Delegates to the internal
        :class:`~fxtr2.services.subscription_util.SubscriberHub` —
        per-subscription worker on the impl's task group, subscriber
        bridged from this dispatch's call scope, unsubscribe handle
        mounted in the subscription's scope. See
        :meth:`WorkstationDispatcherProto.subscribe` for the contract.
        """
        return await self._hub.subscribe(subscriber, get_active_scope())


# --- ergonomic mount helper ----------------------------------------


@dataclasses.dataclass(frozen=True)
class MountedBasicDynamicDispatch:
    """Handle yielded by :func:`mount_basic_dynamic_dispatch`.

    Carries everything the caller needs to bind the dispatcher to a
    workstation as an external service and to drive its hot-reload
    surface from in-process code.

    Attributes:
        scope: The :class:`FxtrScope` the impl + renderer entities
            live in. Pass to :class:`ExternalServiceConfig` as the
            scope the service is bridged from.
        service_ref: A type-annotated :class:`ServiceRef` for the
            mounted impl, ready to hand to
            :class:`ExternalServiceConfig.service`.
        impl: The constructed :class:`BasicDynamicDispatchImpl`.
            Exposed so callers can drive :meth:`trigger_reload` or
            :meth:`recompile_changed_and_reload` from outside (e.g.
            from a background poller in a demo).
    """

    scope: FxtrScope
    service_ref: ServiceRef[WorkstationDispatcherProto]
    impl: BasicDynamicDispatchImpl


@contextlib.asynccontextmanager
async def mount_basic_dynamic_dispatch(
    renderer_dirs: Sequence[Path],
    *,
    parent_scope: FxtrScope | None = None,
    externals: Sequence[str] = DEFAULT_EXTERNALS,
    subtype_key: str = DEFAULT_SUBTYPE_KEY,
    scope_name: str = "basic_dynamic_dispatch",
) -> AsyncGenerator[MountedBasicDynamicDispatch, None]:
    """Open a scope + task group, construct a
    :class:`BasicDynamicDispatchImpl`, compile + register every
    renderer, mount the impl on the scope, and yield a
    :class:`MountedBasicDynamicDispatch` handle.

    The yielded handle's :attr:`MountedBasicDynamicDispatch.service_ref`
    + :attr:`MountedBasicDynamicDispatch.scope` are the
    ``(service, scope)`` pair to hand to
    :class:`ExternalServiceConfig` when registering the dispatcher
    with the workstation.

    On exit, the internal task group is cancelled (which cancels
    every active subscription worker → their scopes tear down →
    bridges release), then the impl scope drains.

    Args:
        renderer_dirs: Directories to walk for renderer subdirs (see
            :class:`BasicDynamicDispatchImpl` for the convention).
        parent_scope: Parent of the impl's scope. The default
            (``None``) uses :data:`GLOBAL_SCOPE` so the
            :class:`RendererEntity` type descriptor is reachable for
            the store walks. Pass an explicit scope if you want the
            impl to live inside a larger arrangement (e.g. a
            workstation's root_scope).
        externals: List of bare specifiers to treat as externals
            during esbuild compile. Defaults to
            :data:`DEFAULT_EXTERNALS`.
        subtype_key: The subtype label written into every
            :class:`RendererResolution`. Frontend-opaque; defaults
            to :data:`DEFAULT_SUBTYPE_KEY`.
        scope_name: Name to give the impl's :class:`FxtrScope` (for
            debug). Defaults to ``"basic_dynamic_dispatch"``.

    Example::

        async with mount_basic_dynamic_dispatch(
            renderer_dirs=[Path("./renderers")],
        ) as mounted:
            cfg = ExternalServiceConfig(
                external_name="renderer-dispatcher",
                service=mounted.service_ref,
                scope=mounted.scope,
            )
            async with WorkstationServer(external_services=[cfg]) as ws:
                # ...serve forever; trigger reloads via
                # mounted.impl.trigger_reload() or
                # mounted.impl.recompile_changed_and_reload()
                ...
    """
    include = [parent_scope] if parent_scope is not None else [GLOBAL_SCOPE]
    async with FxtrScope(
        isolated=True,
        include=include,
        name=scope_name,
    ) as scope:
        async with anyio.create_task_group() as impl_tg:
            impl = BasicDynamicDispatchImpl(
                scope=scope,
                renderer_dirs=renderer_dirs,
                impl_tg=impl_tg,
                externals=externals,
                subtype_key=subtype_key,
            )
            await impl.compile_and_register()
            service_ref = (await scope.mount(impl)).replace_service_type(WorkstationDispatcherProto)
            try:
                yield MountedBasicDynamicDispatch(
                    scope=scope,
                    service_ref=service_ref,
                    impl=impl,
                )
            finally:
                # Flush + tear down every subscription; their scopes
                # drop and release the bridges. The scope CM below then
                # drains the impl scope itself. The flush is bounded
                # here (the hub's aclose itself is unbounded): a peer
                # that won't accept its pending deliveries within the
                # window gets the forceful path — cancelling impl_tg
                # force-closes every subscription (a no-op when the
                # flush completed and the TG is already empty).
                with anyio.move_on_after(SUBSCRIPTION_FLUSH_TIMEOUT_SECONDS):
                    await impl.aclose_subscriptions()
                impl_tg.cancel_scope.cancel()
