"""Host-side implementation of :class:`McpHostProto`.

Constructed fresh per ``_ScriptJob`` and mounted on a per-script
``script_scope``. The resulting ``host_ref`` is threaded into each
sandbox via :class:`RunnerConfig.host_ref` so sandbox-side builtins
can call back to the host.

Methods:

- ``lookup_entity(short_id)`` — read-side; delegates to the
  workstation's ``get_entity`` via the connection-cached
  ``workstation_proxy``.
- ``copy_entity_in(handle, entity_id)`` — write-side; copies the
  entity from the per-RPC-call dispatch scope into the per-script
  ``script_scope`` and stashes ``(handle → fxtr_id)`` in
  :attr:`copied_entities` for the worker to read at end-of-script.
  Used by both ``export_entity`` (where ``handle`` derives from
  the agent slot_key via ``slot_handle``) and ``schedule_invoke``
  (where each entity arg gets a fresh ``invoke_arg:*`` handle).
- ``read_state_slot(slot_key)`` — read-side; returns the local
  mirror's value for ``slot_key`` (or None if absent). Used by
  ``retrieve_result``.
- ``lookup_short_id_by_external_name(name)`` — read-side; proxies
  to the workstation. Used by the sandbox-side
  ``lookup_service_by_name`` / ``lookup_entity_by_name`` builtins.
- ``read_callback_notification(slot_key, notif_index)`` /
  ``list_callback_notification_indices(slot_key)`` — read-side;
  pull from the local mirror's ``callback_notifications`` map.
  Used by the sandbox-side ``retrieve_callback_notification`` /
  ``callback_notification_keys`` builtins.
- ``retrieve_nix_entity(name, nix_location)`` — write-side; resolves the
  entity at ``<nix_location>.<name>`` by delegating to the ``fxtr_nix_runner``
  manager's entities adapter (build ``.layer`` → mount via the repo store →
  copy into ``script_scope``; a ``builtin`` resolves to its primitive FxtrID
  with no build/copy). Returns ``(fxtr_id, untracked_notice)``.
- ``current_commit()`` — read-only; the workdir's git HEAD sha, with a
  clean-tree guard (errors on a dirty tree / non-repo).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.leases.leased_cap_ir import IRException
from fxtr2.services.typed_service import TypedServiceImpl
from fxtr2.services.untyped_service import service_method
from fxtr_nix_runner import Backend, resolve_backend
from fxtr_workstation.protocols import WorkstationServiceProto
from fxtr_ws_mcp_sandbox_bridge.protocols import MCP_HOST_TYPE_ID, McpHostProto

from . import git_util
from .exception_log import ExceptionLog
from .session_state import SessionMirror


class McpHostImpl(TypedServiceImpl[McpHostProto]):
    """Implements :class:`McpHostProto` on the host side.

    Holds a connection-cached :class:`WorkstationServiceProto` proxy
    (resolved once at supervisor startup, lives in the connection's
    ``host_scope``), a per-script ``script_scope``, the per-script
    ``queued_ops`` accumulator, and a reference to the connection's
    :class:`SessionMirror` (read-only — for ``read_state_slot``).
    The proxy + mirror are shared across every script for the
    connection's lifetime; the scope and accumulator are recreated
    per ``run_script``.

    :attr:`copied_entities` is the host-side stash of entities
    passed in by ``copy_entity_in``, keyed by *handle* (see
    ``protocols.slot_handle`` / ``fresh_invoke_arg_handle``).
    The worker, after ``runner.run()`` returns, walks the
    runner's queued-ops list, derives the matching handle for
    each ``ExportEntityOp`` (``slot_handle(op.slot_key)``) and
    looks up the handles listed in each
    ``ScheduleInvokeOp.entity_arg_handles``, and reads the
    FxtrID for the workstation registration.
    """

    def __init__(
        self,
        workstation: WorkstationServiceProto,
        script_scope: FxtrScope,
        queued_ops: list[dict[str, Any]],
        mirror: SessionMirror,
        working_dir: Path,
        exception_log: ExceptionLog | None,
    ) -> None:
        self._ws = workstation
        self._script_scope = script_scope
        self._queued_ops = queued_ops
        self._mirror = mirror
        self._working_dir = working_dir
        # The nix backend (host nix, or in-podman builds — FXTR_NIX_BACKEND +
        # ~/.config/fxtr/nix_runtime.toml). Resolved **lazily** on first nix
        # retrieval (see `_get_nix_backend`), so constructing a host impl for a
        # non-nix script needs no backend config.
        self._nix_backend: Backend | None = None
        # Lifespan-scoped — shared across every script in the
        # process. ``None`` only on legacy/test paths that build a
        # bare McpHostImpl without going through ``make_app_ctx``;
        # ``get_full_exception`` raises a clear error in that case.
        self._exception_log = exception_log
        self.copied_entities: dict[str, FxtrID] = {}

    def _get_nix_backend(self) -> Backend:
        """The nix backend, resolved lazily on first use (cached) unless one was
        injected. Resolution requires a configured backend — so it only fails
        for scripts that actually do nix work on an unconfigured machine."""
        if self._nix_backend is None:
            self._nix_backend = resolve_backend()
        return self._nix_backend

    def _target_type_annotation(self) -> Any:
        return McpHostProto

    def _service_type_id(self) -> FxtrID:
        return MCP_HOST_TYPE_ID

    @service_method
    async def lookup_entity(self, short_id: str) -> FxtrID:
        return await self._ws.get_entity(short_id)

    @service_method
    async def copy_entity_in(self, handle: str, entity_id: FxtrID) -> None:
        if handle in self.copied_entities:
            raise ValueError(f"copy_entity_in: handle {handle!r} is already used in this script.")
        # The fxtr-RPC argument decode has already lifted ``entity_id``
        # into the dispatch scope (the active scope when this method
        # runs). Copy it out into ``script_scope`` so the entity
        # survives past dispatch-scope teardown — the worker will
        # register it via ``expose_entity`` from script_scope at
        # end-of-script.
        await self._script_scope.copy_from_scope(entity_id)
        self.copied_entities[handle] = entity_id

    @service_method
    async def read_state_slot(self, slot_key: str) -> list[Any] | None:
        """Return the local mirror's value for ``slot_key`` (the
        local view, including any unflushed effects), or None if
        absent. No round-trip to the workstation — the mirror is
        the authoritative source for the agent's view."""
        return self._mirror.data_by_key.get(slot_key)

    @service_method
    async def lookup_short_id_by_external_name(self, external_name: str) -> str:
        return await self._ws.lookup_short_id_by_external_name(external_name)

    @service_method
    async def read_callback_notification(self, slot_key: str, notif_index: int) -> Any:
        """Return the wire-escaped payload stored at
        ``mirror.callback_notifications[slot_key][str(notif_index)]``.

        Reads from the local mirror's ``callback_notifications``
        map, which is updated whenever ``_apply_state_effect``
        processes a :class:`CallbackEvent`. JSON object keys are
        strings, so we look up ``str(notif_index)`` even though
        the agent-facing API is keyed by int.

        Raises :class:`IRException` with ``category="KeyError"``
        when the notification isn't stored — fxtr-RPC propagates
        it across the wire, and the sandbox builtin re-raises
        a Python :class:`KeyError` to the agent.
        """
        per_slot = self._mirror.callback_notifications.get(slot_key, {})
        try:
            return per_slot[str(notif_index)]
        except KeyError:
            raise IRException(
                message=f"no callback notification for ({slot_key!r}, {notif_index})",
                category="KeyError",
            )

    @service_method
    async def list_callback_notification_indices(self, slot_key: str) -> list[int]:
        """Return the sorted list of ``notif_index`` values
        currently stored for ``slot_key``. Empty list if the
        slot has no stored notifications.
        """
        per_slot = self._mirror.callback_notifications.get(slot_key, {})
        return sorted(int(k) for k in per_slot)

    @service_method
    async def get_full_exception(self, exc_id: str) -> str:
        """Return the full ``traceback.format_exception`` string the
        host stored when a previous ``run_script`` raised.

        The sandbox-side ``get_full_exception(exc_id)`` builtin
        proxies to this method. The agent reads the ``exc_id`` from
        the compressed-traceback line in the failed
        ``run_script``'s tool result and passes it in here from a
        subsequent ``run_script`` call.

        Raises :class:`IRException` with ``category="LookupError"``
        when the id is unknown — either it was never stored or it
        fell off the bounded LRU. The sandbox builtin re-raises a
        Python :class:`LookupError`. Same shape if the exception
        log isn't configured at all (defensive — production always
        has one)."""
        if self._exception_log is None:
            raise IRException(
                message="get_full_exception: no exception log is configured on this host",
                category="LookupError",
            )
        body = self._exception_log.get(exc_id)
        if body is None:
            raise IRException(
                message=(
                    f"get_full_exception: no traceback stored for exc_id={exc_id!r} "
                    "(either it was never stored or the bounded LRU evicted it)"
                ),
                category="LookupError",
            )
        return body

    @service_method
    async def retrieve_nix_entity(self, name: str, nix_location: str) -> tuple[FxtrID, str]:
        """Materialize the nix DATA entity at ``<nix_location>.<name>``.

        Delegates to the ``fxtr_nix_runner`` manager's entities adapter — the
        same build/mount path the publishing serve uses, so the MCP is a normal
        consumer of the flake. Opens a :class:`FlakeManager` over the workdir,
        builds the entity's ``.layer`` + mounts it (resolving any remote-pointer
        closure through the workdir's repo store), and copies it into
        ``script_scope`` so the FxtrID survives this RPC dispatch and the return
        bridge can lift it into the sandbox. A ``builtin`` entity resolves to its
        primitive FxtrID with no build/copy. Returns ``(fxtr_id,
        untracked_notice)``.

        Errors map to the agent-facing ``NixEntityBuildFailed`` (the full nix
        stderr is kept in the message for ``get_full_exception``; don't truncate).
        """
        # The flake.nix-presence pre-check is host-side (we hold the workdir);
        # surface a friendly error before spinning up any builder / repo store.
        if not (self._working_dir / "flake.nix").is_file():
            raise IRException(
                message=(
                    f"No flake.nix in working dir {self._working_dir}, so nix-backed "
                    "entities are unavailable. Add a flake.nix declaring "
                    f"{nix_location}.<name> to use retrieve_nix_entity."
                ),
                category="NixEntityBuildFailed",
            )

        # Lazy import: the manager tier pulls in fxtr2 + the repo store; keep it
        # off the host-impl import path until a nix entity is actually retrieved.
        from fxtr_nix_runner.entities import NixEntityError
        from fxtr_nix_runner.manager import FlakeManager

        # Catch the adapter's failure (no such entity → KeyError; eval/build/
        # layerKey → NixEntityError) *inside* the build span — at its source,
        # before it crosses build_span's task-group boundary, which would re-wrap
        # it in an ExceptionGroup. Stash it and raise the mapped IRException once
        # the context managers have exited (no task group left to wrap it).
        result: Any = None
        build_error: IRException | None = None
        async with FlakeManager(self._working_dir, backend=self._get_nix_backend()) as mgr:
            async with mgr.build_span() as span:
                try:
                    # copy_into_scope=True gives script_scope an independent copy
                    # (resolving remote-pointer closures through the repo store)
                    # before the manager scope + materialized layer drain.
                    result = await mgr.entities(span).retrieve(
                        name,
                        nix_location=nix_location,
                        into_scope=self._script_scope,
                        copy_into_scope=True,
                    )
                except (KeyError, NixEntityError) as exc:
                    stderr = getattr(exc, "stderr", "")
                    detail = f"\n{stderr}" if stderr else ""
                    build_error = IRException(
                        message=f"{type(exc).__name__} for {nix_location}.{name}: {exc}{detail}",
                        category="NixEntityBuildFailed",
                    )
        if build_error is not None:
            raise build_error

        notice = await self._workdir_untracked_notice()
        return (cast(FxtrID, result), notice)

    @service_method
    async def current_commit(self) -> str:
        """Return the workdir's git HEAD sha, erroring on a dirty tree / non-repo."""
        try:
            return await git_util.head_commit(self._working_dir)
        except git_util.WorkingTreeDirty as exc:
            raise IRException(message=str(exc), category="WorkingTreeDirty")
        except git_util.NotAGitRepo as exc:
            raise IRException(message=str(exc), category="NotAGitRepo")
        except git_util.GitError as exc:
            raise IRException(message=str(exc), category="RuntimeError")

    async def _workdir_untracked_notice(self) -> str:
        """Count-only notice when the workdir is a git repo with untracked
        files (invisible to nix under git-form resolution), else ``""``.
        Not a service method — used to fill ``retrieve_nix_entity``'s tuple."""
        if not await git_util.is_git_repo(self._working_dir):
            return ""
        n = await git_util.untracked_count(self._working_dir)
        if n == 0:
            return ""
        return (
            f"note: {n} untracked file(s) are not visible to nix (it builds from "
            "tracked files only) — `git add` them if an entity depends on them."
        )
